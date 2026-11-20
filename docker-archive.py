#!/usr/bin/env python3
"""
docker-archive: back up idle Docker Compose projects to an external drive,
remove them from Docker to free space, and restore them later.

Unit of work = a compose project (all containers with the same
com.docker.compose.project label). Containers without that label are
treated as a one-container project named "solo-<container name>".

Commands (all are dry runs unless --yes is given):
  status   [--months N]               show projects, age, size, archive state
  backup   [--months N | PROJECT...]   archive projects idle >= N months
  verify   ARCHIVE_DIR                 re-check checksums, tars and image names
  remove   ARCHIVE_DIR                 delete the archived project from Docker
  restore  ARCHIVE_DIR [--start]       bring the project back into Docker
  archives                             list archives on the drive
  ensure   PROJECT|PATH [--start]      make sure a project is in Docker: restore the
                                       newest copy if it is not (for scripts and agents)
  install-skill [--remove|--status]    install the Claude Code skill that uses ensure

Safety rules:
  - The destination must be on a mounted external drive (not the Mac's own
    disk), so an unplugged SSD never fills the internal disk.
  - backup never removes anything. remove is separate and re-verifies first.
  - Projects with a running container are skipped, and so is a volume that a
    running container elsewhere is using. backup --pause-running instead pauses
    the project's running containers while copying and resumes them after.
  - Every container with any changed file is snapshotted (docker commit).
  - remove refuses if any volume changed since the backup (fingerprint), if a
    container was started or recreated since, or if the archive was restored.
  - Images or volumes still used by containers outside the archive are kept.
  - restore rolls back what it created if it fails partway.

Python 3.9 stdlib only.
"""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tarfile

os.environ["PATH"] = os.environ.get("PATH", "") + \
    ":/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/Applications/Docker.app/Contents/Resources/bin"

FORMAT_VERSION = 2
DEFAULT_DEST = os.environ.get("DOCKER_ARCHIVE_DEST", "/Volumes/External-SSD/docker-archive")
HELPER_CANDIDATES = ["alpine:latest", "alpine:3.23", "busybox:latest"]
HELPER_IMAGE = None
BUILTIN_NETS = ("bridge", "host", "none", "default")
_SOCK = None


# ---------------------------------------------------------------- helpers

class Abort(Exception):
    pass


def die(msg):
    raise Abort(msg)


def run(cmd, check=True, capture=True, stdin=None, stdout=None):
    """Run a command. Returns stdout text when capture=True, else the return code."""
    if stdout is not None:
        p = subprocess.run(cmd, stdin=stdin, stdout=stdout, stderr=subprocess.PIPE)
    else:
        p = subprocess.run(cmd, stdin=stdin, stdout=subprocess.PIPE if capture else None,
                           stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        err = p.stderr.decode(errors="replace").strip() if p.stderr else ""
        die("command failed (%d): %s\n%s" % (p.returncode, " ".join(cmd), err))
    if capture and stdout is None and p.stdout is not None:
        return p.stdout.decode(errors="replace")
    return p.returncode


def ok(cmd):
    """True if the command succeeds. Output discarded."""
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def docker_json(args):
    out = run(["docker"] + args)
    return json.loads(out) if out.strip() else []


def image_id(ref):
    """Image ID for a reference, or None if it does not exist."""
    p = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", ref],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return p.stdout.decode().strip() if p.returncode == 0 else None


def parse_time(s):
    if not s or s.startswith("0001-01-01"):
        return None
    m = re.match(r"^(.*?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", s)
    if not m:
        return None
    base, frac, tz = m.group(1), m.group(2), m.group(3) or "+00:00"
    if tz == "Z":
        tz = "+00:00"
    frac = ("." + (frac + "000000")[:6]) if frac else ""
    try:
        return dt.datetime.fromisoformat(base + frac + tz)
    except ValueError:
        return None


def now():
    return dt.datetime.now(dt.timezone.utc)


def months_between(a, b):
    return (b - a).days / 30.44


def human(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1000 or unit == "TB":
            return ("%.0f %s" if unit in ("B", "KB") else "%.1f %s") % (n, unit)
        n /= 1000.0


def parse_size(s):
    """'1.2GB' -> bytes (docker df style, base 1000)."""
    m = re.match(r"([\d.]+)\s*([kKMGT]?B)", s or "")
    if not m:
        return 0
    mult = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[m.group(2)]
    return int(float(m.group(1)) * mult)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def archive_repo(project, cname):
    """Valid lowercase image repo name, unique per container."""
    r = re.sub(r"[^a-z0-9._-]+", "-", ("%s-%s" % (project, cname)).lower()).strip("-._")
    return "docker-archive/" + (r or "x")


def host_path(p):
    """Docker Desktop reports Mac paths as /host_mnt/...; map back to the Mac path."""
    return p[len("/host_mnt"):] if p.startswith("/host_mnt/") else p


def confirm_plan(yes, what):
    if not yes:
        print("\nDry run. Nothing was changed. Re-run with --yes to %s." % what)
        sys.exit(0)


def docker_sock():
    global _SOCK
    if _SOCK:
        return _SOCK
    if os.environ.get("DOCKER_ARCHIVE_SOCK"):   # test hook
        _SOCK = os.environ["DOCKER_ARCHIVE_SOCK"]
        return _SOCK
    host = (run(["docker", "context", "inspect", "-f", "{{.Endpoints.docker.Host}}"], check=False) or "").strip()
    _SOCK = host[len("unix://"):] if host.startswith("unix://") else os.path.expanduser("~/.docker/run/docker.sock")
    return _SOCK


def fsync_path(p, full=True):
    fd = os.open(p, os.O_RDONLY)
    try:
        if full:
            try:
                fcntl.fcntl(fd, fcntl.F_FULLFSYNC)  # macOS: flush the drive's cache too
                return
            except (OSError, AttributeError):
                pass
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_tree(d):
    for root, dirs, files in os.walk(d):
        for f in files:
            fsync_path(os.path.join(root, f))
        fsync_path(root, full=False)


# ---------------------------------------------------------------- destination safety

def check_dest(dest, allow_internal=False, need_bytes=0, create=True):
    """Refuse destinations on the internal disk or on an unmounted drive."""
    dest = os.path.abspath(dest)
    if not allow_internal:
        if not dest.startswith("/Volumes/"):
            die("destination %s is not under /Volumes/. Use an external drive." % dest)
        mount = "/Volumes/" + dest.split("/")[2]
        if not os.path.ismount(mount):
            die("%s is not mounted. Plug in the external drive first.\n"
                "(Refusing so nothing gets written to the Mac's own disk.)" % mount)
        internal = {os.stat("/").st_dev}
        if os.path.exists("/System/Volumes/Data"):
            internal.add(os.stat("/System/Volumes/Data").st_dev)
        if os.stat(mount).st_dev in internal:
            die("%s is on the internal disk. Refusing." % mount)
        try:
            info = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", mount],
                                                 stdout=subprocess.PIPE, check=True).stdout)
            if info.get("Internal", False) and not info.get("RemovableMediaOrExternalDevice", False):
                die("%s is an internal disk. Refusing." % mount)
        except subprocess.CalledProcessError:
            die("could not identify the disk at %s" % mount)
        # the real target (after symlinks) must be on that same drive
        probe = dest
        while not os.path.exists(probe):
            probe = os.path.dirname(probe)
        real = os.path.realpath(probe)
        if os.stat(real).st_dev != os.stat(mount).st_dev:
            die("%s resolves to %s, which is not on %s. Refusing." % (dest, real, mount))
        base = mount
    else:
        base = dest
        while not os.path.exists(base):
            base = os.path.dirname(base)
    free = shutil.disk_usage(base).free
    if need_bytes and free < need_bytes * 1.1:
        die("not enough space on %s: %s free, need about %s"
            % (base, human(free), human(need_bytes * 1.1)))
    if create:
        os.makedirs(dest, exist_ok=True)
    return dest, free


# ---------------------------------------------------------------- docker inventory

def all_containers():
    ids = run(["docker", "ps", "-aq", "--no-trunc"]).split()
    return docker_json(["inspect"] + ids) if ids else []


def project_of(c):
    labels = c["Config"].get("Labels") or {}
    return labels.get("com.docker.compose.project") or ("solo-" + c["Name"].lstrip("/"))


def last_active(c):
    """Latest of StartedAt / FinishedAt, falling back to Created."""
    st = c["State"]
    times = [parse_time(st.get("StartedAt")), parse_time(st.get("FinishedAt"))]
    times = [t for t in times if t]
    return max(times) if times else parse_time(c.get("Created"))


def volume_sizes():
    """name -> bytes, from docker system df -v."""
    out = run(["docker", "system", "df", "-v", "--format", "{{json .}}"])
    sizes = {}
    try:
        data = json.loads(out)
        for v in data.get("Volumes") or []:
            sizes[v.get("Name")] = parse_size(v.get("Size"))
    except ValueError:
        pass
    return sizes


def group_projects(containers):
    projects = {}
    for c in containers:
        projects.setdefault(project_of(c), []).append(c)
    return projects


def volumes_of(c):
    return [m["Name"] for m in c.get("Mounts", []) if m["Type"] == "volume" and m.get("Name")]


def project_summary(name, cs, vsizes, image_cache):
    running = [c for c in cs if c["State"].get("Running")]
    lasts = [last_active(c) for c in cs]
    lasts = [t for t in lasts if t]
    vols = sorted({v for c in cs for v in volumes_of(c)})
    img_ids = sorted({c["Image"] for c in cs})
    img_bytes = 0
    for i in img_ids:
        if i not in image_cache:
            if ok(["docker", "image", "inspect", i]):
                image_cache[i] = docker_json(["image", "inspect", i])[0]
            else:
                image_cache[i] = {"Size": 0, "missing": True}
        img_bytes += image_cache[i].get("Size", 0)
    return {
        "name": name, "containers": cs, "running": len(running),
        "last": max(lasts) if lasts else None, "volumes": vols,
        "volume_bytes": sum(vsizes.get(v, 0) for v in vols),
        "images": img_ids, "image_bytes": img_bytes,
    }


# ---------------------------------------------------------------- helper container

def ensure_helper(prefer=None):
    """Pick a small image that really runs (listed images can be broken)."""
    global HELPER_IMAGE
    if HELPER_IMAGE:
        return HELPER_IMAGE
    for img in ([prefer] if prefer else []) + HELPER_CANDIDATES:
        if ok(["docker", "image", "inspect", img]) and \
                ok(["docker", "run", "--rm", "--pull", "never", "--network", "none", img, "true"]):
            HELPER_IMAGE = img
            return img
    print("  pulling helper image alpine:latest")
    run(["docker", "pull", "alpine:latest"], capture=False)
    HELPER_IMAGE = "alpine:latest"
    return HELPER_IMAGE


FINGERPRINT_SH = ("set -o pipefail; cd /data && find . -xdev -print0 "
                  "| xargs -0 stat -c '%n|%F|%s|%Y|%Z|%a|%u|%g' "
                  "| LC_ALL=C sort | sha256sum | cut -d' ' -f1")
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def volume_fingerprint(vol):
    """Hash of every path, type, size, mtime, mode and owner in the volume."""
    out = run(["docker", "run", "--rm", "--network", "none", "-v", "%s:/data:ro" % vol,
               ensure_helper(), "sh", "-c", FINGERPRINT_SH]).strip()
    if not re.match(r"^[0-9a-f]{64}$", out) or out == EMPTY_SHA256:
        die("could not fingerprint volume %s: %r" % (vol, out))
    return out


# ---------------------------------------------------------------- status

def cmd_status(a):
    cs = all_containers()
    projects = group_projects(cs)
    vsizes = volume_sizes()
    cache = {}
    rows = [project_summary(n, c, vsizes, cache) for n, c in projects.items()]
    rows.sort(key=lambda r: r["last"] or now())
    archived = set()
    if os.path.isdir(a.dest):
        archived = {p for p in os.listdir(a.dest) if os.path.isdir(os.path.join(a.dest, p))}
    print("%-26s %5s %8s %10s %10s  %s" % ("PROJECT", "CTRS", "IDLE", "IMAGES*", "VOLUMES", "STATE"))
    for r in rows:
        idle = months_between(r["last"], now()) if r["last"] else 0
        state = "running" if r["running"] else ("STALE" if idle >= a.months else "")
        if safe_name(r["name"]) in archived:
            state += " (archived)"
        print("%-26s %5d %7.1fm %10s %10s  %s" % (
            r["name"][:26], len(r["containers"]), idle, human(r["image_bytes"]),
            human(r["volume_bytes"]), state))
    print("\n* image sizes overlap where projects share base layers.")
    print("STALE = no container started in the last %g months." % a.months)
    used_vols = {v for c in cs for v in volumes_of(c)}
    orphan = {k: v for k, v in vsizes.items() if k not in used_vols}
    if orphan:
        print("\n%d volumes are not attached to any container (%s). They may hold old "
              "settings or data.\nThis tool does not touch them. Do NOT run "
              "'docker volume prune' without checking them first."
              % (len(orphan), human(sum(orphan.values()))))


# ---------------------------------------------------------------- backup

def select_projects(a, projects, vsizes, all_cs):
    cache = {}
    chosen = []
    for name, cs in projects.items():
        if a.projects:
            if name not in a.projects:
                continue
        s = project_summary(name, cs, vsizes, cache)
        if not a.projects and (not s["last"] or months_between(s["last"], now()) < a.months):
            continue
        if s["running"] and not getattr(a, "pause_running", False):
            print("skip %s: %d container(s) running" % (name, s["running"]))
            continue
        ids = {c["Id"] for c in cs}
        busy = sorted({"%s (used by running %s)" % (v, o["Name"].lstrip("/"))
                       for o in all_cs if o["Id"] not in ids and o["State"].get("Running")
                       for v in volumes_of(o) if v in s["volumes"]})
        if busy:
            print("skip %s: shared volume in use: %s" % (name, ", ".join(busy)))
            continue
        chosen.append(s)
    if a.projects:
        for m in sorted(set(a.projects) - set(projects)):
            print("skip %s: no such project" % m)
    return chosen


PAUSED_FILE = os.path.expanduser("~/.docker-archive-paused.json")


def save_paused(cs):
    with open(PAUSED_FILE, "w") as f:
        json.dump({"pid": os.getpid(), "ids": [c["Id"] for c in cs]}, f)


def resume_paused(cs):
    """Unpause what we paused. Signals are ignored meanwhile so a second stop can't skip this."""
    if not cs:
        return
    old = signal.signal(signal.SIGTERM, signal.SIG_IGN), signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        for c in cs:
            run(["docker", "unpause", c["Id"]], check=False)
            if run(["docker", "inspect", "-f", "{{.State.Paused}}", c["Id"]], check=False).strip() == "true":
                print("WARNING: could not resume %s. Run: docker unpause %s" % (c["Name"].lstrip("/"), c["Id"][:12]))
        print("  resumed %d paused container(s)" % len(cs))
        try:
            os.remove(PAUSED_FILE)
        except OSError:
            pass
    finally:
        signal.signal(signal.SIGTERM, old[0]); signal.signal(signal.SIGINT, old[1])


