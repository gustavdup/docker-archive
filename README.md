# docker-archive

Free up disk space on your Mac by moving idle Docker Compose projects to an external drive, and bring them back exactly as they were.

Docker Desktop keeps everything in one growing disk file. Projects you haven't touched in months still hold their images, volumes and container state. `docker-archive` copies a whole project (images, volumes, container config, changed files inside containers, compose files and `.env`) to an external drive, checks the copy, and only then removes it from Docker. Restoring re-creates the containers with the same names, networks, volumes and settings.

It comes as a single Python script (stdlib only) and an optional Docker Desktop extension with a UI.

> **Status:** early and built for personal use on macOS. It is careful about your data (see Safety), but test it on a project you can afford to lose before trusting it with one you can't. Use at your own risk.

## Backup vs archive

| | Backup | Archive |
|---|---|---|
| What it does | Copies the project to the drive | Copies, verifies, then removes the project from Docker |
| Project afterwards | Stays in Docker | Gone from Docker, the archive is the only copy |
| Frees space | No | Yes |
| Automatic | Every N days (time-based) | When idle for X weeks (age-based) |
| Trimmed by "keep last N" | Yes | Never |

## Requirements

- macOS with Docker Desktop
- Python 3.9+ (the system `/usr/bin/python3` works)
- An external drive mounted under `/Volumes` (the tool refuses to write to the Mac's internal disk)

## Command line

```bash
./docker-archive.py status                       # projects, idle time, size, what removing would free
./docker-archive.py backup myproject --yes       # back up (copy only)
./docker-archive.py backup myproject --pause-running --yes   # back up a running project, paused while copying
./docker-archive.py backup myproject --remove-after --yes    # archive: copy, verify, remove from Docker
./docker-archive.py archives                     # list copies on the drive
./docker-archive.py verify  <archive dir>        # re-check checksums
./docker-archive.py remove  <archive dir> --yes  # remove a backed-up project from Docker
./docker-archive.py restore <archive dir> --start --yes      # restore; deletes the archive once healthy
./docker-archive.py restore <archive dir> --start --keep-backup --yes
./docker-archive.py delete-backup <archive dir> --yes
./docker-archive.py history                      # every action, including failed and interrupted ones
```

Without `--yes`, commands show their plan and change nothing.

Default drive folder: `/Volumes/External-SSD/docker-archive`. Change it with `--dest PATH` or the `DOCKER_ARCHIVE_DEST` environment variable.

### Automation

```bash
./docker-archive.py settings --backup-enable --backup-days 7 --backup-running pause --max-backups 3
./docker-archive.py settings --archive-enable --archive-weeks 8 --min-battery 50
./docker-archive.py schedule --hour 3            # install the daily check (launchd)
./docker-archive.py auto                         # run the daily check now
```

The daily check runs only when the drive is connected, and on battery only above the set percentage (always when plugged in). Running projects are never archived. For backups they are either skipped or paused for the copy, then resumed.

## Docker Desktop extension

A tab in Docker Desktop with projects, archives and backups, automation settings, history and a live log.

1. Docker Desktop → Settings → Extensions → untick **"Allow only extensions distributed through the Docker Marketplace"** → Apply. (Needed for locally built extensions.)
2. `cd docker-archive-extension && ./build.sh`
3. Open the **Archive** tab in the Docker Desktop sidebar.

Updating the extension stops any job that is running in it, so `build.sh` refuses while one runs.

## What's in an archive

Each copy is a folder `<drive>/docker-archive/<project>/<timestamp>/` with:

- `images.tar`: all images the containers use
- `volumes/*.tgz`: every named and anonymous volume
- `manifest.json`: the full container config
- snapshots of containers with changed files inside them
- `compose/`: copies of the compose files and `.env`
- `checksums.sha256` and a `README.txt` explaining the folder

Bind-mounted folders (files in your repos) are not copied. They stay where they are.

## Safety

- It only writes to a mounted external drive, never the internal disk.
- A backup never removes anything.
- Remove re-verifies the archive first, and refuses when any of these hold:
  - a volume changed since the copy;
  - a container was started or re-created since;
  - files inside a container changed;
  - the copy was taken while the project was running;
  - the archive was restored already.
- Images and volumes that other containers still use are kept.
- A restore that fails partway rolls back what it created.
- After `restore --start`, the archive is deleted only if every volume matches the archive file for file and all containers stay running and healthy for 30 seconds.
- Restored containers do not auto-start with Docker unless you pass `--keep-restart-policy`.
- If a run is stopped, it unpauses anything it paused. A hard kill is cleaned up on the next run.

See [ROADMAP.md](ROADMAP.md) for known gaps.

## License

MIT
