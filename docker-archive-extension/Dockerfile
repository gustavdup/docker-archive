FROM alpine:3.23
LABEL org.opencontainers.image.title="Docker Archive" \
      org.opencontainers.image.description="Back up idle compose projects to an external drive, free the space, restore them later." \
      org.opencontainers.image.vendor="local" \
      com.docker.desktop.extension.api.version=">= 0.3.4" \
      com.docker.extension.categories="utility-tools" \
      com.docker.extension.publisher-url="https://github.com/gustavdup/docker-archive" \
      com.docker.extension.detailed-description="Wraps docker-archive.py: back up, archive and restore Docker Compose projects on an external drive." \
      com.docker.extension.screenshots="" \
      com.docker.extension.changelog="" \
      com.docker.extension.additional-urls=""
COPY metadata.json icon.svg /
COPY ui /ui
COPY darwin /darwin