def resume_leftovers():
    """A run that was killed hard (SIGKILL) may have left containers paused. Resume them."""
    try:
        with open(PAUSED_FILE) as f:
            rec = json.load(f)
        ids, pid = rec["ids"], rec["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    try:
        os.kill(pid, 0)
        return            # that run is still going; its containers are paused on purpose
    except ProcessLookupError:
        pass
    except PermissionError:
        return
    print("Resuming containers a stopped run left paused: %s" % ", ".join(i[:12] for i in ids))
    resume_paused([{"Id": i, "Name": i[:12]} for i in ids])


def container_diff(cid):
    p = subprocess.run(["docker", "diff", cid], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        die("docker diff failed for %s: %s" % (cid[:12], p.stderr.decode(errors="replace").strip()))
    return [l for l in p.stdout.decode(errors="replace").splitlines() if l.strip()]


def is_short_id(ref):
    return bool(re.match(r"^(sha256:)?[0-9a-f]{12,64}$", ref or ""))


def plat_str(p):
    """'linux/amd64' (plus '/variant') from a platform dict, or None."""
    if not p or not p.get("os") or not p.get("architecture"):
        return None
    return "%s/%s%s" % (p["os"], p["architecture"], "/" + p["variant"] if p.get("variant") else "")


def os_arch(s):
    return "/".join((s or "").split("/")[:2])


def native_platform():
    return run(["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"]).strip()


def stored_platforms(tag):
    """Platforms whose content is stored locally for a multi-platform image; None for a plain image."""
    import urllib.parse
    try:
        code, js = api("GET", "/v1.48/images/%s/json?manifests=1" % urllib.parse.quote(tag, safe=""))
    except Abort:
        return None
    if not api_ok(code) or not js.get("Manifests"):
        return None
    return [plat_str((m.get("ImageData") or {}).get("Platform")) for m in js["Manifests"]
            if m.get("Available") and m.get("Kind") == "image"]


def save_platform(tag, want, native):
    """Platform to pass to docker save for this tag, or None to save it the normal way."""
    avail = [a for a in (stored_platforms(tag) or []) if a]
    if not avail:
        return None
    if want and os_arch(want) in [os_arch(a) for a in avail]:
        return None if os_arch(want) == os_arch(native) else next(a for a in avail if os_arch(a) == os_arch(want))
    if os_arch(native) in [os_arch(a) for a in avail]:
        return None
    return avail[0]


def base_images_for(tags):
    """Tagged images the given images were built on (FROM ...): their layers are the start of a project image's
    layers. No container runs them, but the project's images need them to be rebuilt. Shared layers make this free
    in images.tar."""
    proj = {}
    for t in tags:
        info = docker_json(["image", "inspect", t])[0]
        proj[info["Id"]] = info.get("RootFS", {}).get("Layers") or []
    ids = sorted(set(run(["docker", "images", "-q", "--no-trunc"]).split()) - set(proj))
    out = []
    for info in (docker_json(["image", "inspect"] + ids) if ids else []):
        L = info.get("RootFS", {}).get("Layers") or []
        tag = next((t for t in info.get("RepoTags") or [] if not t.startswith("docker-archive/")), None)
        if tag and L and any(len(P) > len(L) and P[:len(L)] == L for P in proj.values()):
            out.append(tag)
    return sorted(out)


def backup_project(s, dest_root, stamp):
    name = s["name"]
    adir = os.path.join(dest_root, safe_name(name), stamp)
    tmp = adir + ".partial"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(os.path.join(tmp, "volumes"))
    os.makedirs(os.path.join(tmp, "compose"))
    print("\n== %s -> %s" % (name, adir))
    helper = ensure_helper()

    manifest = {
        "tool": "docker-archive", "version": FORMAT_VERSION, "project": name,
        "created": now().isoformat(), "last_active": s["last"].isoformat() if s["last"] else None,
        "helper": helper, "containers": [], "volumes": [], "images": [], "networks": [],
        "compose": None,
    }
    save_tags = [helper]
    tagged = []   # archive tags we created (removed again after save)
    try:
        return _backup_body(s, name, adir, tmp, helper, manifest, save_tags, tagged)
    finally:
        for t in tagged:   # the tar has them; drop our temporary tags
            run(["docker", "rmi", "--no-prune", t], check=False)


def _backup_body(s, name, adir, tmp, helper, manifest, save_tags, tagged):
    want_platform = {}   # image tag -> platform the container ran as

    # ---- containers + images
    for c in s["containers"]:
        cname = c["Name"].lstrip("/")
        labels = c["Config"].get("Labels") or {}
        service = labels.get("com.docker.compose.service") or cname
        img_id = c["Image"]
        img_name = c["Config"].get("Image") or ""
        cplat = plat_str((c.get("ImageManifestDescriptor") or {}).get("platform"))
        repo = archive_repo(name, cname)
        img_present = image_id(img_id) is not None

        tag, override, retag = None, None, None
        if not img_present:
            print("  %s: its image was deleted earlier, saving from the container itself" % cname)
        elif img_name and "@" not in img_name and not is_short_id(img_name) \
                and image_id(img_name) == img_id:
            tag = img_name if ":" in img_name.split("/")[-1] else img_name + ":latest"
        else:
            # untagged, moved tag, short ID or digest-pinned: save under our own name
            tag = repo + ":orig"
            run(["docker", "tag", img_id, tag])
            tagged.append(tag)
            override = tag
            if "@" in img_name:
                base = img_name.split("@")[0]
                retag = base if ":" in base.split("/")[-1] else None
            print("  %s: image %s saved as %s" % (cname, (img_name or img_id)[:60], tag))
        if tag:
            save_tags.append(tag)
            if cplat:
                want_platform[tag] = cplat

        diff = container_diff(c["Id"])
        snapshot, rootfs = None, None
        if not img_present:
            # docker commit needs the parent image; export the whole filesystem instead
            rootfs = "rootfs/%s.tar" % safe_name(cname)
            os.makedirs(os.path.join(tmp, "rootfs"), exist_ok=True)
            print("  %s: exporting its full filesystem (image is gone)..." % cname)
            run(["docker", "export", "-o", os.path.join(tmp, rootfs), c["Id"]], capture=False)
        elif diff:
            snapshot = repo + ":snapshot"
            run(["docker", "commit", "-p=false", c["Id"], snapshot])
            tagged.append(snapshot)
            save_tags.append(snapshot)
            print("  %s: %d changed path(s) inside container, snapshot saved" % (cname, len(diff)))
        with open(os.path.join(tmp, "diff-%s.txt" % safe_name(cname)), "w") as f:
            f.write("\n".join(diff))

        binds = [m for m in c.get("Mounts", []) if m["Type"] == "bind"]
        for m in binds:
            if not os.path.exists(host_path(m["Source"])):
                print("  WARNING %s: bind mount %s does not exist on the Mac" % (cname, host_path(m["Source"])))
        manifest["containers"].append({
            "name": cname, "id": c["Id"], "service": service,
            "image_id": img_id, "image_name": img_name, "saved_tag": tag, "platform": cplat,
            "image_override": override, "retag": retag, "snapshot": snapshot, "rootfs": rootfs,
            "changed_paths": len(diff),
            "bind_mounts": [{"source": host_path(m["Source"]), "dest": m["Destination"]} for m in binds],
            "inspect": c,
        })

    # ---- small bind-mounted files and folders (config, secrets)
    save_bind_copies(manifest, tmp)

    # ---- networks (user-defined ones the containers join)
    nets = sorted({n for c in s["containers"]
                   for n in (c.get("NetworkSettings", {}).get("Networks") or {}) if n not in BUILTIN_NETS})
    manifest["networks"] = [docker_json(["network", "inspect", n])[0] for n in nets
                            if ok(["docker", "network", "inspect", n])]

    # ---- compose files
    first_labels = s["containers"][0]["Config"].get("Labels") or {}
    wd = first_labels.get("com.docker.compose.project.working_dir")
    cfgs = first_labels.get("com.docker.compose.project.config_files")
    if wd and cfgs:
        files = [f for f in cfgs.split(",") if f]
        copied = []
        for i, f in enumerate(files):
            if os.path.isfile(f):
                dst = "%02d-%s" % (i, os.path.basename(f))
                shutil.copy2(f, os.path.join(tmp, "compose", dst))
                copied.append({"source": f, "file": dst})
            else:
                print("  WARNING: compose file missing: %s" % f)
        envp = os.path.join(wd, ".env")
        if os.path.isfile(envp):
            shutil.copy2(envp, os.path.join(tmp, "compose", ".env"))
            copied.append({"source": envp, "file": ".env"})
        manifest["compose"] = {"working_dir": wd, "config_files": files, "copied": copied}

    # ---- images (one save, shared layers stored once)
    bases = set(base_images_for([t for t in set(save_tags) if t != helper]))
    if bases:
        print("  including %d base image(s) the project's images are built on: %s" % (len(bases), ", ".join(sorted(bases))))
    native = native_platform()
    by_plat = {}
    for t in sorted(set(save_tags) | bases):
        by_plat.setdefault(save_platform(t, want_platform.get(t), native), []).append(t)
    files = {}   # tag -> image file, for images not saved in images.tar
    for plat in sorted(by_plat, key=lambda x: x or ""):
        group = by_plat[plat]
        fname = "images.tar" if plat is None else "images-%s.tar" % plat.replace("/", "-")
        extra = [] if plat is None else ["--platform", plat]
        print("  saving %d image(s)%s..." % (len(group), "" if plat is None else " for " + plat))
        try:
            run(["docker", "save"] + extra + ["-o", os.path.join(tmp, fname)] + group, capture=False)
        except Abort:
            own = [t for t in group if t not in bases]
            if len(own) == len(group):
                raise
            print("  WARNING: saving with the base images failed; saving the project's own images only")
            bases -= set(group)
            group[:] = own
            if not own:
                continue
            run(["docker", "save"] + extra + ["-o", os.path.join(tmp, fname)] + own, capture=False)
        if plat is not None:
            files.update({t: (fname, plat) for t in group})
    if not os.path.exists(os.path.join(tmp, "images.tar")):   # every image was non-native: keep the format
        hp = save_platform(helper, None, native)
        run(["docker", "save"] + (["--platform", hp] if hp else []) + ["-o", os.path.join(tmp, "images.tar"), helper],
            capture=False)
    tags = sorted(t for g in by_plat.values() for t in g)
    for t in tags:
        iid = image_id(t)
        info = docker_json(["image", "inspect", t])[0]
        entry = {"tag": t, "id": iid, "all_tags": info.get("RepoTags") or [], "base": t in bases}
        if t in files:
            entry["file"], entry["platform"] = files[t]
            # a single-platform save stores that platform's image, not the multi-platform index
            ids_in_tar = tar_image_ids(os.path.join(tmp, entry["file"]))
            if t not in ids_in_tar:
                die("%s: could not read the ID of %s back from %s" % (name, t, entry["file"]))
            entry["id"] = ids_in_tar[t]
        manifest["images"].append(entry)

    # ---- volumes (fingerprint before and after: catches writes during the copy)
    for v in s["volumes"]:
        vin = docker_json(["volume", "inspect", v])[0]
        out = os.path.join(tmp, "volumes", safe_name(v) + ".tgz")
        print("  volume %s..." % v)
        fp = volume_fingerprint(v)
        with open(out, "wb") as f:
            run(["docker", "run", "--rm", "--network", "none", "-v", "%s:/data:ro" % v,
                 helper, "tar", "--numeric-owner", "-czf", "-", "-C", "/data", "."], stdout=f)
        if volume_fingerprint(v) != fp:
            die("volume %s changed while it was being copied. Stop whatever uses it and retry." % v)
        manifest["volumes"].append({"name": v, "file": "volumes/" + os.path.basename(out),
                                    "fingerprint": fp, "inspect": vin})

    # ---- checksums, flush to the drive, verify, then mark complete
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    write_readme(tmp, json.loads(json.dumps(manifest, default=str)))
    write_checksums(tmp)
    fsync_tree(tmp)
    good, msg = verify_dir(tmp, quiet=True, need_complete=False)
    if not good:
        die("verification failed for %s: %s (left at %s)" % (name, msg, tmp))
    with open(os.path.join(tmp, "COMPLETE"), "w") as f:
        f.write(now().isoformat() + "\n")
    fsync_path(os.path.join(tmp, "COMPLETE"))
    os.rename(tmp, adir)
    fsync_path(os.path.dirname(adir))
    total = sum(os.path.getsize(os.path.join(r, x)) for r, _, fs in os.walk(adir) for x in fs)
    print("  done: %s, verified" % human(total))
    return adir


def ports_of(ins):
    out = []
    for cport, binds in sorted((ins["HostConfig"].get("PortBindings") or {}).items()):
        for b in binds or []:
            out.append("%s:%s->%s" % (b.get("HostIp") or "", b.get("HostPort"), cport))
    return ", ".join(p.lstrip(":") for p in out) or "none"


def write_readme(tmp, m):
    """Plain-text guide to this backup, written into the archive itself."""
    L = []
    w = L.append
    w("DOCKER PROJECT BACKUP: %s" % m["project"])
    w("=" * (24 + len(m["project"])))
    w("")
    w("Made:          %s (UTC)" % m["created"][:19].replace("T", " "))
    w("Last used:     %s (UTC)" % ((m.get("last_active") or "unknown")[:19].replace("T", " ")))
    w("Made by:       docker-archive.py, format version %s" % m["version"])
    if m.get("compose"):
        w("Project folder: %s" % m["compose"]["working_dir"])
    w("")
    w("WHAT IS IN THIS FOLDER")
    w("----------------------")
    w("README.txt          this file")
    w("manifest.json       full record of every container, volume, image and network (docker inspect)")
    w("images.tar          all images the containers need, plus snapshots of changed containers")
    if any(c.get("rootfs") for c in m["containers"]):
        w("rootfs/             full filesystem of containers whose image no longer existed")
    w("volumes/            one .tgz per Docker volume (databases, uploads, app data)")
    w("compose/            copies of the compose files and .env (spare copies; may contain passwords)")
    w("diff-<name>.txt     list of files each container changed inside itself (A=added C=changed D=deleted)")
    w("checksums.sha256    fingerprints used to check nothing is damaged")
    w("COMPLETE            written last: the backup finished and was verified")
    w("REMOVED / RESTORED  appear when the project was removed from Docker / restored from here")
    w("")
    w("CONTAINERS")
    w("----------")
    vol_users = {}
    for c in m["containers"]:
        ins = c["inspect"]
        w("%s  (service: %s)" % (c["name"], c["service"]))
        w("  image:     %s" % (c["image_name"] or c["image_id"]))
        if c.get("snapshot"):
            w("  saved as:  snapshot of the container (%d changed paths, see diff-%s.txt)"
              % (c.get("changed_paths", 0), safe_name(c["name"])))
        elif c.get("rootfs"):
            w("  saved as:  full filesystem export, %s" % c["rootfs"])
        else:
            w("  saved as:  image only (nothing changed inside the container)")
        w("  ports:     %s" % ports_of(ins))
        for mnt in ins.get("Mounts", []):
            if mnt["Type"] == "volume" and mnt.get("Name"):
                vol_users.setdefault(mnt["Name"], []).append("%s at %s" % (c["name"], mnt["Destination"]))
                w("  volume:    %s -> %s  (in this backup)" % (mnt["Name"], mnt["Destination"]))
            elif mnt["Type"] == "tmpfs":
                w("  tmpfs:     %s  (memory only, never saved)" % mnt["Destination"])
        for b in c["bind_mounts"]:
            state = "exists on the Mac" if os.path.exists(b["source"]) else "MISSING on the Mac at backup time"
            kept = ("copy in %s" % b["copy"]) if b.get("copy") else \
                   ("NOT in this backup%s" % ((": " + b["not_copied"]) if b.get("not_copied") else ""))
            w("  Mac folder: %s -> %s  (%s, %s)" % (b["source"], b["dest"], kept, state))
        w("")
    w("VOLUMES")
    w("-------")
    for v in m["volumes"]:
        anon = "com.docker.volume.anonymous" in ((v["inspect"].get("Labels")) or {})
        users = "; ".join(vol_users.get(v["name"], [])) or "no container"
        w("%s" % v["file"])
        w("  volume:  %s%s" % (v["name"], " (anonymous)" if anon else ""))
        w("  used by: %s" % users)
    if not m["volumes"]:
        w("(none)")
    w("")
    w("NOT IN THIS BACKUP")
    w("------------------")
    w("- Folders on the Mac mounted into containers (listed above as 'Mac folder'). They stay where")
    w("  they are. Deleting that folder or repo loses them.")
    w("- Container logs (docker logs).")
    w("- tmpfs mounts (memory only).")
    w("")
    w("HOW TO RESTORE")
    w("--------------")
    w("With the tool (recommended):")
    w("  docker-archive.py restore \"%s\" --start --yes" % "<this folder>")
    w("  Restored containers do NOT auto-start when Docker launches, unless you add")
    w("  --keep-restart-policy. The command prints how to switch auto-start back on.")
    w("")
    w("By hand, if the tool is gone:")
    w("  1. docker load -i images.tar")
    w("  2. for each volume: docker volume create <name>, then")
    w("     docker run --rm -i -v <name>:/data alpine tar --numeric-owner -xzpf - -C /data < volumes/<file>.tgz")
    if any(c.get("rootfs") for c in m["containers"]):
        w("  2b. docker import rootfs/<name>.tar <new image name>")
    w("  3. recreate containers from the settings in manifest.json (containers[].inspect),")
    w("     or run 'docker compose up' in the project folder using the restored image names.")
    w("")
    w("SECURITY: compose/.env, manifest.json and binds/ (copies of bind-mounted files, often config and")
    w("secrets) can contain passwords and API keys in plain text.")
    with open(os.path.join(tmp, "README.txt"), "w") as f:
        f.write("\n".join(L) + "\n")


def write_checksums(d):
    lines = []
    for root, _, files in os.walk(d):
        for fn in sorted(files):
            if fn in ("checksums.sha256", "COMPLETE", "REMOVING", "REMOVED", "RESTORED"):
                continue
            p = os.path.join(root, fn)
            lines.append("%s  %s" % (sha256_file(p), os.path.relpath(p, d)))
    lines.sort(key=lambda l: l[66:])
    with open(os.path.join(d, "checksums.sha256"), "w") as f:
        f.write("\n".join(lines) + "\n")


def tar_image_ids(path):
    """{image name: image ID} as stored in an OCI docker save tar (index.json)."""
    out = {}
    with tarfile.open(path) as t:
        try:
            idx = json.load(t.extractfile("index.json"))
        except (KeyError, ValueError, AttributeError):
            return out
    for e in idx.get("manifests") or []:
        n = (e.get("annotations") or {}).get("io.containerd.image.name")
        if n:
            for pre in ("docker.io/library/", "docker.io/"):
                if n.startswith(pre):
                    n = n[len(pre):]
                    break
            out[n] = e["digest"]
    return out


def tar_image_names(path):
    """All image names recorded inside a docker save tar (docker and OCI formats)."""
    names = set()
    with tarfile.open(path) as t:
        for member in ("manifest.json", "index.json"):
            try:
                data = json.load(t.extractfile(member))
            except (KeyError, ValueError, AttributeError):
                continue
            if member == "manifest.json":
                for e in data or []:
                    names.update(e.get("RepoTags") or [])
            else:
                for e in data.get("manifests") or []:
                    ann = e.get("annotations") or {}
                    for k in ("io.containerd.image.name", "org.opencontainers.image.ref.name"):
                        if ann.get(k):
                            names.add(ann[k])
    norm = set()
    for n in names:
        norm.add(n)
        for pre in ("docker.io/library/", "docker.io/"):
            if n.startswith(pre):
                norm.add(n[len(pre):])
    return norm


def verify_dir(d, quiet=False, need_complete=True, extras_strict=True):
    if need_complete and not os.path.isfile(os.path.join(d, "COMPLETE")):
        return False, "not a complete archive (no COMPLETE marker)"
    if not os.path.isfile(os.path.join(d, "manifest.json")):
        return False, "no manifest.json"
    if not os.path.isfile(os.path.join(d, "checksums.sha256")):
        return False, "no checksums.sha256"
    listed = {}
    with open(os.path.join(d, "checksums.sha256")) as f:
        for line in f:
            line = line.rstrip("\n")
            if line:
                listed[line[66:]] = line[:64]
    m = load_manifest(d)
    required = {"manifest.json", "images.tar"} | {v["file"] for v in m["volumes"]} | \
        {c["rootfs"] for c in m["containers"] if c.get("rootfs")} | {i["file"] for i in m["images"] if i.get("file")} | \
        {b["copy"] for c in m["containers"] for b in c["bind_mounts"] if b.get("kind") == "file"}
    missing = required - set(listed)
    if missing:
        return False, "checksum list is missing: " + ", ".join(sorted(missing))
    for rel, digest in listed.items():
        p = os.path.join(d, rel)
        bad = "missing " + rel if not os.path.isfile(p) else \
              "checksum mismatch " + rel if sha256_file(p) != digest else None
        if bad and not extras_strict and (rel == EXTRA_LIST or rel.startswith("extra-images-")):
            EXTRA_PROBLEM.append(bad)
            print("  WARNING: %s. Restoring the project without the extra images." % bad)
        elif bad:
            return False, bad
    try:
        names = tar_image_names(os.path.join(d, "images.tar"))
        for f in sorted({i["file"] for i in m["images"] if i.get("file")}):
            names |= tar_image_names(os.path.join(d, f))
    except (tarfile.TarError, OSError) as e:
        return False, "image file unreadable: %s" % e
    unnamed = [i["tag"] for i in m["images"] if i["tag"] not in names]
    if unnamed:
        return False, "images.tar has no entry named: " + ", ".join(unnamed)
    if os.path.exists(os.path.join(d, EXTRA_LIST)):
        if EXTRA_LIST not in listed and extras_strict:
            return False, "checksum list is missing: " + EXTRA_LIST
        good, msg = verify_extra(d)
        if not good and extras_strict:
            return False, msg
        if not good:
            EXTRA_PROBLEM.append(msg)
            print("  WARNING: extra images: %s. Restoring the project without them." % msg)
    for c in m["containers"]:
        if c.get("rootfs"):
            p = subprocess.run(["tar", "-tf", os.path.join(d, c["rootfs"])],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if p.returncode != 0:
                return False, "filesystem export unreadable: " + c["rootfs"]
    for v in m["volumes"]:
        p = subprocess.run(["tar", "-tzf", os.path.join(d, v["file"])],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if p.returncode != 0:
            return False, "volume archive unreadable: " + v["file"]
    if not quiet:
        print("OK: %s (%d containers, %d volumes, %d images)"
              % (d, len(m["containers"]), len(m["volumes"]), len(m["images"])))
    return True, "ok"


def load_manifest(d):
    with open(os.path.join(d, "manifest.json")) as f:
        m = json.load(f)
    if m.get("version", 1) < 2:
        die("%s was made by an older version of this tool. Back the project up again." % d)
    return m


def cmd_backup(a):
    cs = all_containers()
    projects = group_projects(cs)
    vsizes = volume_sizes()
    chosen = select_projects(a, projects, vsizes, cs)
    if not chosen:
        print("Nothing to back up.")
        return
    need = sum(s["image_bytes"] + s["volume_bytes"] for s in chosen)
    print("\nWill back up %d project(s), up to about %s before compression:" % (len(chosen), human(need)))
    for s in chosen:
        idle = months_between(s["last"], now()) if s["last"] else 0
        print("  %-26s idle %.1f months, %d containers, %d volumes (%s), images %s" % (
            s["name"], idle, len(s["containers"]), len(s["volumes"]),
            human(s["volume_bytes"]), human(s["image_bytes"])))
    dest, free = check_dest(a.dest, a.allow_internal_dest, need_bytes=need, create=a.yes)
    print("Destination: %s (%s free)" % (dest, human(free)))
    if getattr(a, "remove_after", False):
        print("After each verified archive, that project will be REMOVED from Docker.")
    confirm_plan(a.yes, "write the archives" + (" and remove the projects from Docker"
                                                if getattr(a, "remove_after", False) else " (nothing is removed from Docker)"))
    stamp = now().strftime("%Y%m%d-%H%M%S")
    done = []
    kind = "archive" if getattr(a, "remove_after", False) else "backup"
    for s in chosen:
        paused = []
        try:
            if getattr(a, "pause_running", False):
                for c in s["containers"]:
                    if c["State"].get("Running") and not c["State"].get("Paused"):
                        paused.append(c)
                        save_paused(paused)
                        run(["docker", "pause", c["Id"]])
                if paused:
                    print("  paused %d running container(s) of %s while copying" % (len(paused), s["name"]))
            adir = backup_project(s, dest, stamp)
        except Abort as e:
            log_history(dest, kind, s["name"], result="failed: %s" % e, auto=getattr(a, "auto_run", None))
            if getattr(a, "auto_run", None):
                print("auto: %s failed, going on with the others: %s" % (s["name"], e))
                continue
            raise
        finally:
            resume_paused(paused)
        done.append(adir)
        log_history(dest, kind, s["name"], adir, size=dir_size(adir),
                    paused_running=len(paused) or None, auto=getattr(a, "auto_run", None))
        apply_retention(dest, s["name"])
    print("\n%s %d project(s)." % ("Archived" if kind == "archive" else "Backed up", len(done)))
    if not getattr(a, "remove_after", False):
        print("Nothing was removed from Docker. To free the space, run:  docker-archive.py remove <archive dir>")
    for d in done:
        print("  " + d)
    if getattr(a, "remove_after", False):
        for d in done:
            proj = os.path.basename(os.path.dirname(d))
            print("\n== removing %s from Docker (archive verified)" % proj)
            try:
                cmd_remove(argparse.Namespace(archive=d, yes=True))
            except Abort as e:
                print("NOT removed, %s stays in Docker: %s" % (proj, e))
                log_history(dest, "remove", proj, d, result="failed: %s" % e)
    print("Eject the drive in Finder before unplugging it.")
    return done


# ---------------------------------------------------------------- verify

def cmd_verify(a):
    d = os.path.abspath(a.archive)
    good, msg = verify_dir(d)
    log_history(dest_root_of(d), "verify", archive_project(d), d, result="ok" if good else "failed: %s" % msg)
    if not good:
        die(msg)


# ---------------------------------------------------------------- remove

def cmd_remove(a):
    d = os.path.abspath(a.archive)
    good, msg = verify_dir(d)
    if not good:
        die("archive failed verification, refusing to remove: " + msg)
    m = load_manifest(d)
    if os.path.exists(os.path.join(d, "RESTORED")):
        die("this archive was restored since it was made. The project may have changed: back it up again.")
    if os.path.exists(os.path.join(d, "REMOVED")):
        print("This archive was already removed from Docker.")
        return
    resuming = os.path.exists(os.path.join(d, "REMOVING"))

    ids = {c["id"] for c in m["containers"]}
    current = all_containers()
    present = [c for c in current if c["Id"] in ids]
    others = [c for c in current if c["Id"] not in ids]
    gone = [c["name"] for c in m["containers"] if c["id"] not in {x["Id"] for x in present}]
    if gone and not resuming:
        die("archived container(s) no longer exist: %s.\nThe project was probably recreated "
            "after the backup, so it may hold newer data. Back it up again." % ", ".join(gone))
    running = [c["Name"].lstrip("/") for c in present if c["State"].get("Running")]
    if running:
        die("these containers are running now: %s. Stop them first." % ", ".join(running))
    for c in present:
        rec = next(x for x in m["containers"] if x["id"] == c["Id"])
        if rec["inspect"]["State"].get("Running"):
            die("%s was running when this backup was made, so it may have kept changing after it. "
                "Stop the project and archive it again." % c["Name"].lstrip("/"))
        if c["State"].get("StartedAt") != rec["inspect"]["State"].get("StartedAt"):
            die("%s was started after the backup. Back it up again first." % c["Name"].lstrip("/"))

    for c in present:
        rec = next(x for x in m["containers"] if x["id"] == c["Id"])
        saved = os.path.join(d, "diff-%s.txt" % safe_name(rec["name"]))
        if os.path.isfile(saved):
            with open(saved) as f:
                old = sorted(l for l in f.read().splitlines() if l.strip())
            if sorted(container_diff(c["Id"])) != old:
                die("files inside %s changed since the backup (docker cp?). Back it up again first."
                    % rec["name"])
    other_vols = {v for c in others for v in volumes_of(c)}
    existing_vols = set(run(["docker", "volume", "ls", "-q"]).split())
    vols_rm, vols_keep = [], []
    for v in m["volumes"]:
        if v["name"] not in existing_vols:
            continue
        (vols_keep if v["name"] in other_vols else vols_rm).append(v)
    print("Checking volumes still match the backup (runs a small helper container per volume)...")
    for v in vols_rm:
        if volume_fingerprint(v["name"]) != v["fingerprint"]:
            die("volume %s changed since the backup. Back it up again first." % v["name"])

    other_imgs = {c["Image"] for c in others}
    helper_ids = {image_id(h) for h in HELPER_CANDIDATES} - {None}
    img_ids = sorted({c["image_id"] for c in m["containers"]})
    imgs_rm = [i for i in img_ids if i not in other_imgs and i not in helper_ids and image_id(i)]
    imgs_keep = [i for i in img_ids if i in other_imgs]
    base_ids = [i["id"] for i in m["images"] if i.get("base") and i["id"] not in img_ids
                and i["id"] not in other_imgs and i["id"] not in helper_ids and image_id(i["id"])]
    proj_nets = [n["Name"] for n in m.get("networks") or []
                 if (n.get("Labels") or {}).get("com.docker.compose.project") == m["project"]]

    print("Archive verified: %s" % d)
    if resuming:
        print("(continuing an interrupted remove)")
    print("Will remove from Docker:")
    print("  %d container(s): %s" % (len(present), ", ".join(c["Name"].lstrip("/") for c in present) or "-"))
    print("  %d volume(s): %s" % (len(vols_rm), ", ".join(v["name"] for v in vols_rm) or "-"))
    if vols_keep:
        print("  keeping volume(s) used by other containers: %s" % ", ".join(v["name"] for v in vols_keep))
    print("  %d image(s) not used elsewhere" % len(imgs_rm))
    if imgs_keep:
        print("  keeping %d image(s) used by other containers" % len(imgs_keep))
    if base_ids:
        print("  base image(s), unless another image is still built on them: %s"
              % ", ".join(next(i["tag"] for i in m["images"] if i["id"] == b) for b in base_ids))
    print("  network(s) if unused: %s" % (", ".join(proj_nets) or "-"))
    print("\nVolume deletion is permanent inside Docker. The only copy left will be the archive above.")
    confirm_plan(a.yes, "remove them")

    with open(os.path.join(d, "REMOVING"), "w") as f:
        f.write(now().isoformat() + "\n")
    for c in present:
        run(["docker", "rm", c["Id"]])
    for v in vols_rm:
        if ok(["docker", "volume", "inspect", v["name"]]):
            run(["docker", "volume", "rm", v["name"]])
    for i in imgs_rm:
        if not image_id(i):
            continue
        for t in docker_json(["image", "inspect", i])[0].get("RepoTags") or []:
            run(["docker", "rmi", "--no-prune", t], check=False)
        if image_id(i):
            run(["docker", "rmi", "--no-prune", i], check=False)
    base_info = [docker_json(["image", "inspect", i])[0] for i in base_ids]
    bases_removed = []
    for info in sorted(base_info, key=lambda x: -len(x.get("RootFS", {}).get("Layers") or [])):
        why = image_users(info["Id"], info.get("RootFS", {}).get("Layers") or [], set())
        if why:
            print("  keeping base image %s: %s" % ((info.get("RepoTags") or ["?"])[0], "; ".join(why)))
            continue
        for t in info.get("RepoTags") or []:
            run(["docker", "rmi", "--no-prune", t], check=False)
            bases_removed.append(t)
    still_used = {n for c in all_containers() for n in (c.get("NetworkSettings", {}).get("Networks") or {})}
    for n in proj_nets:
        if n not in still_used and ok(["docker", "network", "inspect", n]):
            run(["docker", "network", "rm", n], check=False)
    with open(os.path.join(d, "REMOVED"), "w") as f:
        f.write(now().isoformat() + "\n")
    os.remove(os.path.join(d, "REMOVING"))
    log_history(dest_root_of(d), "remove", m["project"], d,
                containers=len(present), volumes=len(vols_rm), images=len(imgs_rm),
                base_images=", ".join(bases_removed) or None)
    print("Removed. Docker Desktop gives the space back to macOS gradually, so Docker.raw shrinks over time.")
    print("Restore with:  docker-archive.py restore \"%s\"" % d)


# ---------------------------------------------------------------- restore

def api(method, path, body=None):
    """Call the Docker Engine API over the local socket. Returns (status, json)."""
    cmd = ["curl", "-sS", "-o", "-", "-w", "\n%{http_code}", "--unix-socket", docker_sock(),
           "-X", method, "http://localhost" + path]
    data = None
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", "@-"]
        data = json.dumps(body).encode()
    p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        die("cannot reach Docker at %s: %s" % (docker_sock(), p.stderr.decode(errors="replace").strip()))
    out = p.stdout.decode(errors="replace")
    text, _, code = out.rpartition("\n")
    try:
        js = json.loads(text) if text.strip() else {}
    except ValueError:
        js = {"message": text}
    return int(code or 0), js


def api_ok(code):
    return 200 <= code < 300


def recreate_network(n, created):
    """Recreate a user network with its original driver, labels, options and subnets."""
    name = n["Name"]
    if ok(["docker", "network", "inspect", name]):
        return
    ipam = n.get("IPAM") or {}
    body = {
        "Name": name, "Driver": n.get("Driver", "bridge"),
        "Internal": n.get("Internal", False), "Attachable": n.get("Attachable", False),
        "EnableIPv6": n.get("EnableIPv6", False),
        "Labels": n.get("Labels") or {}, "Options": n.get("Options") or {},
        "IPAM": {"Driver": ipam.get("Driver", "default"), "Config": ipam.get("Config") or [],
                 "Options": ipam.get("Options") or {}},
    }
    code, js = api("POST", "/networks/create", body)
    if not api_ok(code) and "overlap" in (js.get("message") or "").lower():
        body["IPAM"]["Config"] = []
        code, js = api("POST", "/networks/create", body)
        if api_ok(code):
            print("  network %s: original subnet is taken, Docker picked a new one" % name)
    if not api_ok(code):
        die("could not create network %s: %s" % (name, js.get("message")))
    created["networks"].append(name)


def rewrite_container_refs(value, id_to_name):
    """container:<old id> -> container:<name>"""
    if isinstance(value, str) and value.startswith("container:"):
        ref = value[len("container:"):]
        for i, n in id_to_name.items():
            if i.startswith(ref) or ref == n:
                return "container:" + n
    return value


def create_exact(c, image_ref, id_to_name, keep_restart=False, paused=None):
    """Recreate a container from its saved docker inspect: same name, labels, ports, mounts."""
    ins = c["inspect"]
    body = dict(ins["Config"])
    body["Image"] = image_ref
    hc = json.loads(json.dumps(ins["HostConfig"]))
    for k in ("NetworkMode", "PidMode", "IpcMode", "UTSMode"):
        if hc.get(k):
            hc[k] = rewrite_container_refs(hc[k], id_to_name)
    if hc.get("VolumesFrom"):
        hc["VolumesFrom"] = [rewrite_container_refs("container:" + v, id_to_name)[len("container:"):]
                             for v in hc["VolumesFrom"]]
    # anonymous / implicit volumes: attach the same named volume again
    vol_by_dest = {mnt["Destination"]: mnt["Name"] for mnt in ins.get("Mounts", [])
                   if mnt["Type"] == "volume" and mnt.get("Name")}
    for mnt in hc.get("Mounts") or []:
        if mnt.get("Type") == "volume" and not mnt.get("Source") and mnt.get("Target") in vol_by_dest:
            mnt["Source"] = vol_by_dest[mnt["Target"]]
    covered = set()
    for b in hc.get("Binds") or []:
        parts = b.split(":")
        if len(parts) >= 2:
            covered.add(parts[1])
    for mnt in hc.get("Mounts") or []:
        covered.add(mnt.get("Target"))
    extra = []
    for mnt in ins.get("Mounts", []):
        if mnt["Type"] == "volume" and mnt.get("Name") and mnt["Destination"] not in covered:
            extra.append("%s:%s%s" % (mnt["Name"], mnt["Destination"], "" if mnt.get("RW", True) else ":ro"))
    if extra:
        hc["Binds"] = (hc.get("Binds") or []) + extra
    # default: no auto-start after restore; the original policy is printed so it can be put back
    pol = (hc.get("RestartPolicy") or {}).get("Name")
    if not keep_restart and pol not in (None, "", "no"):
        if paused is not None:
            paused.append((c["name"], pol))
        hc["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    # never let Docker auto-start a container whose bind folders are missing
    if any(bind_problem(b) for b in c["bind_mounts"]):
        if (hc.get("RestartPolicy") or {}).get("Name") not in (None, "", "no"):
            print("  %s: restart policy set to 'no' because bind mount folders are missing" % c["name"])
            hc["RestartPolicy"] = {"Name": "no", "MaximumRetryCount": 0}
    body["HostConfig"] = hc
    eps = {}
    for net, ep in (ins.get("NetworkSettings", {}).get("Networks") or {}).items():
        e = {"Aliases": ep.get("Aliases"), "DriverOpts": ep.get("DriverOpts"), "Links": ep.get("Links")}
        if ep.get("IPAMConfig"):
            e["IPAMConfig"] = ep["IPAMConfig"]
        if ep.get("MacAddress") and hc.get("NetworkMode") not in ("host", "none"):
            e["MacAddress"] = ep["MacAddress"]
        eps[net] = e
    body["NetworkingConfig"] = {"EndpointsConfig": eps}
    if str(hc.get("NetworkMode", "")).startswith("container:"):
        # shares another container's network: Docker forbids its own hostname, ports, DNS
        for k in ("Hostname", "Domainname", "ExposedPorts", "MacAddress"):
            body.pop(k, None)
        for k in ("PortBindings", "Dns", "DnsOptions", "DnsSearch", "ExtraHosts", "Links"):
            hc.pop(k, None)
        hc["PublishAllPorts"] = False
        body["NetworkingConfig"] = {}
    # pass the image's own platform, so an amd64 image on an Apple-chip Mac is created as amd64 again
    query = "name=" + c["name"]
    plat = c.get("platform")
    if not plat:
        try:
            ii = docker_json(["image", "inspect", body["Image"]])[0]
            plat = plat_str({"os": ii.get("Os"), "architecture": ii.get("Architecture"), "variant": ii.get("Variant")})
        except Abort:
            plat = None
    if plat:
        query += "&platform=" + plat
    code, js = api("POST", "/containers/create?" + query, body)
    if not api_ok(code):
        die("could not create %s: %s" % (c["name"], js.get("message")))
    for w in js.get("Warnings") or []:
        print("  %s: %s" % (c["name"], w))


def create_order(containers):
    """Containers referenced via container:<x> first."""
    names = {c["name"] for c in containers}
    refs = {}
    for c in containers:
        hc = c["inspect"]["HostConfig"]
        r = [hc.get(k, "") for k in ("NetworkMode", "PidMode", "IpcMode")] + \
            ["container:" + v for v in hc.get("VolumesFrom") or []]
        refs[c["name"]] = len([x for x in r if isinstance(x, str) and x.startswith("container:")])
    return sorted(containers, key=lambda c: refs[c["name"]]) if names else containers


def start_order(containers):
    """Start dependencies first, using compose's depends_on label."""
    by_service = {c["service"]: c for c in containers}
    done, order = set(), []

    def visit(c, stack=()):
        if c["name"] in done or c["name"] in stack:
            return
        labels = c["inspect"]["Config"].get("Labels") or {}
        for dep in (labels.get("com.docker.compose.depends_on") or "").split(","):
            svc = dep.split(":")[0]
            if svc in by_service:
                visit(by_service[svc], stack + (c["name"],))
        done.add(c["name"])
        order.append(c)

    for c in containers:
        visit(c)
    return order


def rollback(created):
    print("\nRestore failed. Undoing what this run created...")
    left = []
    for kind, cmd in (("containers", ["docker", "rm", "-f"]), ("volumes", ["docker", "volume", "rm", "-f"]),
                      ("networks", ["docker", "network", "rm"])):
        for n in created[kind]:
            if not ok(cmd + [n]):
                left.append("%s %s" % (kind[:-1], n))
    if left:
        print("Could NOT undo: %s\nRemove these by hand before restoring again." % ", ".join(left))
    else:
        print("Rolled back. Docker is as it was before the restore (loaded images are kept).")


def cmd_restore(a):
    restore_lock(wait=False)
    del EXTRA_PROBLEM[:]
    d = os.path.abspath(a.archive)
    good, msg = verify_dir(d, extras_strict=False)
    if not good:
        die(msg)
    m = load_manifest(d)
    moves = folder_moves(a, m)
    old_wd = (m.get("compose") or {}).get("working_dir")
    apply_moves(m, moves)
    fix_compose_hash(m, d, moves, old_wd)
    existing = {c["Name"].lstrip("/") for c in all_containers()}
    clash = [c["name"] for c in m["containers"] if c["name"] in existing]
    existing_vols = set(run(["docker", "volume", "ls", "-q"]).split())
    vclash = [v["name"] for v in m["volumes"] if v["name"] in existing_vols]
    print("Restore %s from %s" % (m["project"], d))
    print("  %d image(s), %d volume(s), %d container(s)"
          % (len(m["images"]), len(m["volumes"]), len(m["containers"])))
    snaps = [c["name"] for c in m["containers"] if c["snapshot"]]
    if snaps:
        print("  from snapshots (files changed inside): %s" % ", ".join(snaps))
    for o, n in moves:
        print("  folder %s -> %s" % (o, n))
    putback = getattr(a, "restore_files", False)
    wd_now = host_path((m.get("compose") or {}).get("working_dir") or "")
    if putback and wd_now and not os.path.isdir(wd_now):
        die("the project folder %s is gone, so files can't be put back into it. If the project moved, choose its new "
            "folder with --project-folder." % wd_now)
    for c in m["containers"]:
        for b in c["bind_mounts"]:
            if not bind_needed(b):
                continue
            if bind_absent(b) and b.get("copy") and putback:
                print("  %s: %s is missing; it will be put back from the archive" % (c["name"], b["source"]))
            elif os.path.exists(b["source"]) and bind_absent(b):
                print("  WARNING %s: %s is an empty folder, but it was %s when archived (Docker creates missing "
                      "bind sources as empty folders); it will not be started. Add --restore-files to put it back."
                      % (c["name"], b["source"], "a file" if b.get("kind") == "file" else "a folder with files"))
            elif not os.path.exists(b["source"]):
                print("  WARNING %s: bind mount source %s is missing; it will not be started%s"
                      % (c["name"], b["source"], " (a copy is in the archive: add --restore-files)" if b.get("copy") else ""))
    if clash:
        die("containers already exist: %s. Remove or rename them first." % ", ".join(clash))
    if vclash:
        die("volumes already exist: %s. Refusing to overwrite them." % ", ".join(vclash))
    confirm_plan(a.yes, "restore")

    root = dest_root_of(d)
    created = {"containers": [], "volumes": [], "networks": []}
    put = []
    try:
        if putback:
            put_back_files(m, d, put)
        res = restore_steps(d, m, a.start, created, a.keep_restart_policy)
    except (Abort, KeyboardInterrupt) as e:
        rollback(created)
        remove_put_back(put)
        log_history(root, "restore", m["project"], d, result="failed: %s" % e)
        raise
    except Exception as e:
        rollback(created)
        remove_put_back(put)
        log_history(root, "restore", m["project"], d, result="failed: %r" % e)
        die("unexpected error during restore: %r" % e)
    _print_paused(res["paused"])
    return finish_restore(a, d, m, root, res)


def finish_restore(a, d, m, root, res):
    """Delete the backup only after a started, healthy, verified restore."""
    if not a.start:
        log_history(root, "restore", m["project"], d, started=False)
        print("\nBackup kept (containers were not started). Delete it later by restoring with --start,")
        print("or remove the folder yourself once you are happy.")
        return "not started"
    if res["failed"]:
        log_history(root, "restore", m["project"], d, result="started with problems",
                    problems=[n for n, _ in res["failed"]])
        print("\nBackup KEPT because not every container started.")
        return "problems"
    print("\nChecking the containers stay up (%ds)..." % a.check_seconds)
    bad = wait_healthy(res["started"], a.check_seconds)
    if bad:
        log_history(root, "restore", m["project"], d, result="unhealthy", problems=bad)
        print("Backup KEPT: %s" % "; ".join(bad))
        return "unhealthy"
    print("All containers running and healthy.")
    if a.keep_backup:
        log_history(root, "restore", m["project"], d, kept_backup=True)
        print("Backup kept (--keep-backup).")
        return "ok"
    if EXTRA_PROBLEM:
        log_history(root, "restore", m["project"], d, kept_backup=True, problems=EXTRA_PROBLEM)
        print("Backup KEPT: the extra images (base images) did not come back: %s" % "; ".join(EXTRA_PROBLEM))
        return "ok"
    size = delete_archive(d, "restored and running")
    log_history(root, "restore", m["project"], d, deleted_backup=True, size=size)
    return "ok"


def restore_steps(d, m, start, created, keep_restart=False):
    # tags that already exist locally and point elsewhere: keep them pointing where they are
    before = {}
    for i in m["images"]:
        for t in set([i["tag"]] + i.get("all_tags", [])):
            cur = image_id(t)
            if cur and cur != i["id"]:
                before[t] = cur
    keep = {}
    for n, (t, cur) in enumerate(sorted(before.items())):
        keep[t] = "docker-archive/keep:%d" % n
        run(["docker", "tag", cur, keep[t]])   # holds the user's image while load moves the name
    try:
        print("loading images...")
        run(["docker", "load", "-i", os.path.join(d, "images.tar")], capture=False)
        for f in sorted({i["file"] for i in m["images"] if i.get("file")}):
            print("loading %s..." % f)
            run(["docker", "load", "-i", os.path.join(d, f)], capture=False)
        for i in m["images"]:
            if image_id(i["tag"]) != i["id"]:
                if i.get("base"):
                    EXTRA_PROBLEM.append("base image %s did not load correctly" % i["tag"])
                    print("  WARNING: base image %s did not load correctly; continuing without it" % i["tag"])
                    continue
                die("image %s did not load correctly" % i["tag"])
            for t in i.get("all_tags", []):
                if t not in before and image_id(t) is None:
                    run(["docker", "tag", i["id"], t])
    finally:
        for t, cur in before.items():
            run(["docker", "tag", cur, t], check=False)   # put the user's newer image back on its name
            run(["docker", "rmi", "--no-prune", keep[t]], check=False)
    restore_extra_images(d)
    for c in m["containers"]:
        if c.get("retag") and image_id(c["retag"]) is None:
            run(["docker", "tag", c["image_override"], c["retag"]])

    ensure_helper(m.get("helper"))
    for v in m["volumes"]:
        vin = v["inspect"]
        cmd = ["docker", "volume", "create", "--driver", vin.get("Driver", "local")]
        for k, val in (vin.get("Labels") or {}).items():
            cmd += ["--label", "%s=%s" % (k, val)]
        for k, val in (vin.get("Options") or {}).items():
            cmd += ["--opt", "%s=%s" % (k, val)]
        run(cmd + [v["name"]])
        created["volumes"].append(v["name"])
        print("restoring volume %s..." % v["name"])
        with open(os.path.join(d, v["file"]), "rb") as f:
            run(["docker", "run", "--rm", "-i", "--network", "none", "-v", "%s:/data" % v["name"],
                 HELPER_IMAGE, "tar", "--numeric-owner", "-xzpf", "-", "-C", "/data"], stdin=f)
        n = check_volume_matches(v["name"], os.path.join(d, v["file"]))
        print("  checked: %d files match the backup" % n)

    for n in m.get("networks") or []:
        recreate_network(n, created)
    id_to_name = {c["id"]: c["name"] for c in m["containers"]}
    paused = []
    img_by_tag = {i["tag"]: i["id"] for i in m["images"]}
    for c in m["containers"]:
        if c.get("rootfs"):
            tag = archive_repo(m["project"], c["name"]) + ":rootfs"
            print("importing filesystem of %s..." % c["name"])
            iid = run(["docker", "import"] + (["--platform", c["platform"]] if c.get("platform") else [])
                      + [os.path.join(d, c["rootfs"]), tag]).strip()
            img_by_tag[tag] = image_id(tag)
            c["snapshot"] = tag
    for c in create_order(m["containers"]):
        ref = c["snapshot"] or c["image_override"] or c["saved_tag"]
        want = img_by_tag.get(ref)
        if want and image_id(ref) != want:
            ref = want   # the name now points at another image: use the exact ID
        create_exact(c, ref, id_to_name, keep_restart, paused)
        created["containers"].append(c["name"])
        print("created %s" % c["name"])

    failed = []
    if start:
        for c in start_order(m["containers"]):
            missing = [b["source"] for b in c["bind_mounts"] if bind_problem(b)]
            if missing:
                failed.append((c["name"], "bind mount source missing: " + ", ".join(missing)))
                print("  skipped starting %s (bind mount source missing)" % c["name"])
                continue
            r = subprocess.run(["docker", "start", c["name"]], stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE)
            if r.returncode != 0:
                failed.append((c["name"], r.stderr.decode(errors="replace").strip()))
                print("  could not start %s" % c["name"])
            else:
                print("started %s" % c["name"])
    try:
        with open(os.path.join(d, "RESTORED"), "a") as f:
            f.write(now().isoformat() + "\n")
    except OSError as e:
        print("  note: could not write RESTORED marker (%s). The restore itself worked." % e)
    print("\nRestored %s.%s" % (m["project"], "" if start else " Containers are created but not started."))
    for n, e in failed:
        print("  %s did not start: %s" % (n, e))
    started = [c["name"] for c in m["containers"]
               if start and c["name"] not in {n for n, _ in failed}]
    return {"failed": failed, "started": started, "paused": paused}


def _print_paused(paused):
    if paused:
        print("\nAuto-start is OFF for these restored containers (default). Their original setting:")
        for n, pol in paused:
            print("  %-40s %s" % (n, pol))
        print("To switch it back on later:")
        for n, pol in paused:
            print("  docker update --restart %s %s" % (pol, n))
        print("Or restore with --keep-restart-policy to keep the original setting.")


# ---------------------------------------------------------------- content check, history, retention

CONTENT_SH = ("set -o pipefail; cd /data && find . -type f -print0 | xargs -0 -r sha256sum "
              "| LC_ALL=C sort -k2")


def volume_content(vol):
    """{path: sha256} of every regular file in a volume."""
    out = run(["docker", "run", "--rm", "--network", "none", "-v", "%s:/data:ro" % vol,
               ensure_helper(), "sh", "-c", CONTENT_SH])
    res = {}
    for line in out.splitlines():
        if line.strip():
            digest, path = line.split("  ", 1)
            res[path] = digest
    return res


def archive_content(tgz):
    """{path: sha256} of every regular file inside a volume .tgz (hardlinks resolved)."""
    res = {}
    with tarfile.open(tgz, "r:gz") as t:
        for mem in t:
            name = mem.name if mem.name.startswith("./") else "./" + mem.name.lstrip("/")
            if mem.isfile():
                h = hashlib.sha256()
                f = t.extractfile(mem)
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
                res[name] = h.hexdigest()
            elif mem.islnk():
                target = mem.linkname if mem.linkname.startswith("./") else "./" + mem.linkname.lstrip("/")
                if target in res:
                    res[name] = res[target]
    return res


def check_volume_matches(vol, tgz):
    want, got = archive_content(tgz), volume_content(vol)
    if want != got:
        missing = sorted(set(want) - set(got))[:5]
        extra = sorted(set(got) - set(want))[:5]
        changed = sorted(k for k in set(want) & set(got) if want[k] != got[k])[:5]
        die("restored volume %s does not match the backup (missing %s, extra %s, changed %s)"
            % (vol, missing, extra, changed))
    return len(want)


def dest_root_of(archive_dir):
    return os.path.dirname(os.path.dirname(os.path.abspath(archive_dir)))


LOGGED_CMDS = {"backup", "remove", "restore", "delete-backup", "auto", "schedule", "settings", "add-images"}
RUN_LOG = []   # results logged during this run, so main() can add a record for failures


def archive_project(d):
    try:
        with open(os.path.join(d, "manifest.json")) as f:
            return json.load(f)["project"]
    except (OSError, ValueError, KeyError):
        return os.path.basename(os.path.dirname(d.rstrip("/")))


def log_history(root, action, project, archive=None, result="ok", **extra):
    RUN_LOG.append(result)
    extra = {k: v for k, v in extra.items() if v is not None}
    rec = {"time": now().isoformat(), "action": action, "project": project,
           "archive": os.path.basename(archive) if archive else None,
           "result": result, "host": os.uname().nodename}
    rec.update(extra)
    try:
        path = os.path.join(root, "history.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        print("  note: could not write history (%s)" % e)


def load_settings(root):
    try:
        with open(os.path.join(root, "settings.json")) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {}
    auto = st.get("auto") or {}
    if "remove_after_backup" in auto:   # old format: auto.enabled = idle backups that only removed if this was on
        if auto.get("enabled") and not auto["remove_after_backup"]:
            auto["enabled"] = False
            st.setdefault("backups", {})["enabled"] = True
        auto.pop("remove_after_backup")
    return st


def max_backups_for(root, project):
    st = load_settings(root)
    n = (st.get("projects") or {}).get(project, st.get("default_max_backups", 3))
    return max(1, int(n))


def dir_size(d):
    return sum(os.path.getsize(os.path.join(r, x)) for r, _, fs in os.walk(d) for x in fs)


def delete_archive(d, reason):
    """Delete one archive folder. Only folders that look like our archives."""
    d = os.path.abspath(d)
    if not (os.path.isfile(os.path.join(d, "manifest.json")) and
            os.path.isfile(os.path.join(d, "checksums.sha256"))):
        die("refusing to delete %s: it does not look like a docker-archive backup" % d)
    size = dir_size(d)
    trash = d + ".deleting"
    os.rename(d, trash)
    shutil.rmtree(trash)
    fsync_path(os.path.dirname(d))
    print("  deleted backup %s (%s): %s" % (os.path.basename(d), human(size), reason))
    return size


def apply_retention(root, project):
    """Keep the newest N complete backups of a project. Never delete the copy of a removed project."""
    pdir = os.path.join(root, safe_name(project))
    if not os.path.isdir(pdir):
        return
    keep_n = max_backups_for(root, project)

    def archived(x):
        d = os.path.join(pdir, x)
        return (os.path.exists(os.path.join(d, "REMOVED")) or os.path.exists(os.path.join(d, "REMOVING"))) \
            and not os.path.exists(os.path.join(d, "RESTORED"))
    # Only backups count. An archive (project removed from Docker) is never trimmed.
    full = sorted(x for x in os.listdir(pdir)
                  if not x.endswith((".partial", ".deleting"))
                  and os.path.isfile(os.path.join(pdir, x, "COMPLETE")) and not archived(x))
    old = full[:-keep_n] if len(full) > keep_n else []
    for x in old:
        d = os.path.join(pdir, x)
        size = delete_archive(d, "more than %d backups kept for %s" % (keep_n, project))
        log_history(root, "delete-backup", project, d, reason="max_backups=%d" % keep_n, size=size)


def cmd_delete_backup(a):
    """Delete one backup folder. Extra confirmation when the project is not in Docker."""
    d = os.path.abspath(a.archive)
    try:
        with open(os.path.join(d, "manifest.json")) as f:
            project = json.load(f)["project"]
    except (OSError, ValueError, KeyError):
        die("%s is not a docker-archive backup" % d)
    in_docker = project in group_projects(all_containers())
    size = dir_size(d)
    print("Backup:  %s (%s)" % (d, human(size)))
    if in_docker:
        print("Project %s is still in Docker, so this backup is not its only copy." % project)
    else:
        print("WARNING: project %s is NOT in Docker. This backup may be the only copy of its data." % project)
        if not a.only_copy:
            die("refusing: add --only-copy to confirm deleting what may be the only copy")
    confirm_plan(a.yes, "delete this backup permanently")
    delete_archive(d, "deleted by user")
    log_history(dest_root_of(d), "delete-backup", project, d, size=size,
                reason="user", only_copy=not in_docker)


def cmd_settings(a):
    root, _ = check_dest(a.dest, a.allow_internal_dest, create=True)
    st = load_settings(root)
    before = json.dumps(st, sort_keys=True)
    auto = dict(AUTO_DEFAULTS, **(st.get("auto") or {}))
    auto.pop("remove_after_backup", None)   # archiving always removes now
    bk = dict(BACKUP_DEFAULTS, **(st.get("backups") or {}))
    if a.auto_enable or a.auto_disable:
        auto["enabled"] = bool(a.auto_enable)
    if a.auto_weeks is not None:
        auto["weeks"] = max(1, a.auto_weeks)
    if a.min_battery is not None:
        auto["min_battery"] = min(100, max(0, a.min_battery))
    if a.backup_enable or a.backup_disable:
        bk["enabled"] = bool(a.backup_enable)
    if a.backup_days is not None:
        bk["every_days"] = max(1, a.backup_days)
    if a.backup_running is not None:
        bk["running"] = a.backup_running
    if a.max_backups is not None:
        if a.max_backups < 1:
            die("--max-backups must be at least 1")
        if a.project:
            st.setdefault("projects", {})[a.project] = a.max_backups
        else:
            st["default_max_backups"] = a.max_backups
    st["auto"], st["backups"] = auto, bk
    with open(os.path.join(root, "settings.json"), "w") as f:
        json.dump(st, f, indent=2)
    old = json.loads(before)
    changes = []
    for sec in ("auto", "backups"):
        for k, v in st[sec].items():
            if (old.get(sec) or {}).get(k, (AUTO_DEFAULTS if sec == "auto" else BACKUP_DEFAULTS).get(k)) != v:
                changes.append("%s %s=%s" % ("archiving" if sec == "auto" else "backups", k, v))
    if st.get("default_max_backups", 3) != old.get("default_max_backups", 3):
        changes.append("backups keep=%s" % st["default_max_backups"])
    if st.get("projects") != old.get("projects"):
        changes.append("per-project keep=%s" % st.get("projects"))
    if changes:
        log_history(root, "settings", a.project or "-", changes=", ".join(changes))
    print(json.dumps({"default_max_backups": st.get("default_max_backups", 3),
                      "projects": st.get("projects", {}), "auto": auto, "backups": bk}, indent=2))


def wait_healthy(names, seconds):
    """After start: running (or exited 0) and healthy where a healthcheck exists."""
    import time
    deadline = time.time() + seconds + 90
    time.sleep(seconds)
    while True:
        bad = []
        for n in names:
            st = docker_json(["inspect", n])[0]["State"]
            health = (st.get("Health") or {}).get("Status")
            if st.get("Running"):
                if health in (None, "healthy"):
                    continue
                bad.append("%s is %s" % (n, health))
            elif st.get("ExitCode") == 0 and not st.get("Restarting"):
                continue   # one-shot container that finished cleanly
            else:
                bad.append("%s stopped (exit %s)" % (n, st.get("ExitCode")))
        if not bad or time.time() > deadline or not any("starting" in b for b in bad):
            return bad
        time.sleep(5)


# ---------------------------------------------------------------- json, sizes, automation

def df_verbose():
    out = run(["docker", "system", "df", "-v", "--format", "{{json .}}"])
    try:
        return json.loads(out)
    except ValueError:
        return {}


def status_data(dest):
    """Everything the UI needs: projects, containers, sizes, archive state."""
    cs = all_containers()
    df = df_verbose()
    vsize = {v.get("Name"): parse_size(v.get("Size")) for v in df.get("Volumes") or []}
    csize = {c.get("ID"): parse_size(c.get("Size")) for c in df.get("Containers") or []}
    iuniq = {i.get("ID"): parse_size(i.get("UniqueSize")) for i in df.get("Images") or []}
    isize = {i.get("ID"): parse_size(i.get("Size")) for i in df.get("Images") or []}
    projects = group_projects(cs)
    archived = {}
    if os.path.isdir(dest):
        for proj in os.listdir(dest):
            pdir = os.path.join(dest, proj)
            if os.path.isdir(pdir):
                full = sorted(x for x in os.listdir(pdir) if os.path.isfile(os.path.join(pdir, x, "COMPLETE"))
                              and not x.endswith((".partial", ".deleting")))
                if full:
                    archived[proj] = full
    rows = []
    for name, pcs in projects.items():
        ids = {c["Id"] for c in pcs}
        others = [c for c in cs if c["Id"] not in ids]
        other_vols = {v for c in others for v in volumes_of(c)}
        other_imgs = {c["Image"] for c in others}
        lasts = [t for t in (last_active(c) for c in pcs) if t]
        last = max(lasts) if lasts else None
        ctrs, frees, seen_v, seen_i = [], 0, set(), set()
        for c in pcs:
            vols = volumes_of(c)
            rw = csize.get(c["Id"], 0)
            ctrs.append({
                "name": c["Name"].lstrip("/"), "state": c["State"].get("Status"),
                "image": c["Config"].get("Image"), "image_size": isize.get(c["Image"], 0),
                "changes_size": rw,
                "volumes": [{"name": v, "size": vsize.get(v, 0), "shared": v in other_vols} for v in vols],
                "last_active": last_active(c).isoformat() if last_active(c) else None,
            })
            frees += rw
            for v in vols:
                if v not in other_vols and v not in seen_v:
                    frees += vsize.get(v, 0); seen_v.add(v)
            if c["Image"] not in other_imgs and c["Image"] not in seen_i:
                frees += iuniq.get(c["Image"], 0); seen_i.add(c["Image"])
        rows.append({
            "project": name, "containers": ctrs,
            "running": sum(1 for c in pcs if c["State"].get("Running")),
            "last_active": last.isoformat() if last else None,
            "idle_days": (now() - last).days if last else None,
            "frees_if_removed": frees,
            "backups": archived.get(safe_name(name), []),
        })
    rows.sort(key=lambda r: -(r["idle_days"] or 0))
    return {"projects": rows, "dest": dest, "dest_mounted": dest_available(dest),
            "battery": battery_state()}


def archives_data(dest):
    res = []
    if not os.path.isdir(dest):
        return res
    for proj in sorted(os.listdir(dest)):
        pdir = os.path.join(dest, proj)
        if not os.path.isdir(pdir):
            continue
        for st in sorted(os.listdir(pdir)):
            d = os.path.join(pdir, st)
            if not os.path.isdir(d):
                continue
            flags = [f for f in ("COMPLETE", "REMOVING", "REMOVED", "RESTORED") if os.path.exists(os.path.join(d, f))]
            row = {"project": proj, "archive": st, "path": d, "size": dir_size(d), "flags": flags,
                   "partial": st.endswith(".partial")}
            try:
                m = json.load(open(os.path.join(d, "manifest.json")))
                wd = host_path((m.get("compose") or {}).get("working_dir") or "")
                binds = {b["source"]: b for c in m["containers"] for b in c["bind_mounts"]}
                row.update(folder=wd or None, folder_exists=bool(wd) and os.path.isdir(wd),
                           binds=len(binds), binds_missing=sum(bind_problem(b) for b in binds.values()),
                           binds_copied=sum(bool(b.get("copy")) for b in binds.values()))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            res.append(row)
    return res


def dest_available(dest):
    if dest.startswith("/Volumes/"):
        return os.path.ismount("/Volumes/" + dest.split("/")[2])
    return os.path.isdir(dest)


def battery_state():
    """{'percent': int|None, 'on_ac': bool} from pmset. Desktops report on_ac=True."""
    out = run(["pmset", "-g", "batt"], check=False) or ""
    pct = re.search(r"(\d+)%", out)
    return {"percent": int(pct.group(1)) if pct else None,
            "on_ac": "AC Power" in out or not pct,
            "charging": bool(re.search(r"; (charging|charged|finishing charge)", out))}


def cmd_json(a):
    if a.what == "status":
        data = status_data(a.dest)
    elif a.what == "archives":
        data = archives_data(a.dest)
    elif a.what == "settings":
        data = load_settings(a.dest) if os.path.isdir(a.dest) else {}
    else:
        path = os.path.join(a.dest, "history.jsonl")
        data = [json.loads(l) for l in open(path)] if os.path.isfile(path) else []
    print(json.dumps(data, default=str))


AUTO_DEFAULTS = {"enabled": False, "weeks": 8, "min_battery": 50}            # automatic archiving
BACKUP_DEFAULTS = {"enabled": False, "every_days": 7, "running": "skip"}       # automatic backups


def complete_copies(root, name):
    """Names of complete copies of a project, oldest first."""
    pdir = os.path.join(root, safe_name(name))
    if not os.path.isdir(pdir):
        return []
    return sorted(x for x in os.listdir(pdir) if re.match(r"^\d{8}-\d{6}$", x)
                  and os.path.isfile(os.path.join(pdir, x, "COMPLETE")))


def copy_time(x):
    return dt.datetime.strptime(x[:15], "%Y%m%d-%H%M%S").replace(tzinfo=dt.timezone.utc)


def cmd_auto(a):
    """Daily check. Archive (copy + remove) projects idle X weeks; back up the others every N days."""
    if not dest_available(a.dest):
        print("auto: drive not connected, nothing to do")
        return
    root = a.dest
    st = load_settings(root)
    arch = dict(AUTO_DEFAULTS, **(st.get("auto") or {}))
    bk = dict(BACKUP_DEFAULTS, **(st.get("backups") or {}))
    if not arch["enabled"] and not bk["enabled"]:
        print("auto: automatic backups and archiving are both off")
        return
    bat = battery_state()
    if not a.force and not bat["on_ac"] and (bat["percent"] or 0) < arch["min_battery"]:
        msg = "battery %s%% on battery power, below %s%%" % (bat["percent"], arch["min_battery"])
        print("auto: skipped, " + msg)
        log_history(root, "auto", "-", result="skipped: " + msg)
        return
    lock = open("/tmp/docker-archive-auto.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("auto: another run is busy")
        return
    projects = group_projects(all_containers())
    if a.only:   # test hook: limit the run to these projects
        projects = {k: v for k, v in projects.items() if k in a.only}
    archived = set()

    # ---- 1. automatic archiving: idle >= weeks, stopped -> copy, verify, remove from Docker
    if arch["enabled"]:
        fresh = []
        for name, pcs in sorted(projects.items()):
            if any(c["State"].get("Running") for c in pcs):
                continue
            lasts = [t for t in (last_active(c) for c in pcs) if t]
            if not lasts or (now() - max(lasts)).days < arch["weeks"] * 7:
                continue
            copies = complete_copies(root, name)
            if copies and copy_time(copies[-1]) > max(lasts):
                d = os.path.join(root, safe_name(name), copies[-1])
                print("\nauto: archiving %s using its backup from %s (unchanged since)" % (name, copies[-1]))
                try:
                    cmd_remove(argparse.Namespace(archive=d, yes=True))
                    archived.add(name)
                    continue
                except Abort as e:
                    print("auto: that backup can't be used (%s), making a fresh archive" % e)
            fresh.append(name)
        if fresh:
            print("auto: archiving %s (idle >= %d weeks)" % (", ".join(fresh), arch["weeks"]))
            ns = argparse.Namespace(**vars(a))
            ns.projects, ns.yes, ns.months, ns.remove_after, ns.pause_running, ns.auto_run = \
                fresh, True, 0, True, False, "archive"
            for d in cmd_backup(ns) or []:
                archived.add(os.path.basename(os.path.dirname(d)))
        else:
            print("auto: no project idle >= %d weeks to archive" % arch["weeks"])

    # ---- 2. automatic backups: every N days, copy only
    if bk["enabled"]:
        todo = []
        for name, pcs in sorted(projects.items()):
            if safe_name(name) in archived or name in archived:
                continue
            running = any(c["State"].get("Running") for c in pcs)
            if running and bk["running"] != "pause":
                print("auto: skip %s: running (setting: skip running projects)" % name)
                continue
            copies = complete_copies(root, name)
            if copies and (now() - copy_time(copies[-1])).total_seconds() < bk["every_days"] * 86400 - 3 * 3600:
                continue   # not due yet
            lasts = [t for t in (last_active(c) for c in pcs) if t]
            if not running and copies and lasts and copy_time(copies[-1]) > max(lasts):
                print("auto: skip %s: unchanged since its last backup" % name)
                continue
            todo.append(name)
        if todo:
            print("auto: backing up %s (every %d days)" % (", ".join(todo), bk["every_days"]))
            ns = argparse.Namespace(**vars(a))
            ns.projects, ns.yes, ns.months, ns.remove_after, ns.auto_run = todo, True, 0, False, "backup"
            ns.pause_running = bk["running"] == "pause"
            cmd_backup(ns)
        else:
            print("auto: no backups due")


PLIST = os.path.expanduser("~/Library/LaunchAgents/com.docker-archive.auto.plist")


def cmd_schedule(a):
    if a.remove:
        if os.path.exists(PLIST):
            run(["launchctl", "unload", PLIST], check=False)
            os.remove(PLIST)
        log_history(a.dest, "schedule", "-", result="removed")
        print("Daily check removed.")
        return
    if a.hour is None:
        if not os.path.exists(PLIST):
            print("installed: no")
        else:
            try:
                with open(PLIST, "rb") as f:
                    hour = plistlib.load(f)["StartCalendarInterval"]["Hour"]
                print("installed: yes, hour %d, %s" % (hour, PLIST))
            except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
                print("installed: yes, %s" % PLIST)
        return
    script = os.path.abspath(__file__)
    log = os.path.expanduser("~/Library/Logs/docker-archive.log")
    plist = {
        "Label": "com.docker-archive.auto",
        "ProgramArguments": ["/usr/bin/python3", script, "--dest", a.dest, "auto"],
        "StartCalendarInterval": {"Hour": a.hour, "Minute": 0},
        "StandardOutPath": log, "StandardErrorPath": log,
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
    }
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    if os.path.exists(PLIST):
        run(["launchctl", "unload", PLIST], check=False)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    run(["launchctl", "load", PLIST])
    log_history(a.dest, "schedule", "-", hour=a.hour)
    print("Daily check at %02d:00 installed. It runs automatic backups and archiving (whichever are on),"
          " only when the drive is connected, and only on power or above the battery limit. Log: %s" % (a.hour, log))


# ---------------------------------------------------------------- archives

def cmd_history(a):
    path = os.path.join(a.dest, "history.jsonl")
    if not os.path.isfile(path):
        die("no history yet at %s" % path)
    with open(path) as f:
        lines = f.readlines()[-a.n:]
    for line in lines:
        r = json.loads(line)
        extra = ", ".join("%s=%s" % (k, human(v) if k == "size" else v) for k, v in r.items()
                          if k not in ("time", "action", "project", "archive", "result", "host"))
        print("%s  %-14s %-24s %-16s %s%s" % (r["time"][:19].replace("T", " "), r["action"], r["project"][:24],
                                             r.get("archive") or "", r["result"], ("  " + extra) if extra else ""))


def cmd_archives(a):
    if not os.path.isdir(a.dest):
        die("%s not found (is the drive plugged in?)" % a.dest)
    for proj in sorted(os.listdir(a.dest)):
        pdir = os.path.join(a.dest, proj)
        if not os.path.isdir(pdir):
            continue
        for st in sorted(os.listdir(pdir)):
            d = os.path.join(pdir, st)
            if not os.path.isdir(d):
                continue
            size = sum(os.path.getsize(os.path.join(r, x)) for r, _, fs in os.walk(d) for x in fs)
            flags = [f for f in ("COMPLETE", "REMOVING", "REMOVED", "RESTORED") if os.path.exists(os.path.join(d, f))]
            if st.endswith(".partial"):
                flags = ["PARTIAL but COMPLETE inside: rename to drop .partial"] \
                    if os.path.exists(os.path.join(d, "COMPLETE")) else \
                    ["PARTIAL (failed or interrupted, safe to delete)"]
            print("%-60s %9s  %s" % (d, human(size), " ".join(flags)))


# ---------------------------------------------------------------- bind mounts: small copies, moved folders

BIND_FILE_MAX = 20 * 1000 ** 2      # copy bind-mounted files up to this size
BIND_DIR_MAX = 50 * 1000 ** 2       # and folders up to this total size
BIND_DIR_FILES = 5000               # and this many files


def measure_bind(src):
    """('file'|'dir', size) when small enough to keep a copy in the archive, else (None, reason)."""
    import stat
    try:
        st = os.stat(src)
    except OSError:
        return None, "missing"
    if stat.S_ISREG(st.st_mode):
        return ("file", st.st_size) if st.st_size <= BIND_FILE_MAX else (None, "too big (%s)" % human(st.st_size))
    if not stat.S_ISDIR(st.st_mode):
        return None, "not a file or folder"
    total, n = 0, 0
    for r, _, fs in os.walk(src):
        for f in fs:
            try:
                s2 = os.lstat(os.path.join(r, f))
            except OSError:
                continue
            n += 1
            total += s2.st_size if stat.S_ISREG(s2.st_mode) else 0
            if total > BIND_DIR_MAX or n > BIND_DIR_FILES:
                return None, "too big (over %s or %d files)" % (human(BIND_DIR_MAX), BIND_DIR_FILES)
    return "dir", total


def copy_bind(src, dst):
    """Copy a bind-mounted file or folder (regular files, folders and symlinks only)."""
    if os.path.isdir(src):
        def skip(folder, names):   # symlinks, sockets, devices: not copied
            return [x for x in names if os.path.islink(os.path.join(folder, x)) or not
                    (os.path.isdir(os.path.join(folder, x)) or os.path.isfile(os.path.join(folder, x)))]
        shutil.copytree(src, dst, ignore=skip)
    else:
        shutil.copy2(src, dst)


def save_bind_copies(manifest, tmp):
    """Keep copies of small bind-mounted files/folders (config, secrets) so a moved or deleted repo can be refilled."""
    copies = {}
    for c in manifest["containers"]:
        for b in c["bind_mounts"]:
            src = b["source"]
            if src not in copies:
                kind, info = measure_bind(src)
                if kind:
                    rel = "binds/%03d-%s" % (len([x for x in copies.values() if "copy" in x]) + 1,
                                             safe_name(os.path.basename(src.rstrip("/"))) or "root")
                    os.makedirs(os.path.join(tmp, "binds"), exist_ok=True)
                    try:
                        copy_bind(src, os.path.join(tmp, rel))
                        copies[src] = {"copy": rel, "kind": kind, "copy_size": info}
                    except (OSError, shutil.Error) as e:
                        p = os.path.join(tmp, rel)
                        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else \
                            (os.path.exists(p) and os.remove(p))
                        copies[src] = {"not_copied": "could not read it (%s)" % (getattr(e, "strerror", None) or e)}
                else:
                    copies[src] = {"not_copied": info}
            b.update(copies[src])
    done = [s for s, x in copies.items() if "copy" in x]
    if done:
        print("  kept a copy of %d bind-mounted file(s)/folder(s) (%s)"
              % (len(done), human(sum(copies[s]["copy_size"] for s in done))))
    for s, x in copies.items():
        if "not_copied" in x and x["not_copied"] not in ("missing", "not a file or folder"):
            print("  bind mount %s not copied: %s" % (s, x["not_copied"]))


def folder_moves(a, m):
    """[(old, new)] folder mappings for restore: --project-folder and --move-folder OLD NEW."""
    moves = [tuple(x) for x in (getattr(a, "move_folder", None) or [])]
    pf = getattr(a, "project_folder", None)
    if pf:
        old = (m.get("compose") or {}).get("working_dir")
        if not old:
            die("this archive has no project folder recorded. Use --move-folder OLD NEW instead.")
        moves.insert(0, (host_path(old), pf))
    out = []
    for o, n in moves:
        n = os.path.abspath(os.path.expanduser(n))
        if not os.path.isdir(n):
            die("folder not found: %s" % n)
        out.append((os.path.abspath(os.path.expanduser(host_path(o))).rstrip("/"), n.rstrip("/") or "/"))
    return sorted(out, key=lambda x: -len(x[0]))   # most specific folder first


def moved(path, moves):
    hp = host_path(path or "")
    pre = "/host_mnt" if (path or "").startswith("/host_mnt/") else ""
    for o, n in moves:
        if hp == o or hp.startswith(o + "/"):
            return pre + n + hp[len(o):]
    return path


def apply_moves(m, moves):
    """Point bind mounts and compose labels at the folders the user chose."""
    if not moves:
        return
    for c in m["containers"]:
        for b in c["bind_mounts"]:
            b["source"] = moved(b["source"], moves)
        ins = c["inspect"]
        hc = ins["HostConfig"]
        binds = []
        for x in hc.get("Binds") or []:
            src, sep, rest = x.partition(":")
            binds.append(moved(src, moves) + sep + rest if sep and src.startswith("/") else x)
        if hc.get("Binds"):
            hc["Binds"] = binds
        for mnt in (hc.get("Mounts") or []) + (ins.get("Mounts") or []):
            if mnt.get("Type") == "bind" and mnt.get("Source"):
                mnt["Source"] = moved(mnt["Source"], moves)
        labels = ins["Config"].get("Labels") or {}
        if labels.get("com.docker.compose.project.working_dir"):
            labels["com.docker.compose.project.working_dir"] = moved(labels["com.docker.compose.project.working_dir"], moves)
        if labels.get("com.docker.compose.project.config_files"):
            labels["com.docker.compose.project.config_files"] = ",".join(
                moved(f, moves) for f in labels["com.docker.compose.project.config_files"].split(","))
    if m.get("compose") and m["compose"].get("working_dir"):
        m["compose"]["working_dir"] = moved(m["compose"]["working_dir"], moves)


def compose_hashes(project, wd, files, env_file=None, cwd=None):
    """{service: config hash} as docker compose computes it, or None if compose can't read the files."""
    cmd = ["docker", "compose", "-p", project, "--project-directory", wd]
    for f in files:
        cmd += ["-f", f]
    if env_file:
        cmd += ["--env-file", env_file]
    try:
        out = subprocess.run(cmd + ["config", "--hash", "*"], cwd=cwd or wd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode:
        return None
    h = dict(l.split(None, 1) for l in out.stdout.splitlines() if len(l.split()) == 2)
    return {k: v.strip() for k, v in h.items()} or None


def fix_compose_hash(m, d, moves, old_wd):
    """After a folder move, compose would see a new config hash and re-create the containers (losing changes made
    inside them). If the compose files in the new folder are the ones that were archived, store the new hash."""
    comp = m.get("compose") or {}
    if not moves or not comp.get("working_dir") or not comp.get("copied"):
        return
    wd = host_path(comp["working_dir"])
    copied = {x["source"]: x["file"] for x in comp["copied"]}
    if any(f not in copied for f in comp["config_files"]) or not os.path.isdir(wd):
        return
    proj = (m["containers"][0]["inspect"]["Config"].get("Labels") or {}).get("com.docker.compose.project")
    if not proj:
        return
    now = compose_hashes(proj, wd, [moved(f, moves) for f in comp["config_files"]])
    env = os.path.join(d, "compose", ".env") if ".env" in copied.values() else None
    arch = [os.path.join(d, "compose", copied[f]) for f in comp["config_files"]]
    then = compose_hashes(proj, wd, arch, env, cwd=d)
    orig = compose_hashes(proj, host_path(old_wd), arch, env, cwd=d) or {}   # what the containers were made from
    if not now or now != then:
        if comp.get("config_files"):
            print("  note: the compose files in %s differ from the archived ones; compose will re-create the containers" % wd)
        return
    for c in m["containers"]:
        labels = c["inspect"]["Config"].get("Labels") or {}
        svc = labels.get("com.docker.compose.service")
        # only when the container matched the archived compose file (no edit was waiting for a compose up)
        if svc in now and labels.get("com.docker.compose.config-hash") and \
                labels["com.docker.compose.config-hash"] == orig.get(svc):
            labels["com.docker.compose.config-hash"] = now[svc]


def bind_absent(b, path=None):
    """Missing, or an empty folder where the archive saved a file or a non-empty folder (Docker creates missing
    bind sources as empty folders). A symlink, even a broken one, counts as there: it is never written through."""
    p = path or b["source"]
    if not os.path.lexists(p):
        return True
    if os.path.islink(p) or not os.path.isdir(p) or os.listdir(p):
        return False
    return b.get("kind") == "file" or (b.get("kind") == "dir" and b.get("copy_size", 0) > 0)


def bind_needed(b):
    """False for a bind source that was already missing on the Mac at backup time (e.g. Docker VM-only paths)."""
    return b.get("not_copied") != "missing"


def bind_problem(b, path=None):
    return bind_needed(b) and bind_absent(b, path)


def put_back_files(m, d, done):
    """Copy saved bind files/folders back where the containers expect them, if missing. Never overwrites.
    Appends each path it creates (and each parent folder it makes) to done, before creating it, for rollback.
    Only fills in an existing folder: never rebuilds a project folder that is gone."""
    wd = host_path((m.get("compose") or {}).get("working_dir") or "")
    seen = set()
    for b in sorted((b for c in m["containers"] for b in c["bind_mounts"]),
                    key=lambda b: host_path(b["source"]).count("/")):   # outer folders before files inside them
        src = host_path(b["source"])
        if not b.get("copy") or src in seen or not bind_absent(b):
            continue
        seen.add(src)
        parent = os.path.dirname(src)
        if not os.path.isdir(parent):
            if not wd or not os.path.isdir(wd) or not parent.startswith(wd.rstrip("/") + "/"):
                print("  NOT put back %s: its folder %s does not exist" % (src, parent))
                continue
            missing, p = [], parent
            while not os.path.isdir(p):
                missing.append(p)
                p = os.path.dirname(p)
            for p in reversed(missing):
                done.append(("dir", p))
                os.mkdir(p)
        if os.path.isdir(src) and not os.path.islink(src):
            os.rmdir(src)   # the empty folder Docker made in its place
            done.append(("emptydir", src))
        done.append(("copy", src))
        copy_bind(os.path.join(d, b["copy"]), src)
        print("  put back %s" % src)
    return done


def remove_put_back(done):
    """Undo put_back_files: remove what it copied and the folders it made; re-make empty folders it replaced."""
    for kind, p in reversed(done):
        try:
            if kind == "copy" and os.path.lexists(p):
                shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
            elif kind == "dir":
                os.rmdir(p)
            elif kind == "emptydir" and not os.path.lexists(p):
                os.mkdir(p)
        except OSError:
            pass


# ---------------------------------------------------------------- extra images (base images and others no container runs)

EXTRA_LIST = "extra-images.json"
EXTRA_PROBLEM = []   # set during a restore when extra images could not be checked or loaded: keep the archive


def load_extra_list(d):
    p = os.path.join(d, EXTRA_LIST)
    if not os.path.isfile(p):
        return []
    with open(p) as f:
        return json.load(f)


def verify_extra(d):
    """(ok, msg) for the extra image files listed in the archive, if any."""
    try:
        extra = load_extra_list(d)
        if not isinstance(extra, list) or not all(isinstance(e, dict) and "file" in e and "tags" in e for e in extra):
            raise ValueError("unexpected contents")
    except (OSError, ValueError) as e:
        return False, "%s unreadable: %s" % (EXTRA_LIST, e)
    for e in extra:
        try:
            names = tar_image_names(os.path.join(d, e["file"]))
        except (tarfile.TarError, OSError, TypeError) as err:
            return False, "%s unreadable: %s" % (e["file"], err)
        missing = [t for t in e["tags"] if t not in names]
        if missing:
            return False, "%s has no entry named: %s" % (e["file"], ", ".join(missing))
    return True, "ok"


def load_tar_keeping_names(path, tags):
    """docker load, but leave any local image that already has one of these names on that name."""
    before = {t: image_id(t) for t in tags if image_id(t)}
    keep = {}
    for n, (t, cur) in enumerate(sorted(before.items())):
        keep[t] = "docker-archive/keep-extra:%d" % n
        run(["docker", "tag", cur, keep[t]])
    try:
        run(["docker", "load", "-i", path], capture=False)
    finally:
        for t, cur in before.items():
            run(["docker", "tag", cur, t], check=False)
            run(["docker", "rmi", "--no-prune", keep[t]], check=False)
    return before


def restore_extra_images(d):
    if EXTRA_PROBLEM:
        return
    try:
        extra = load_extra_list(d)
        if not isinstance(extra, list) or not all(isinstance(e, dict) and "file" in e and "tags" in e for e in extra):
            raise ValueError("unexpected contents of " + EXTRA_LIST)
    except (OSError, ValueError) as err:
        EXTRA_PROBLEM.append(str(err))
        return
    for e in extra:
        if all(image_id(t) for t in e["tags"]):
            print("extra images already in Docker, not loaded: %s" % ", ".join(e["tags"]))
            continue
        print("loading extra images: %s..." % ", ".join(e["tags"]))
        try:
            kept = load_tar_keeping_names(os.path.join(d, e["file"]), e["tags"])
        except Abort as err:
            EXTRA_PROBLEM.append("%s did not load: %s" % (e["file"], err))
            print("  WARNING: extra images did not load (%s). The project is restored without them." % err)
            continue
        for t in kept:
            print("  %s already existed in Docker and was left as it is" % t)


def image_users(img_id, layers, skip_ids):
    """Why an image can't be removed: containers using it, or other images built on top of it."""
    why = []
    for c in all_containers():
        if c.get("Image") == img_id:
            why.append("container %s uses it" % c["Name"].lstrip("/"))
    for line in run(["docker", "images", "-q", "--no-trunc"]).split():
        if line == img_id or line in skip_ids:
            continue
        other = docker_json(["image", "inspect", line])[0]
        ol = other.get("RootFS", {}).get("Layers") or []
        if len(ol) > len(layers) and ol[:len(layers)] == layers:
            why.append("image %s is built on it" % ((other.get("RepoTags") or [line[7:19]])[0]))
    return why


def _write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def cmd_add_images(a):
    """Add images no container runs (e.g. base images a project's images are built FROM) to an existing archive."""
    d = os.path.abspath(a.archive)
    good, msg = verify_dir(d, quiet=True)
    if not good:
        die(msg)
    m = load_manifest(d)
    root = dest_root_of(d)
    have = {i["tag"] for i in m["images"]} | {t for e in load_extra_list(d) for t in e["tags"]}
    imgs = []
    for ref in a.images:
        tag = ref if ":" in ref.split("/")[-1] else ref + ":latest"
        if tag in have:
            print("  %s is already in this archive, skipped" % tag)
            continue
        info = docker_json(["image", "inspect", tag])[0]
        imgs.append({"tag": tag, "id": info["Id"], "size": info.get("Size", 0),
                     "layers": info.get("RootFS", {}).get("Layers") or []})
    if not imgs:
        print("Nothing to add.")
        return
    print("Add to %s:" % d)
    for i in imgs:
        print("  %-45s %s" % (i["tag"], human(i["size"])))
    blocked = {}
    if a.remove:
        ids = {i["id"] for i in imgs}
        for i in imgs:
            why = image_users(i["id"], i["layers"], ids)
            if why:
                blocked[i["tag"]] = why
        print("Then remove them from Docker%s." % ("" if not blocked else ", except:"))
        for t, why in blocked.items():
            print("  keep %s: %s" % (t, "; ".join(why)))
    confirm_plan(a.yes, "add them")
    restore_lock(wait=False)

    n = 1
    while os.path.exists(os.path.join(d, "extra-images-%d.tar" % n)):
        n += 1
    fname = "extra-images-%d.tar" % n
    final, tmp = os.path.join(d, fname), os.path.join(d, fname + ".partial")
    snap = {}
    for name in (EXTRA_LIST, "checksums.sha256"):
        p = os.path.join(d, name)
        snap[name] = open(p, "rb").read() if os.path.exists(p) else None
    print("saving images (one copy of each shared layer)...")
    try:
        native = native_platform()
        plats = {save_platform(i["tag"], None, native) for i in imgs}
        if len(plats) > 1:
            die("these images are stored for different platforms (%s). Add them in separate runs."
                % ", ".join(sorted(p or native for p in plats)))
        plat = plats.pop()
        run(["docker", "save"] + (["--platform", plat] if plat else []) + ["-o", tmp] + [i["tag"] for i in imgs])
        if plat:
            got = tar_image_ids(tmp)
            for i in imgs:
                if i["tag"] not in got:
                    die("could not read the ID of %s back from the saved file" % i["tag"])
                i["id"] = got[i["tag"]]
        names = tar_image_names(tmp)
        bad = [i["tag"] for i in imgs if i["tag"] not in names]
        if bad:
            die("saved file has no entry for: %s" % ", ".join(bad))
        fsync_path(tmp)
        digest = sha256_file(tmp)
        os.replace(tmp, final)
        extra = load_extra_list(d) + [{"file": fname, "tags": [i["tag"] for i in imgs],
                                       "ids": [i["id"] for i in imgs], "added": now().isoformat()}]
        _write_atomic(os.path.join(d, EXTRA_LIST), (json.dumps(extra, indent=1) + "\n").encode())
        lines = [l for l in snap["checksums.sha256"].decode().splitlines() if l.strip() and l[66:] != EXTRA_LIST]
        lines += ["%s  %s" % (digest, fname), "%s  %s" % (sha256_file(os.path.join(d, EXTRA_LIST)), EXTRA_LIST)]
        lines.sort(key=lambda l: l[66:])
        _write_atomic(os.path.join(d, "checksums.sha256"), ("\n".join(lines) + "\n").encode())
        fsync_path(d)
        good, msg = verify_dir(d)
        if not good:
            die("the archive did not verify after adding the images: %s" % msg)
    except BaseException:
        # put the archive back exactly as it was
        for name, data in snap.items():
            p = os.path.join(d, name)
            if data is None:
                if os.path.exists(p):
                    os.remove(p)
            else:
                _write_atomic(p, data)
        for p in (tmp, final):
            if os.path.exists(p):
                os.remove(p)
        fsync_path(d)
        print("  the archive was put back as it was", file=sys.stderr)
        raise
    size = os.path.getsize(final)
    log_history(root, "add-images", m["project"], d, images=", ".join(i["tag"] for i in imgs), size=size)
    print("Added %d image(s), %s." % (len(imgs), human(size)))
    if not a.remove:
        return
    removed = []
    for i in sorted(imgs, key=lambda x: -len(x["layers"])):   # images built on others first
        if i["tag"] in blocked:
            continue
        why = image_users(i["id"], i["layers"], {x["id"] for x in imgs if x["tag"] not in blocked})
        if why:
            print("  kept %s: %s" % (i["tag"], "; ".join(why)))
            continue
        if ok(["docker", "rmi", i["tag"]]):
            removed.append(i["tag"])
            print("  removed %s from Docker" % i["tag"])
        else:
            print("  could not remove %s" % i["tag"])
    log_history(root, "remove-images", m["project"], d, images=", ".join(removed) or "-")



# ---------------------------------------------------------------- ensure (for scripts and agents)

# exit codes of `ensure` (the Claude Code skill documents them). 1 = other error, 2 = bad arguments.
ENSURE_READY, ENSURE_NO_DRIVE, ENSURE_ASK = 0, 3, 4
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
INDEX_FILE = os.path.expanduser("~/.docker-archive-index.json")   # what is archived, for when the drive is away
_RESTORE_LOCK = None


def ensure_exit(code, msg):
    print("ENSURE: %s" % msg)
    sys.stdout.flush()
    sys.exit(code)


def restore_lock(wait):
    """One restore at a time (UI and agents). Held until the process exits."""
    global _RESTORE_LOCK
    if _RESTORE_LOCK:
        return
    f = open("/tmp/docker-archive-restore.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if not wait:
            die("another restore is running. Wait for it to finish.")
        print("Waiting for another restore to finish...")
        sys.stdout.flush()
        fcntl.flock(f, fcntl.LOCK_EX)
    _RESTORE_LOCK = f


def _real(p):
    return os.path.realpath(host_path(p)) if p else ""


def norm_project(s):
    """docker compose's normalisation of a project name."""
    return re.sub(r"[^a-z0-9_-]", "", s.lower()).lstrip("_-")


def compose_dir_of(path):
    """The folder docker compose would use from here: this one or the nearest parent with a compose file."""
    p = path
    while True:
        if any(os.path.isfile(os.path.join(p, f)) for f in COMPOSE_FILES):
            return p
        up = os.path.dirname(p)
        if up == p:
            return None
        p = up


def compose_name(cdir):
    """Project name compose uses for a folder: COMPOSE_PROJECT_NAME (env or .env), top-level name:, folder name."""
    n = os.environ.get("COMPOSE_PROJECT_NAME")
    if not n:
        try:
            with open(os.path.join(cdir, ".env")) as f:
                for line in f:
                    m = re.match(r"\s*(?:export\s+)?COMPOSE_PROJECT_NAME\s*=\s*['\"]?([^'\"\s#]+)", line)
                    if m:
                        n = m.group(1)
        except OSError:
            pass
    if not n:
        for fn in COMPOSE_FILES:
            try:
                with open(os.path.join(cdir, fn)) as f:
                    m = re.search(r"(?m)^name:\s*['\"]?([^'\"\s#]+)", f.read())
            except OSError:
                continue
            if m and "$" not in m.group(1):
                n = m.group(1)
            break
    return norm_project(n or os.path.basename(cdir))


def drive_copies(root):
    """Every complete copy on the drive, plus the folders of copies that can't be read."""
    good, bad = [], []
    if not os.path.isdir(root):
        return good, bad
    for sub in sorted(os.listdir(root)):
        pdir = os.path.join(root, sub)
        if not os.path.isdir(pdir):
            continue
        for st in sorted(os.listdir(pdir)):
            d = os.path.join(pdir, st)
            if not (re.match(r"^\d{8}-\d{6}$", st) and os.path.isfile(os.path.join(d, "COMPLETE"))):
                continue
            try:
                with open(os.path.join(d, "manifest.json")) as f:
                    m = json.load(f)
                proj = m["project"]
                ok_ver = m.get("version", 1) >= 2
            except (OSError, ValueError, KeyError, TypeError):
                ok_ver, proj = False, None
            if not ok_ver:
                bad.append((sub, d))
                continue
            def mtime(*names):
                ts = [os.path.getmtime(os.path.join(d, x)) for x in names if os.path.exists(os.path.join(d, x))]
                return max(ts) if ts else None
            good.append({"project": proj, "dir": d, "stamp": st, "m": m,
                         "wd": _real((m.get("compose") or {}).get("working_dir")),
                         "removed": mtime("REMOVED", "REMOVING"), "restored": mtime("RESTORED")})
    # archived = removed from Docker from this copy, and the project has not come back since:
    # no restore (from any copy of it) after the removal, and no newer copy (it was in Docker again for that)
    for c in good:
        group = [o for o in good if o["project"] == c["project"] and o["wd"] == c["wd"]]
        c["archived"] = bool(c["removed"] and not c["restored"]
                             and not any(o["restored"] and o["restored"] > c["removed"] for o in group)
                             and not any(o["stamp"] > c["stamp"] for o in group))
    return good, bad


def archived_now(copies):
    """{project: [folders]} of projects removed from Docker and not restored since."""
    out = {}
    for c in copies:
        if c["archived"]:
            out.setdefault(c["project"], [])
            if c["wd"] not in out[c["project"]]:
                out[c["project"]].append(c["wd"])
    return out


def load_index():
    try:
        with open(INDEX_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sync_index(dest, copies=None):
    """Remember what is archived on this drive, so ensure can answer while the drive is unplugged."""
    if not (dest_available(dest) and os.path.isdir(dest)):
        return
    if copies is None:
        copies, _ = drive_copies(dest)
    idx = load_index()
    idx[os.path.abspath(dest)] = {"synced": now().isoformat(), "archived": archived_now(copies)}
    import tempfile
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(INDEX_FILE), prefix=".docker-archive-index.")
        with os.fdopen(fd, "w") as f:
            json.dump(idx, f)
        os.replace(tmp, INDEX_FILE)
    except OSError:
        pass


def same_compose(c, wd):
    """True when the archived compose file(s) of copy c equal the ones in folder wd and c's own folder is gone."""
    comp = c["m"].get("compose") or {}
    if not comp.get("copied") or not c["wd"] or os.path.isdir(c["wd"]):
        return False
    files = [x for x in comp["copied"] if x["file"] != ".env"]
    try:
        return bool(files) and all(open(os.path.join(c["dir"], "compose", x["file"]), "rb").read() ==
                                   open(os.path.join(wd, os.path.basename(x["source"])), "rb").read() for x in files)
    except OSError:
        return False


def missing_binds(m, moves=(), refill=False):
    """Bind sources that would be missing after a restore with these folder moves (and --restore-files)."""
    return sorted({moved(b["source"], moves) for c in m["containers"] for b in c["bind_mounts"]
                   if bind_problem(b, moved(b["source"], moves)) and not (refill and b.get("copy"))})


def cmd_ensure(a):
    """Make sure a project is in Docker before it is started. Restores it if it was archived. Never deletes."""
    restore_lock(wait=True)
    target = a.target
    in_docker = {}   # project -> folders its containers came from
    for c in all_containers():
        in_docker.setdefault(project_of(c), set()).add(
            _real((c["Config"].get("Labels") or {}).get("com.docker.compose.project.working_dir")))
    drive = dest_available(a.dest) and os.path.isdir(a.dest)
    copies, bad = drive_copies(a.dest) if drive else ([], [])
    if drive:
        sync_index(a.dest, copies)
        arch = archived_now(copies)
    else:
        idx = load_index().get(os.path.abspath(a.dest))
        arch = idx["archived"] if idx else None

    if target in (".", "..") or "/" in target:
        here = os.path.realpath(os.path.abspath(os.path.expanduser(target)))
        if not os.path.isdir(here):
            ensure_exit(ENSURE_ASK, "folder not found: %s" % here)
        wd = compose_dir_of(here)
        names = {compose_name(wd)} if wd else set()
        if not wd:
            wd = here
            below = sorted({c["wd"] for c in copies if c["wd"].startswith(here + "/")}
                           | {w for ws in (arch or {}).values() for w in ws if w.startswith(here + "/")})
            if below:
                ensure_exit(ENSURE_ASK, "there is no compose file in or above %s, but projects were saved from folders "
                            "inside it: %s. Run ensure from the project's own folder." % (here, ", ".join(below)))
        names |= {c["project"] for c in copies if c["wd"] == wd}
        names |= {p for p, ws in (arch or {}).items() if wd in ws}
        if not names:
            ensure_exit(ENSURE_READY, "no compose project in %s. Nothing to restore." % here)
    else:
        wd = None
        known = target in in_docker or target in (arch or {}) or any(c["project"] == target for c in copies)
        names = {target if known else norm_project(target)} - {""}
    others_idx = {d: e.get("archived", {}) for d, e in load_index().items() if d != os.path.abspath(a.dest)}

    def elsewhere(n):
        """Archived in a different archive folder than the one ensure was given (stale skill install)."""
        for d, ar in others_idx.items():
            if n in ar and (wd is None or wd in ar[n]):
                ensure_exit(ENSURE_ASK, "%s is archived in another archive folder, %s, but ensure is using %s. Ask the "
                            "user; the skill may need updating from the Claude Code tab in Docker Desktop." % (n, d, a.dest))

    vols_now = set(run(["docker", "volume", "ls", "-q"]).split())
    notes, todo = [], []
    for n in sorted(names):
        if n in in_docker:
            if wd is None or wd in in_docker[n] or "" in in_docker[n]:
                notes.append("%s is in Docker" % n)
                continue
            mine_arch = (arch is not None and wd in (arch or {}).get(n, [])) or \
                any(c["project"] == n and c["wd"] == wd and c["archived"] for c in copies)
            if mine_arch or arch is None:
                ensure_exit(ENSURE_ASK, "a project named %s from another folder (%s) is in Docker, but the one in %s "
                            "is %s. Starting it here would clash with the other one. Ask the user."
                            % (n, ", ".join(sorted(in_docker[n])), wd,
                               "archived" if mine_arch else "maybe archived (drive not connected)"))
            notes.append("%s is in Docker (from %s)" % (n, ", ".join(sorted(in_docker[n]))))
            continue
        if not drive:
            if arch is None:
                ensure_exit(ENSURE_NO_DRIVE, "the archive drive is not connected (%s), and docker-archive has no record yet "
                            "of what is archived on it. Connect the drive and run ensure again. Don't start %s before "
                            "that." % (a.dest, n))
            if n in arch:
                ensure_exit(ENSURE_NO_DRIVE, "%s is archived on the drive, which is not connected (%s). Connect it and run "
                            "ensure again. Don't start the project before that: it would get new, empty volumes."
                            % (n, a.dest))
            elsewhere(n)
            notes.append("%s is not archived" % n)
            continue
        pf = getattr(a, "project_folder", None)
        mine = [c for c in copies if c["project"] == n and (wd is None or pf or c["wd"] == wd)]
        if not mine:
            others = sorted({c["wd"] or "?" for c in copies if c["project"] == n})
            if others:
                ensure_exit(ENSURE_ASK, "no copy of %s was saved from %s, but there are copies saved from %s. If the "
                            "project moved here, ask the user, then run: ensure %s --project-folder \"%s\""
                            % (n, wd, ", ".join(others), n, wd))
            if any(sub == safe_name(n) for sub, _ in bad):
                ensure_exit(ENSURE_ASK, "the drive has copies of %s that can't be read or are from an old version." % n)
            elsewhere(n)
            if wd:
                like = sorted({(c["project"], c["wd"]) for c in copies if c["archived"] and same_compose(c, wd)})
                if like:
                    ensure_exit(ENSURE_ASK, "no copy of %s was saved, but %s has the same compose file as archived "
                                "project(s) saved from a folder that is gone: %s. If the project moved here, ask the user, "
                                "then run: ensure %s --project-folder \"%s\"" % (n, wd, ", ".join(
                                    "%s (%s)" % x for x in like), like[0][0], wd))
            notes.append("%s has no saved copy" % n)
            continue
        if (wd is None or pf) and len({c["wd"] for c in mine}) > 1:
            ensure_exit(ENSURE_ASK, "copies of %s were saved from several folders (%s). %s" % (
                n, ", ".join(sorted({c["wd"] or "?" for c in mine})),
                "Ask the user which one this is." if pf else "Run ensure from the project's folder instead."))
        archived = [c for c in mine if c["archived"]]
        pick = max(archived or mine, key=lambda c: c["stamp"])
        vols = [v["name"] for v in pick["m"]["volumes"]]
        have = [v for v in vols if v in vols_now]
        when = copy_time(pick["stamp"]).astimezone().strftime("%Y-%m-%d %H:%M")
        if have and len(have) < len(vols):
            ensure_exit(ENSURE_ASK, "%s has no containers and only some of its volumes (%s missing). Ask the user."
                        % (n, ", ".join(v for v in vols if v not in have)))
        if vols and have:
            if archived:
                ensure_exit(ENSURE_ASK, "%s was archived (removed from Docker) from the copy of %s, but its volumes exist "
                            "again, probably created empty by a later start. Ask the user: the archived data is on the "
                            "drive, and starting now would use the volumes in Docker." % (n, when))
            notes.append("%s has no containers but its volumes are in Docker" % n)
            continue
        if archived or not vols:
            if archived:
                todo.append((n, pick))
            else:
                notes.append("%s has no volumes" % n)
            continue
        if not a.restore_anyway:
            why = ("its newest copy (%s) was restored once already" % when) if pick["restored"] else \
                  ("it was deleted from Docker outside docker-archive; there is a backup from %s" % when)
            ensure_exit(ENSURE_ASK, "%s is not in Docker and its volumes are gone: %s. Ask the user whether to restore "
                        "that copy (run ensure again with --restore-anyway) or start fresh." % (n, why))
        todo.append((n, pick))

    for n, c in todo:
        pf = getattr(a, "project_folder", None)
        mv = folder_moves(argparse.Namespace(project_folder=pf), c["m"]) if pf else []
        holes = missing_binds(c["m"], mv, a.restore_files)
        if holes and not a.restore_anyway:
            kept = sorted({moved(b["source"], mv) for x in c["m"]["containers"] for b in x["bind_mounts"]
                           if b.get("copy") and moved(b["source"], mv) in holes})
            wd0 = (c["m"].get("compose") or {}).get("working_dir")
            gone = wd0 and not pf and not os.path.isdir(host_path(wd0))
            ensure_exit(ENSURE_ASK, "%s is archived, but %d bind-mounted file(s)/folder(s) it needs are missing: %s. "
                        "Nothing was restored. Ask the user.%s%s Or restore anyway with --restore-anyway (containers are "
                        "not started; Docker would create missing ones empty)." % (
                            n, len(holes), ", ".join(holes),
                            (" Its folder %s is gone: if the project moved, run ensure from the new folder with "
                             "--project-folder \"<new folder>\"." % host_path(wd0)) if gone else "",
                            (" The archive has copies of %d of them: add --restore-files%s to put them back."
                             % (len(kept), " (with --project-folder)" if gone else "")) if kept else ""))
        print("%s: restoring the copy %s" % (n, c["dir"]))
        ra = argparse.Namespace(archive=c["dir"], start=a.start and not holes, keep_restart_policy=True,
                                keep_backup=True, check_seconds=a.check_seconds, yes=True,
                                project_folder=pf, move_folder=None, restore_files=a.restore_files)
        try:
            outcome = cmd_restore(ra)
        except Abort as e:
            ensure_exit(ENSURE_ASK, "could not restore %s: %s" % (n, e))
        finally:
            sync_index(a.dest)
        if holes:
            ensure_exit(ENSURE_ASK, "restored %s (not started); these bind-mount files/folders are still missing: %s. "
                        "Ask the user before starting it." % (n, ", ".join(holes)))
        if outcome in ("problems", "unhealthy"):
            ensure_exit(ENSURE_ASK, "restored %s, but not every container started and stayed up (see above)." % n)
        notes.append("restored %s%s" % (n, " and started it" if a.start else ""))
        if pf and compose_dir_of(pf) and compose_name(compose_dir_of(pf)) != n:
            notes.append("the folder's name differs from the project's, so start it with: docker compose -p %s up -d" % n)
    ensure_exit(ENSURE_READY, "%s. Start the project as usual." % "; ".join(notes))


# ---------------------------------------------------------------- Claude Code skill

SKILL_MARK = "<!-- installed by docker-archive install-skill -->"

SKILL_TEMPLATE = """---
name: docker-archive
description: Restore a Docker Compose project that docker-archive moved to an external drive. Use before starting, running or spinning up a Docker Compose project on this Mac (docker compose up, "start the project", "run it locally"), and when a project's containers, volumes or images are missing from Docker.
---

# Restore archived Docker projects

%(mark)s

Some Docker Compose projects on this Mac are archived to an external drive by docker-archive to free disk space. An archived project has no containers, volumes or images in Docker until it is restored. Running `docker compose up` before restoring creates new, empty volumes: the project looks like its data is gone, and the archived data no longer fits in.

## Before starting a compose project

From the folder with the project's compose file, run:

```bash
%(ensure)s .
```

Or by compose project name: `%(ensure)s NAME`.

- If nothing needs restoring, it returns at once.
- If the project was archived, it restores it (containers created, not started) and keeps the copy on the drive.
- A restore can take many minutes for big projects. Run the command in the background and wait for it to finish; don't stop it partway.

Read the last line (it starts with `ENSURE:`) and the exit code:

| Exit | Meaning | What to do |
|---|---|---|
| 0 | Ready: in Docker, restored, or nothing archived | Start the project the usual way, e.g. `docker compose up -d` |
| 3 | The archive drive is not connected | Ask the user to connect it, then run ensure again. Don't start the project before that |
| 4 | Needs a decision, or the restore failed | Stop. Show the user the `ENSURE:` line and do what they decide |
| any other | Error (bad arguments, Docker not running, interrupted) | Stop and show the user the error |

Add `--start` to let ensure start the restored containers and check they stay up.

## Rules

- Only restore. Never back up, archive, remove projects from Docker, or delete saved copies. Those change or delete data; the user does them in the Archive tab in Docker Desktop.
- Use `--restore-anyway`, `--project-folder` and `--restore-files` only after the user says so (exit 4 explains when).
  - `--project-folder "<folder>"`: the project was moved; restore it pointing at its new folder.
  - `--restore-files`: put back config and secrets files the archive kept copies of. It never overwrites a file.
- Don't create missing bind-mount folders or delete volumes to get past exit 4. Ask the user.
"""

WRAPPER_TEMPLATE = """#!/bin/sh
# Generated by docker-archive install-skill. Runs only the ensure command (restore if missing).
exec /usr/bin/python3 %(script)s --dest %(dest)s ensure "$@"
"""


def skill_paths():
    base = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
    d = os.path.join(base, "skills", "docker-archive")
    return base, d


def skill_files(a):
    """What install-skill writes: {file name: (bytes, mode)}."""
    import shlex
    _, d = skill_paths()
    with open(os.path.realpath(__file__), "rb") as f:
        script = f.read()
    ensure = os.path.join(d, "ensure")
    wrapper = WRAPPER_TEMPLATE % {"script": shlex.quote(os.path.join(d, "docker-archive.py")),
                                  "dest": shlex.quote(os.path.abspath(a.dest))}
    skill = SKILL_TEMPLATE % {"mark": SKILL_MARK, "ensure": shlex.quote(ensure)}
    # SKILL.md last, so a half-finished install never points at a missing wrapper
    return {"docker-archive.py": (script, 0o755), "ensure": (wrapper.encode(), 0o755),
            "SKILL.md": (skill.encode(), 0o644)}


def skill_state(a):
    base, d = skill_paths()
    import shlex
    info = {"path": d, "permission_rule": "Bash(%s:*)" % shlex.quote(os.path.join(d, "ensure"))}
    if not os.path.isdir(base):
        return dict(info, state="no-claude")
    md = os.path.join(d, "SKILL.md")
    if not os.path.exists(md):
        return dict(info, state="not-installed")
    try:
        with open(md) as f:
            if SKILL_MARK not in f.read():
                return dict(info, state="foreign")
    except OSError:
        return dict(info, state="foreign")
    for name, (data, _) in skill_files(a).items():
        try:
            with open(os.path.join(d, name), "rb") as f:
                if f.read() != data:
                    return dict(info, state="outdated")
        except OSError:
            return dict(info, state="outdated")
    return dict(info, state="installed")


def cmd_install_skill(a):
    st = skill_state(a)
    if a.status:
        print(json.dumps(st))
        return
    d = st["path"]
    if st["state"] == "no-claude":
        die("Claude Code is not set up on this Mac (%s not found)." % skill_paths()[0])
    if st["state"] == "foreign":
        die("%s exists but was not installed by docker-archive. Leaving it alone." % d)
    if a.remove:
        if st["state"] == "not-installed":
            print("The Claude Code skill is not installed.")
            return
        print("Remove the Claude Code skill: %s" % d)
        confirm_plan(a.yes, "remove it")
        for name in skill_files(a):
            try:
                os.remove(os.path.join(d, name))
            except FileNotFoundError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            print("  note: %s has other files in it, so the folder was kept" % d)
        change = "removed"
    else:
        if st["state"] == "installed":
            print("The Claude Code skill is installed and up to date: %s" % d)
            return
        change = "updated" if st["state"] == "outdated" else "installed"
        print("%s the Claude Code skill at %s" % ("Update" if change == "updated" else "Install", d))
        print("  It lets Claude Code restore an archived project when asked to start it. It never removes anything.")
        confirm_plan(a.yes, "install it")
        os.makedirs(d, exist_ok=True)
        import tempfile
        for name, (data, mode) in skill_files(a).items():
            fd, tmp = tempfile.mkstemp(dir=d, prefix="." + name + ".")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, os.path.join(d, name))
        print("Done. Claude Code picks it up in new sessions.")
        print("To run it without a permission prompt, add this to permissions.allow in ~/.claude/settings.json:")
        print("  %s" % st["permission_rule"])
    if os.path.isdir(a.dest):
        log_history(a.dest, "skill", "-", change=change)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Archive idle Docker projects to an external drive.")
    p.add_argument("--dest", default=DEFAULT_DEST, help="archive folder (default %(default)s)")
    p.add_argument("--allow-internal-dest", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status"); s.add_argument("--months", type=float, default=3)
    s.set_defaults(fn=cmd_status)

    b = sub.add_parser("backup")
    b.add_argument("projects", nargs="*", help="project names (default: all idle >= --months)")
    b.add_argument("--months", type=float, default=3)
    b.add_argument("--remove-after", action="store_true",
                   help="after a verified archive, remove the project from Docker")
    b.add_argument("--pause-running", action="store_true",
                   help="pause running containers while copying, resume them after (backup only)")
    b.add_argument("--yes", action="store_true")
    b.set_defaults(fn=cmd_backup)

    v = sub.add_parser("verify"); v.add_argument("archive"); v.set_defaults(fn=cmd_verify)

    r = sub.add_parser("remove"); r.add_argument("archive"); r.add_argument("--yes", action="store_true")
    r.set_defaults(fn=cmd_remove)

    rs = sub.add_parser("restore"); rs.add_argument("archive")
    rs.add_argument("--start", action="store_true", help="start the containers after restoring")
    rs.add_argument("--keep-restart-policy", action="store_true",
                    help="keep the original restart policy (default: containers do not auto-start)")
    rs.add_argument("--keep-backup", action="store_true",
                    help="keep the backup even after a successful restore (default: delete it)")
    rs.add_argument("--check-seconds", type=int, default=30,
                    help="how long containers must stay up before the backup is deleted")
    rs.add_argument("--project-folder", metavar="PATH",
                    help="the project folder has moved: point bind mounts and compose labels at PATH")
    rs.add_argument("--move-folder", nargs=2, action="append", metavar=("OLD", "NEW"),
                    help="map any other moved folder used by bind mounts (repeatable)")
    rs.add_argument("--restore-files", action="store_true",
                    help="put back missing bind-mounted files/folders from the copies kept in the archive")
    rs.add_argument("--yes", action="store_true")
    rs.set_defaults(fn=cmd_restore)

    ar = sub.add_parser("archives"); ar.set_defaults(fn=cmd_archives)

    db = sub.add_parser("delete-backup", help="delete one backup folder")
    db.add_argument("archive")
    db.add_argument("--only-copy", action="store_true",
                    help="confirm deleting even though the project is not in Docker")
    db.add_argument("--yes", action="store_true")
    db.set_defaults(fn=cmd_delete_backup)

    st = sub.add_parser("settings", help="show or change backup and archive settings")
    st.add_argument("--max-backups", type=int, help="backups kept per project (archives never count)")
    st.add_argument("--project", help="apply --max-backups to one project only")
    st.add_argument("--archive-enable", "--auto-enable", dest="auto_enable", action="store_true",
                    help="turn automatic archiving on (copy + remove idle projects)")
    st.add_argument("--archive-disable", "--auto-disable", dest="auto_disable", action="store_true")
    st.add_argument("--archive-weeks", "--auto-weeks", dest="auto_weeks", type=int,
                    help="archive projects idle for this many weeks")
    st.add_argument("--backup-enable", action="store_true", help="turn automatic backups on (copy only)")
    st.add_argument("--backup-disable", action="store_true")
    st.add_argument("--backup-days", type=int, help="back up each project every N days")
    st.add_argument("--backup-running", choices=["skip", "pause"],
                    help="running projects: skip them, or pause them while copying")
    st.add_argument("--min-battery", type=int, help="on battery, only run above this percent")
    st.set_defaults(fn=cmd_settings)

    js = sub.add_parser("json", help="machine-readable output for the UI")
    js.add_argument("what", choices=["status", "archives", "history", "settings"])
    js.set_defaults(fn=cmd_json)

    au = sub.add_parser("auto", help="the daily check: automatic archiving and backups, per settings")
    au.add_argument("--force", action="store_true", help="ignore the battery limit")
    au.add_argument("--only", nargs="+", help=argparse.SUPPRESS)
    au.set_defaults(fn=cmd_auto)

    sc = sub.add_parser("schedule", help="install/remove the daily automatic check (launchd)")
    sc.add_argument("--hour", type=int, help="hour of day to run, 0-23")
    sc.add_argument("--remove", action="store_true")
    sc.set_defaults(fn=cmd_schedule)

    en = sub.add_parser("ensure", help="make sure a project is in Docker; restore its newest copy if not")
    en.add_argument("target", help="compose project name, or a folder path such as . for the current folder")
    en.add_argument("--start", action="store_true", help="start the restored containers and check they stay up")
    en.add_argument("--restore-anyway", action="store_true",
                    help="restore a copy even though the project was not archived by docker-archive, was restored "
                         "before, or misses bind-mounted files")
    en.add_argument("--project-folder", metavar="PATH", help="the project folder has moved here (see restore)")
    en.add_argument("--restore-files", action="store_true",
                    help="put back missing bind-mounted files from the archive's copies")
    en.add_argument("--check-seconds", type=int, default=30, help=argparse.SUPPRESS)
    en.set_defaults(fn=cmd_ensure)

    sk = sub.add_parser("install-skill", help="install the Claude Code skill that restores projects on request")
    sk.add_argument("--remove", action="store_true", help="remove the skill")
    sk.add_argument("--status", action="store_true", help="print the skill state as JSON")
    sk.add_argument("--yes", action="store_true")
    sk.set_defaults(fn=cmd_install_skill)

    ai = sub.add_parser("add-images", help="add images no container runs (e.g. base images) to an existing archive")
    ai.add_argument("archive")
    ai.add_argument("images", nargs="+", help="image names, e.g. myapp-base:latest")
    ai.add_argument("--remove", action="store_true",
                    help="then remove them from Docker (skips any a container or another image still needs)")
    ai.add_argument("--yes", action="store_true")
    ai.set_defaults(fn=cmd_add_images)

    hi = sub.add_parser("history", help="show the action log")
    hi.add_argument("-n", type=int, default=30)
    hi.set_defaults(fn=cmd_history)

    a = p.parse_args()

    def on_term(signum, frame):   # extension stop / kill: unwind so finally blocks run (unpause, rollback)
        raise KeyboardInterrupt("stopped by signal %d" % signum)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGHUP, on_term)

    def record(result):
        if a.cmd not in LOGGED_CMDS or not os.path.isdir(a.dest):
            return
        arc = getattr(a, "archive", None)
        if arc:
            d = os.path.abspath(arc)
            if os.path.isfile(os.path.join(d, "manifest.json")):   # not a typo'd path
                log_history(dest_root_of(d), a.cmd, archive_project(d), d, result=result)
        else:
            log_history(a.dest, "archive" if getattr(a, "remove_after", False) else a.cmd,
                        ",".join(getattr(a, "projects", None) or []) or "-", result=result)
    try:
        if a.cmd == "install-skill":
            return a.fn(a)
        if not ok(["docker", "info", "--format", "{{.ServerVersion}}"]):
            die("Docker is not running.")
        resume_leftovers()
        try:
            a.fn(a)
        finally:
            if a.cmd in ("backup", "remove", "restore", "delete-backup", "auto", "json"):
                try:
                    sync_index(dest_root_of(a.archive) if getattr(a, "archive", None) else a.dest)
                except Exception:
                    pass
    except Abort as e:
        if getattr(a, "yes", True) and not (RUN_LOG and str(RUN_LOG[-1]).startswith("failed")):
            record("failed: %s" % e)
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt as e:
        why = str(e) or "Ctrl-C"
        if getattr(a, "yes", True):
            record("interrupted: %s" % why)
        print("\nINTERRUPTED: %s" % why, file=sys.stderr)
        sys.exit(130)
    except OSError as e:   # e.g. drive unplugged mid-copy
        if getattr(a, "yes", True):
            record("failed: %s" % e)
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
