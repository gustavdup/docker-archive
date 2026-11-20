#!/bin/sh
# Build and install (or update) the extension in your own Docker Desktop.
set -e
cd "$(dirname "$0")"
# Updating the extension kills any running archive/restore. Refuse while one runs.
if pgrep -f "extensions/docker-archive-ext/host/docker-archive" >/dev/null 2>&1; then
  echo "An archive/remove/restore is running in the extension. Wait for it to finish, then re-run." >&2
  exit 1
fi
mkdir -p darwin
cp ../docker-archive.py darwin/docker-archive
chmod +x darwin/docker-archive
docker build -t docker-archive-ext:dev .
if docker extension ls | grep -q docker-archive-ext; then
  docker extension update -f docker-archive-ext:dev
else
  docker extension install -f docker-archive-ext:dev
fi
