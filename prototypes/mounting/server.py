"""Loopback-only SFTP fixture; not a production filesystem security boundary."""

import argparse
import asyncio
import os
from pathlib import Path

import asyncssh


class ConfinedSFTP(asyncssh.SFTPServer):
    """Reject lexical traversal and existing links escaping the fixture root.

    This realpath check is deliberately not claimed race-safe against a
    concurrent local attacker. Production confinement needs descriptor-based
    operations or an OS sandbox, not this Python path check.
    """

    def __init__(self, channel, root):
        self.root = os.fsencode(Path(root).resolve())
        super().__init__(channel, chroot=self.root)

    def map_path(self, path):
        if b".." in path.split(b"/"):
            raise asyncssh.SFTPPermissionDenied("Parent traversal denied")
        mapped = super().map_path(path)
        if os.path.commonpath([self.root, os.path.realpath(mapped)]) != self.root:
            raise asyncssh.SFTPPermissionDenied("Path outside shared root")
        return mapped

    def symlink(self, oldpath, newpath):
        raise asyncssh.SFTPPermissionDenied("Creating symlinks disabled in fixture")

    def readlink(self, path):
        # Preserve safe relative targets; the base virtual-chroot implementation
        # resolves relative targets against the process working directory.
        target = os.readlink(self.map_path(path))
        if os.path.isabs(target) or b".." in target.split(b"/"):
            raise asyncssh.SFTPPermissionDenied("Only contained relative links allowed")
        return target

    def link(self, oldpath, newpath):
        raise asyncssh.SFTPPermissionDenied("Creating hardlinks disabled in fixture")


async def main():
    parser = argparse.ArgumentParser()
    for name in ("root", "host-key", "client-key", "ready"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    allowed_key = asyncssh.read_public_key(args.client_key)

    class KeyOnlyServer(asyncssh.SSHServer):
        def public_key_auth_supported(self):
            return True

        def validate_public_key(self, username, key):
            return username == "fixture" and key == allowed_key

    server = await asyncssh.create_server(
        KeyOnlyServer, "127.0.0.1", 0,
        server_host_keys=[args.host_key],
        sftp_factory=lambda channel: ConfinedSFTP(channel, args.root),
        allow_scp=False,
    )
    Path(args.ready).write_text(str(server.get_port()))
    await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
