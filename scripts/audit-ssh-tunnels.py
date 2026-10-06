#!/usr/bin/env python3
"""Read-only Linux audit of legacy fastagent SSH listeners.

The same pathname can name many kernel sockets after repeated unlinks. A
socket diagnostic query maps each kernel listener to its filesystem inode,
identifying the one still reachable.
This is a snapshot for review, not authorization to signal any process.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path
import re
import socket
import struct


def unix_sockets(proc: Path) -> dict[str, tuple[str, str, str]]:
    with (proc / "net/unix").open() as f:
        if f.readline().split() != "Num RefCount Protocol Flags Type St Inode Path".split():
            raise RuntimeError("unrecognized /proc/net/unix header")
        records = {}
        for line in f:
            fields = line.split(maxsplit=7)
            if len(fields) < 7:
                raise RuntimeError(f"malformed /proc/net/unix record: {line!r}")
            if fields[4] == "0001":  # SOCK_STREAM
                records[fields[6]] = (fields[3], fields[5], fields[7].strip() if len(fields) == 8 else "")
        return records


def forwarding_path(argv: list[str], uid: int) -> str | None:
    """Recognize the exact -f -N -L command emitted by the legacy plugin."""
    if "-f" not in argv or "-N" not in argv:
        return None
    for i, arg in enumerate(argv[:-1]):
        if arg != "-L":
            continue
        local, sep, remote = argv[i + 1].partition(":")
        if (sep and re.fullmatch(rf"/tmp/fastagent-local-{uid}-[0-9a-f]{{32}}\.sock", local)
                and re.fullmatch(r"/tmp/fastagent-(?:root-)?[^/:]+\.sock", remote)):
            return local
    return None


def listener_files() -> dict[str, tuple[int, int]]:
    """Map kernel socket inodes to (filesystem inode, kernel device).

    SO_PEERCRED is unsuitable here: an ssh -f listener is bound before
    the fork, so the returned PID can be the already-exited parent.
    UNIX_DIAG_VFS records the actual inode held by each listener, even
    after unlink, and does not establish a forwarded connection.
    """
    # Linux uapi: NETLINK_SOCK_DIAG=4, SOCK_DIAG_BY_FAMILY=20,
    # NLM_F_REQUEST|NLM_F_DUMP=0x301, TCP_LISTEN=10, UDIAG_SHOW_VFS=2.
    request = struct.pack("=BBHIIIII", socket.AF_UNIX, 0, 0, 1 << 10, 0, 2, 0xffffffff, 0xffffffff)
    header = struct.pack("=IHHII", 16 + len(request), 20, 0x301, 1, 0)
    records = {}
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4) as diagnostic:
        diagnostic.settimeout(5)
        diagnostic.sendto(header + request, (0, 0))
        while True:
            packet, _, flags, _ = diagnostic.recvmsg(1024 * 1024)
            if flags & socket.MSG_TRUNC:
                raise RuntimeError("truncated socket diagnostic response")
            offset = 0
            while offset < len(packet):
                length, kind, message_flags, sequence, _ = struct.unpack_from("=IHHII", packet, offset)
                if length < 16 or offset + length > len(packet) or sequence != 1:
                    raise RuntimeError("malformed socket diagnostic response")
                if message_flags & 0x10:  # NLM_F_DUMP_INTR
                    raise RuntimeError("socket diagnostic snapshot interrupted; retry")
                body = packet[offset + 16:offset + length]
                if kind == 3:  # NLMSG_DONE
                    return records
                if kind == 2:  # NLMSG_ERROR
                    error = struct.unpack_from("=i", body)[0]
                    raise OSError(-error, os.strerror(-error))
                if kind != 20 or len(body) < 16:
                    raise RuntimeError("unexpected socket diagnostic response")
                _, socktype, _, _, inode, _, _ = struct.unpack_from("=BBBBIII", body)
                attr = 16
                while attr < len(body):
                    size, field = struct.unpack_from("=HH", body, attr)
                    if size < 4 or attr + size > len(body):
                        raise RuntimeError("malformed socket diagnostic attribute")
                    if field == 1 and socktype == socket.SOCK_STREAM:  # UNIX_DIAG_VFS
                        records[str(inode)] = struct.unpack_from("=II", body, attr + 4)
                    attr += (size + 3) & ~3
                offset += (length + 3) & ~3


def audit(proc: Path = Path("/proc")) -> list[dict]:
    uid = os.getuid()
    records = unix_sockets(proc)
    rows = []
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != uid:
                continue
            argv = directory.joinpath("cmdline").read_bytes().decode().rstrip("\0").split("\0")
        except FileNotFoundError:
            continue  # A process may exit during the snapshot.
        path = forwarding_path(argv, uid)
        if path is None:
            continue
        try:
            # An upgraded SSH binary can leave older processes running
            # from the old inode, displayed by procfs with this suffix.
            executable = os.readlink(directory / "exe").removesuffix(" (deleted)")
            if Path(executable).name != "ssh":
                raise RuntimeError(f"PID {directory.name}: fastagent forwarding arguments on a non-SSH executable")
            held = set()
            for fd in directory.joinpath("fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:
                    continue
                if target.startswith("socket:[") and target.endswith("]"):
                    held.add(target[8:-1])
            listeners = sorted(inode for inode in held if records.get(inode) == ("00010000", "01", path))
            # Preserve any connected Unix stream, including shared master
            # sessions. The SSH transport TCP connection is always present
            # and does not itself mean a forwarded RPC is active.
            active = sorted(inode for inode in held if inode in records and records[inode][1] != "01")
            status = dict(line.split(":", 1) for line in directory.joinpath("status").read_text().splitlines())
        except FileNotFoundError:
            continue
        rows.append({"pid": int(directory.name), "path": path, "listeners": listeners,
                     "active": active, "rss_kib": int(status.get("VmRSS", "0 kB").split()[0])})

    files = listener_files()
    current = {}
    for path in {row["path"] for row in rows}:
        try:
            info = os.stat(path)
        except FileNotFoundError:
            current[path] = None
        else:
            current[path] = (info.st_ino & 0xffffffff, (os.major(info.st_dev) << 20) | os.minor(info.st_dev))
    for row in rows:
        if current[row["path"]] is not None and any(
                files.get(inode) == current[row["path"]] for inode in row["listeners"]):
            row["decision"] = "preserve: reachable"
        elif row["active"]:
            row["decision"] = "preserve: active"
        elif not row["listeners"] or any(inode not in files for inode in row["listeners"]):
            row["decision"] = "preserve: listener ownership unverified"
        else:
            row["decision"] = "candidate: unreachable and idle"
    return sorted(rows, key=lambda row: (row["path"], row["pid"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the full snapshot as JSON")
    args = parser.parse_args()
    if not hasattr(socket, "AF_NETLINK") or not Path("/proc/net/unix").exists():
        parser.error("this audit requires Linux with readable /proc and socket diagnostics")
    rows = audit()
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print(f"{len(rows)} verified SSH processes, {len({r['path'] for r in rows})} forwarding paths, "
          f"{sum(r['rss_kib'] for r in rows) / 1024:.1f} MiB RSS")
    for decision, count in sorted(collections.Counter(r["decision"] for r in rows).items()):
        print(f"{count}: {decision}")
    for row in rows:
        print(f"{row['pid']}: {row['decision']}: {row['path']}")
    print("Read-only snapshot. Recheck ownership and activity before stopping any candidate.")


if __name__ == "__main__":
    main()
