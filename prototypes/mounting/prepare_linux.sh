#!/bin/sh
# Run inside the dedicated Ubuntu VM. Does not replace system sshfs.
set -eu
sudo apt-get update
sudo apt-get install -y --no-install-recommends build-essential meson ninja-build pkg-config libfuse3-dev libglib2.0-dev
build_root=/tmp/itmo-sshfs-build
mkdir -p "$build_root"
curl -fsSL https://github.com/libfuse/sshfs/archive/refs/tags/sshfs-3.7.6.tar.gz -o "$build_root/source.tar.gz"
printf '%s  %s\n' 67a3e166a39b07708497ee0aee308547dba386053cf8d816b4ce8a9b3066a6ce "$build_root/source.tar.gz" | sha256sum -c -
tar -xzf "$build_root/source.tar.gz" -C "$build_root"
if [ ! -d "$build_root/build" ]; then
    meson setup "$build_root/build" "$build_root/sshfs-sshfs-3.7.6"
fi
ninja -C "$build_root/build"
"$build_root/build/sshfs" --version
