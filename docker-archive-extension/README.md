# Docker Archive extension (personal, macOS)

A Docker Desktop tab on top of `../docker-archive.py`. All backup, remove and restore logic
lives in that script; the UI only calls it.

## Install / update
1. **One-time setting (required):** Docker Desktop → Settings (gear, top right) → Extensions →
   untick **"Allow only extensions distributed through the Docker Marketplace"** → Apply.
   No restart needed. Without this, install fails with:
   `only extensions distributed through the Docker Marketplace are allowed`.
2. `./build.sh` (copies the script in, builds `docker-archive-ext:dev`, installs or updates it).
3. The **Archive** tab appears in Docker Desktop's left sidebar under Extensions.
   After code changes, run `./build.sh` again to update it.

Uninstall: `docker extension rm docker-archive-ext:dev`

## What it does
- Projects: idle time, what removing would free, per-container sizes (click a project).
  Tick projects, then **Back up** (copy, project stays in Docker) or **Archive** (copy, then
  remove from Docker after a confirm panel).
- Archives & backups: status is Backup (in Docker), Archived (removed, only copy) or Outdated
  (restored from). Each row has Restore, Remove from Docker and Delete; buttons that don't
  apply are greyed with a tooltip. Clicking one opens its options/confirm under the row.
  After a started, verified, healthy restore the archive is deleted unless "keep" is ticked.
- Automation: automatic backups (every N days, keep the last N, running projects skipped or
  paused), automatic archiving (idle X weeks, always removes from Docker), and the daily check
  (launchd) with a battery limit (always runs when plugged in).
- History of every action (including failed and interrupted ones) and a live log.
- Claude Code: install the skill that lets Claude Code restore an archived project when asked to
  start it (`ensure`). Claude Code only; Claude.ai web/desktop chat skills can't reach Docker.

The daily check runs the script copy inside the installed extension. Re-run
"Install daily check" after updating the extension.
