# Roadmap

Done:
- Archive format v2 with checksums, per-archive README, verified restore.
- Backups (copy only) and archives (copy, verify, remove from Docker).
- Delete the archive automatically after a verified, healthy restore (optional keep).
- Keep the last N backups per project. Archives never count and are never trimmed.
- History of every action, including failed and interrupted runs.
- Automatic backups every N days (running projects skipped or paused).
- Automatic archiving of projects idle X weeks. Battery limit. Daily launchd check.
- Docker Desktop extension (macOS).

Open:
- Round trip of containers with bind mounts from Docker Desktop's `/host_mnt` paths.
- Test with large projects (20 GB+ of images).
- Archive unused images that no container uses.
- Volume copies use busybox tar: extended attributes are dropped and sparse files are expanded.
- Image platform is not passed when containers are re-created.
- Linux support (battery, launchd and the drive check are macOS-only).
- Publish the extension so it installs without turning off the Marketplace-only setting.
