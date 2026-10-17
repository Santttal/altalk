# Build image for packaging/build.sh: Debian 11 (glibc 2.31) keeps the executable runnable on
# Ubuntu 22.04+, Debian 12+, Fedora, RHEL 9 and Arch; rpm and libarchive-tools pack .rpm and Arch.
# Debian 11 is end-of-life, so its packages come from archive.debian.org.
FROM python:3.12-slim-bullseye
RUN printf '%s\n' \
        'deb http://archive.debian.org/debian bullseye main' \
        'deb http://archive.debian.org/debian-security bullseye-security main' \
        > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        libportaudio2 binutils gcc libc6-dev rpm libarchive-tools zstd \
    && rm -rf /var/lib/apt/lists/*
