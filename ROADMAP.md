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
- Base images archived with the project; `add-images` for leftover images.
- Containers are re-created with their image's platform (amd64 images on Apple chips).
- `ensure` and a Claude Code skill to restore a project before it starts.
- Small bind-mounted files kept in archives; restore into a moved project folder (`--project-folder`, `--restore-files`).

Open:
- Round trip of containers with bind mounts from Docker Desktop's `/host_mnt` paths.
- Test with large projects (20 GB+ of images).
- Volume copies use busybox tar: extended attributes are dropped and sparse files are expanded.
- Linux support (battery, launchd and the drive check are macOS-only).
- Publish the extension so it installs without turning off the Marketplace-only setting.
