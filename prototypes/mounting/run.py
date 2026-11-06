"""Exercise an actual Linux SSHFS mount of a temporary macOS folder."""

import argparse
import asyncio
import datetime
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import asyncssh


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--sshfs", default="/tmp/itmo-sshfs-build/build/sshfs")
    parser.add_argument("--ssh-config", type=Path, default=Path.home() / ".colima/_lima/colima/ssh.config")
    args = parser.parse_args()
    args.results.mkdir(parents=True, exist_ok=True)
    report = {"started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "host": platform.platform(), "python": sys.version, "checks": [], "commands": [],
              "limitations": ["Controlled fixture, not race-safe against a concurrent local attacker",
                              "Explicit remount after interruption; no transparent retry of writes",
                              "No product sandbox integration, concurrent edit or durability guarantee"]}
    work = Path(tempfile.mkdtemp(prefix="itmo-mount-", dir="/private/tmp"))
    remote = "/tmp/" + work.name
    mountpoint = remote + "/mounted"
    children = []
    logs = []
    ssh = None
    server = None
    tunnel = None
    success = False

    def check(name, passed, details):
        report["checks"].append({"name": name, "passed": bool(passed), "details": details})
        print(json.dumps(report["checks"][-1]), flush=True)
        if not passed:
            raise AssertionError(name)

    def run(command, *, input=None, timeout=15, expected=0):
        start = time.monotonic()
        record = {"argv": command}
        report["commands"].append(record)
        try:
            result = subprocess.run(command, input=input, capture_output=True, text=True, timeout=timeout)
            record.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                          seconds=round(time.monotonic() - start, 4))
        except subprocess.TimeoutExpired:
            record.update(error="timeout", seconds=round(time.monotonic() - start, 4))
            raise
        if expected is not None and result.returncode != expected:
            raise RuntimeError(json.dumps(record))
        return result

    def vm(command, **kwargs):
        return run(ssh + ["lima-colima", command], **kwargs)

    def py(code, **kwargs):
        return vm(shlex.join(["python3", "-c", code]), **kwargs)

    def spawn(command, name):
        log = (args.results / (name + ".log")).open("w")
        logs.append(log)
        report["commands"].append({"argv": command, "background_log": name + ".log"})
        child = subprocess.Popen(command, stdout=log, stderr=log)
        children.append(child)
        return child

    def stop(child):
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def unmount():
        vm(shlex.join(["fusermount3", "-u", mountpoint]), expected=None)

    def mount(label, readonly=False):
        options = ["port=" + str(port), "IdentityFile=" + remote + "/client_key",
                   "UserKnownHostsFile=" + remote + "/known_hosts", "StrictHostKeyChecking=yes",
                   "IdentitiesOnly=yes", "BatchMode=yes", "ServerAliveInterval=1",
                   "ServerAliveCountMax=2", "ConnectTimeout=3", "default_permissions",
                   "idmap=user", "direct_io", "dir_cache=no", "attr_timeout=0",
                   "entry_timeout=0", "negative_timeout=0", "sshfs_sync", "contain_symlinks"]
        if readonly:
            options.append("ro")
        command = shlex.join([args.sshfs, "fixture@127.0.0.1:/", mountpoint, "-f", "-o", ",".join(options)])
        child = spawn(ssh + ["lima-colima", command], "sshfs-" + label)
        for _ in range(40):
            result = vm(shlex.join(["mountpoint", "-q", mountpoint]), expected=None)
            if result.returncode == 0:
                return child
            if child.poll() is not None:
                raise RuntimeError("SSHFS exited; inspect log " + label)
            time.sleep(0.1)
        raise RuntimeError("Mount did not become ready")

    async def raw_sftp_checks():
        async with asyncssh.connect("127.0.0.1", port=port, username="fixture",
                                    client_keys=[str(work / "client_key")],
                                    known_hosts=str(work / "sftp_known_hosts")) as conn:
            async with conn.start_sftp_client() as sftp:
                for path in ["../../outside.txt", "/../../outside.txt", "/external-link", "/outside-dir/outside.txt"]:
                    try:
                        async with sftp.open(path) as handle:
                            await handle.read()
                    except asyncssh.SFTPPermissionDenied as exc:
                        check("sftp_denies_" + path, True, str(exc))
                    else:
                        check("sftp_denies_" + path, False, "Read unexpectedly allowed")
            result = await conn.run("uname", check=False)
            check("shell_session_denied", result.exit_status != 0, {"exit_status": result.exit_status})

    try:
        for name in ["host_key", "client_key", "wrong_key"]:
            key = asyncssh.generate_private_key("ssh-ed25519")
            key.write_private_key(work / name)
            os.chmod(work / name, 0o600)
            key.write_public_key(work / (name + ".pub"))
        shared = work / "shared"
        shared.mkdir()
        (shared / "from_mac.txt").write_text("mac-one\n")
        (shared / "readonly.txt").write_text("unchanged\n")
        (shared / "readonly.txt").chmod(0o444)
        (work / "outside.txt").write_text("fixture outside marker\n")
        (shared / "internal-link").symlink_to("from_mac.txt")
        (shared / "external-link").symlink_to(work / "outside.txt")
        (shared / "outside-dir").symlink_to(work)
        server = spawn([sys.executable, str(Path(__file__).with_name("server.py")),
                        "--root", str(shared), "--host-key", str(work / "host_key"),
                        "--client-key", str(work / "client_key.pub"), "--ready", str(work / "ready")], "sftp-server")
        for _ in range(100):
            if (work / "ready").exists():
                break
            if server.poll() is not None:
                raise RuntimeError("SFTP server exited")
            time.sleep(0.05)
        port = int((work / "ready").read_text())
        host_pub = (work / "host_key.pub").read_text().strip()
        (work / "sftp_known_hosts").write_text(f"[127.0.0.1]:{port} {host_pub}\n")
        # The existing Colima management channel is the trust anchor for its VM key.
        vm_key = run(["colima", "ssh", "--", "cat", "/etc/ssh/ssh_host_ed25519_key.pub"]).stdout.strip()
        vm_config = args.ssh_config.read_text()
        vm_port = next(line.split()[1] for line in vm_config.splitlines() if line.strip().startswith("Port "))
        (work / "vm_known_hosts").write_text(f"[127.0.0.1]:{vm_port} {vm_key}\n")
        ssh = ["ssh", "-F", str(args.ssh_config), "-o", "ControlMaster=no", "-o", "ControlPath=none",
               "-o", "StrictHostKeyChecking=yes", "-o", "NoHostAuthenticationForLocalhost=no",
               "-o", "UserKnownHostsFile=" + str(work / "vm_known_hosts")]
        vm(shlex.join(["mkdir", "-p", mountpoint]))
        vm("umask 077; cat > " + shlex.quote(remote + "/client_key"), input=(work / "client_key").read_text())
        vm("cat > " + shlex.quote(remote + "/known_hosts"), input=(work / "sftp_known_hosts").read_text())
        report["versions"] = {"sshfs": vm(shlex.join([args.sshfs, "--version"])).stdout,
                              "linux": vm("uname -a").stdout, "asyncssh": asyncssh.__version__}
        if "3.7.6" not in report["versions"]["sshfs"]:
            raise RuntimeError("This fixture requires the reviewed SSHFS 3.7.6 build")
        tunnel_command = ssh + ["-N", "-o", "ExitOnForwardFailure=yes", "-R",
                                f"127.0.0.1:{port}:127.0.0.1:{port}", "lima-colima"]
        tunnel = spawn(tunnel_command, "reverse-tunnel")
        time.sleep(0.5)
        if tunnel.poll() is not None:
            raise RuntimeError("Tunnel exited")
        mounted = mount("rw")
        evidence = vm(shlex.join(["findmnt", "-J", "-T", mountpoint])).stdout
        check("real_network_mount", "fuse.sshfs" in evidence, json.loads(evidence))
        result = py(f"from pathlib import Path; p=Path({mountpoint!r}); assert (p/'from_mac.txt').read_text()=='mac-one\\n'; (p/'from_linux.txt').write_text('linux-one\\n'); (p/'from_mac.txt').write_text('linux-update\\n')")
        check("mac_to_linux_read_linux_write", result.returncode == 0, "Linux independently asserted Mac content")
        check("linux_to_mac_read", (shared / "from_linux.txt").read_text() == "linux-one\n" and
              (shared / "from_mac.txt").read_text() == "linux-update\n", "Mac independently read both Linux writes")
        (shared / "from_linux.txt").write_text("mac-update\n")
        py(f"from pathlib import Path; p=Path({mountpoint!r}); assert (p/'from_linux.txt').read_text()=='mac-update\\n'; (p/'from_linux.txt').chmod(0o600); assert (p/'internal-link').read_text()=='linux-update\\n'")
        check("live_mac_update_and_internal_link", True, "Linux saw post-mount Mac update and safe relative symlink")
        check("chmod_propagates", (shared / "from_linux.txt").stat().st_mode & 0o777 == 0o600, "Mac mode is 0600 after Linux chmod")
        denial_code = """from pathlib import Path
import errno, json
p=Path(MOUNT)
for name in ['readonly.txt','external-link','outside-dir/outside.txt']:
 try: (p/name).write_text('forbidden')
 except OSError as exc:
  assert exc.errno in [errno.EACCES,errno.EPERM], (name,exc)
  print(json.dumps({'name':name,'errno':exc.errno}))
 else: raise AssertionError(name+' unexpectedly writable')
""".replace("MOUNT", repr(mountpoint))
        result = py(denial_code)
        check("mode_and_external_symlink_denials", True, result.stdout)
        try:
            asyncio.run(raw_sftp_checks())
        except asyncssh.ChannelOpenError as exc:
            check("shell_session_denied", True, str(exc))
        async def wrong_key():
            try:
                await asyncssh.connect("127.0.0.1", port=port, username="fixture", client_keys=[str(work / "wrong_key")], known_hosts=str(work / "sftp_known_hosts"))
            except asyncssh.PermissionDenied:
                return True
            return False
        check("unregistered_key_denied", asyncio.run(wrong_key()), "Second generated key rejected")
        unmount()
        stop(mounted)
        mounted = mount("ro", readonly=True)
        result = py(f"from pathlib import Path; import errno; p=Path({mountpoint!r})/'ro-created';\ntry: p.write_text('x')\nexcept OSError as e: assert e.errno==errno.EROFS; print(e.errno)\nelse: raise AssertionError('read-only mount writable')")
        check("read_only_mount_denies_creation", not (shared / "ro-created").exists(), result.stdout)
        unmount()
        stop(mounted)
        mounted = mount("stall")
        server.send_signal(signal.SIGSTOP)
        failure_code = f"from pathlib import Path; import time,json; t=time.monotonic();\ntry: Path({mountpoint!r}+'/from_mac.txt').read_text()\nexcept OSError as e: print(json.dumps({{'errno':e.errno,'seconds':time.monotonic()-t}}))\nelse: raise AssertionError('operation unexpectedly succeeded')"
        result = py(failure_code, timeout=10)
        stall = json.loads(result.stdout)
        check("stalled_connection_bounded_error", stall["seconds"] < 8 and stall["errno"] in [5, 107], stall)
        server.send_signal(signal.SIGCONT)
        unmount()
        stop(mounted)
        mounted = mount("restored")
        py(f"from pathlib import Path; assert Path({mountpoint!r}+'/from_mac.txt').read_text()=='linux-update\\n'")
        check("explicit_remount_restores_access", True, "Fresh open after remount succeeded")
        stop(tunnel)
        result = py(failure_code, timeout=10)
        interrupted = json.loads(result.stdout)
        mounted_after_break = vm(shlex.join(["mountpoint", "-q", mountpoint]), expected=None).returncode == 0
        interrupted["mount_still_present"] = mounted_after_break
        # SSHFS may terminate and auto-unmount on EOF. ENOENT then originates
        # from the underlying empty directory, not from the remote filesystem.
        acceptable_error = interrupted["errno"] in [5, 107] or (interrupted["errno"] == 2 and not mounted_after_break)
        check("broken_tunnel_bounded_error", interrupted["seconds"] < 8 and acceptable_error, interrupted)
        report["limitations"].append("On tunnel EOF SSHFS may auto-unmount. A manager must gate tools on mount state; otherwise writes can fall through to the underlying local directory.")
        check("outside_and_readonly_unchanged", (work / "outside.txt").read_text() == "fixture outside marker\n" and (shared / "readonly.txt").read_text() == "unchanged\n", "Mac independently verified denial had no side effect")
        success = True
    except BaseException as exc:
        report["error"] = repr(exc)
        print(repr(exc), file=sys.stderr)
    finally:
        if server and server.poll() is None:
            server.send_signal(signal.SIGCONT)
        if ssh:
            try:
                unmount()
            except Exception as exc:
                report["cleanup_error"] = repr(exc)
        for child in reversed(children):
            stop(child)
        if ssh:
            result = vm(shlex.join(["mountpoint", "-q", mountpoint]), expected=None)
            report["cleanup_unmounted"] = result.returncode != 0
            vm(shlex.join(["rm", "-rf", remote]))
        for log in logs:
            log.close()
        shutil.rmtree(work)
        report["cleanup_local_removed"] = not work.exists()
        report["passed"] = success and report.get("cleanup_unmounted", False)
        report["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (args.results / "result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
