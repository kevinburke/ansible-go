"""Check that the Linux tunnel audit preserves reachable and active owners."""

import importlib.util
import os
from pathlib import Path
import socket
import struct
import tempfile
import types
import unittest
from unittest import mock


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit-ssh-tunnels.py"
_SPEC = importlib.util.spec_from_file_location("tunnel_audit", _SCRIPT)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


class TestTunnelAudit(unittest.TestCase):
    def test_reachable_active_and_unverified_listeners_are_preserved(self):
        uid = os.getuid()
        local = f"/tmp/fastagent-local-{uid}-{'a' * 32}.sock"
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            proc.joinpath("net").mkdir()
            records = ["Num RefCount Protocol Flags Type St Inode Path"]
            for pid, inode in ((101, 201), (102, 202), (103, 203), (104, 204)):
                process = proc / str(pid)
                process.joinpath("fd").mkdir(parents=True)
                process.joinpath("exe").symlink_to("/usr/local/bin/ssh (deleted)")
                process.joinpath("cmdline").write_bytes(
                    f"ssh\0-f\0-N\0-L\0{local}:/tmp/fastagent-0.11.0-abc.sock\0host\0".encode())
                process.joinpath("status").write_text("VmRSS:\t1024 kB\n")
                process.joinpath("fd/3").symlink_to(f"socket:[{inode}]")
                records.append(f"0: 00000002 00000000 00010000 0001 01 {inode} {local}")
            proc.joinpath("103/fd/4").symlink_to("socket:[205]")
            records.append(f"0: 00000002 00000000 00000000 0001 03 205 {local}")
            proc.joinpath("net/unix").write_text("\n".join(records) + "\n")
            real_stat = os.stat

            def stat(path, *args, **kwargs):
                if str(path) == local:
                    return types.SimpleNamespace(st_ino=301, st_dev=os.makedev(8, 1))
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(audit, "listener_files", return_value={
                "201": (301, (8 << 20) | 1),  # Current pathname.
                "202": (302, (8 << 20) | 1),  # Unlinked idle duplicate.
                "203": (303, (8 << 20) | 1),  # Unlinked but active.
                # 204 is absent from diagnostics: ownership is unverified.
            }), mock.patch.object(audit.os, "stat", side_effect=stat):
                rows = audit.audit(proc)
            self.assertEqual({row["pid"]: row["decision"] for row in rows}, {
                101: "preserve: reachable", 102: "candidate: unreachable and idle",
                103: "preserve: active", 104: "preserve: listener ownership unverified",
            })

    def test_other_users_and_other_forwarding_paths_are_not_recognized(self):
        local = f"/tmp/fastagent-local-{os.getuid()}-{'a' * 32}.sock"
        for argv in (["ssh", "-L", local + ":/tmp/remote"],
                     ["ssh", "-f", "-N", "-L", "/tmp/other:/tmp/remote"],
                     ["ssh", "-f", "-N", "-L", local + ":localhost:22"]):
            with self.subTest(argv=argv):
                self.assertIsNone(audit.forwarding_path(argv, os.getuid()))

    def test_socket_diagnostics_maps_kernel_inode_to_filesystem_inode(self):
        # UNIX_DIAG_VFS contains filesystem identity, not SO_PEERCRED's
        # pre-fork PID. Include another attribute to exercise alignment.
        body = struct.pack("=BBBBIII", socket.AF_UNIX, socket.SOCK_STREAM, 10, 0, 201, 0, 0)
        body += struct.pack("=HHBxxx", 5, 6, 0)
        body += struct.pack("=HHII", 12, 1, 301, (8 << 20) | 1)
        packet = struct.pack("=IHHII", len(body) + 16, 20, 2, 1, 0) + body
        packet += struct.pack("=IHHII", 16, 3, 2, 1, 0)
        with mock.patch.object(audit.socket, "AF_NETLINK", 16, create=True), \
             mock.patch.object(audit.socket, "socket") as socket_factory:
            socket_factory.return_value.__enter__.return_value.recvmsg.return_value = (packet, [], 0, None)
            self.assertEqual(audit.listener_files(), {"201": (301, (8 << 20) | 1)})

    def test_interrupted_or_failed_diagnostic_dump_fails_visibly(self):
        for packet in (struct.pack("=IHHII", 16, 3, 0x10, 1, 0),
                       struct.pack("=IHHIIi", 20, 2, 0, 1, 0, -1)):
            with self.subTest(packet=packet), \
                 mock.patch.object(audit.socket, "AF_NETLINK", 16, create=True), \
                 mock.patch.object(audit.socket, "socket") as socket_factory:
                socket_factory.return_value.__enter__.return_value.recvmsg.return_value = (packet, [], 0, None)
                with self.assertRaises((OSError, RuntimeError)):
                    audit.listener_files()


if __name__ == "__main__":
    unittest.main()
