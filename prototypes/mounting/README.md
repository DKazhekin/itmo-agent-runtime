# macOS to Linux SSHFS prototype

This standalone experiment shares a generated macOS folder over authenticated
SFTP and mounts it with SSHFS inside a running Colima VM. It is not integrated
with the runtime. No real user folder is exported. The SFTP server binds only
to `127.0.0.1`; the Mac opens a reverse SSH tunnel to the VM, also bound to
loopback. No macOS Remote Login, NFS exports, FUSE extension, or security
settings are changed.

The selected SSHFS version is **3.7.6**, not the VM's original 3.7.3. Upstream
3.7.6 fixes symlink containment and argument injection vulnerabilities. The
server is AsyncSSH 2.24.1. Exact Python dependencies are pinned.

## Prerequisites and run

macOS with Python 3.14 (the tested version), OpenSSH, and a running Colima
Ubuntu VM with `/dev/fuse`, `fusermount3`, and its generated SSH configuration.
The VM must be disposable development infrastructure. Preparing the compiler
installs Ubuntu packages and may upgrade their dependencies. It needs network
access; the experiment after preparation uses only loopback and the local VM.

From the repository root:

```sh
python3 -m venv /private/tmp/itmo-mount-venv
/private/tmp/itmo-mount-venv/bin/python -m pip install -r prototypes/mounting/requirements.txt
colima ssh -- sh < prototypes/mounting/prepare_linux.sh
/private/tmp/itmo-mount-venv/bin/python prototypes/mounting/run.py --results results/mounting/latest
```

Each command, exit code, duration, and check is written to `result.json`.
Private key content is never logged. Do not reuse an existing results path
when retaining an earlier run matters. All credentials, exported files,
mountpoints, SFTP server, SSHFS workers and tunnel belong to this run and are
removed when it exits. The Colima VM remains running. The prepared Python
environment, Linux build directory and packages remain available until
explicitly removed.

Optional cleanup of the preparation after finishing experiments:

```sh
colima ssh -- rm -rf /tmp/itmo-sshfs-build
rm -rf /private/tmp/itmo-mount-venv
```

Do not uninstall shared VM packages automatically; other work may need them.

## Checks and limits

The runner checks the actual mount type (`fuse.sshfs`), Mac writes read by
Linux, Linux writes read by Mac, later Mac edits, permission changes, mode
0444 write denial, read-only mount denial, safe relative links, rejection of
external links and `..` traversal, rejection of an unknown key and shell
sessions, a stalled server, a closed tunnel, explicit remount, and cleanup.

`SIGSTOP` on the fixture models an unresponsive SFTP peer. A separately tested
closed SSH tunnel models a broken connection. Keepalive interval 1 second,
count 2 and connection timeout 3 seconds are experiment parameters. They do
not establish a production service-level guarantee. Tests fail if an
operation hangs beyond the external 10-second process bound; the successful
run requires a filesystem error within 8 seconds.

SSHFS can automatically unmount after a tunnel closes. A file read can then
return `ENOENT` from the underlying empty directory. A runtime must stop
tools on mount loss; otherwise a later write can go to the server's local
directory. This prototype records the behavior, without claiming a complete
runtime protection. It does not retry writes automatically.

The SFTP fixture checks real paths and forbids creating links. It is **not a
production confinement boundary**: a concurrent local actor can race a path
check. Descriptor-relative filesystem operations or an operating-system
sandbox must close that gap before exporting real folders. Only controlled
fixtures are used. Concurrent edits, rename/fsync durability, crash recovery,
large files, case/Unicode collisions, containers consuming the mount, and
separate physical machines remain untested.
