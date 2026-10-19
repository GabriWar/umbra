"""Unlock channel between the human's terminal and running umbra servers.

Each server listens on a unix socket in a 0700 per-user directory. The CLI
(`python -m umbra vault unlock`) reads the passphrase with getpass and sends
it to every live server. The socket only accepts unlock/lock/status and
never answers with anything stored in the vault.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import tempfile
from pathlib import Path
from typing import Any

from umbra.vault import store


def socket_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    d = Path(base) / f"umbra-{os.getuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _handle(req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    if op == "unlock":
        try:
            until = store.unlock(str(req.get("passphrase", "")), float(req.get("minutes", 30)))
        except store.VaultError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "until": int(until)}
    if op == "lock":
        store.lock()
        return {"ok": True}
    if op == "status":
        return {"ok": True, "unlocked": store.unlocked_vault() is not None}
    return {"ok": False, "error": f"unknown op {op!r}"}


async def serve() -> asyncio.AbstractServer:
    path = socket_dir() / f"vault-{os.getpid()}.sock"
    path.unlink(missing_ok=True)

    async def on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            req = json.loads(line or b"{}")
            # PBKDF2 is ~0.5 s of CPU; keep it off the event loop.
            resp = await asyncio.to_thread(_handle, req if isinstance(req, dict) else {})
        except (ValueError, asyncio.TimeoutError) as e:
            resp = {"ok": False, "error": f"bad request: {type(e).__name__}"}
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()

    srv = await asyncio.start_unix_server(on_conn, path=str(path))
    os.chmod(path, 0o600)
    return srv


async def stop(srv: asyncio.AbstractServer) -> None:
    srv.close()
    await srv.wait_closed()
    (socket_dir() / f"vault-{os.getpid()}.sock").unlink(missing_ok=True)


def is_umbra_server(pid: int) -> bool:
    """Does this process look like an umbra server (by its argv)?"""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    joined = b" ".join(argv).decode(errors="replace")
    return any(m in joined for m in ("umbra.server", "umbra-server", "-m umbra server",
                                       "-m umbra serve"))


def _peer_pid(s: socket.socket) -> tuple[int, int]:
    pid, uid, _gid = struct.unpack("3i", s.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
    return pid, uid


def broadcast(req: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """Send `req` to every live server; drops sockets of dead processes.

    The listening process is identified by SO_PEERCRED, not by the pid in
    the file name, and must be an umbra server of this uid before anything
    is sent — any same-user process can create a socket in this directory."""
    out: list[tuple[int, dict[str, Any]]] = []
    for sock_path in sorted(socket_dir().glob("vault-*.sock")):
        pid = int(sock_path.stem.split("-", 1)[1])
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            sock_path.unlink(missing_ok=True)
            continue
        except PermissionError:
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(30)
                s.connect(str(sock_path))
                peer, uid = _peer_pid(s)
                if uid != os.getuid() or peer != pid or not is_umbra_server(peer):
                    out.append((peer, {"ok": False, "error": "refused: the process behind "
                                       f"{sock_path.name} is not an umbra server; nothing sent"}))
                    continue
                s.sendall(json.dumps(req).encode() + b"\n")
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            out.append((pid, json.loads(buf or b"{}")))
        except OSError as e:
            out.append((pid, {"ok": False, "error": str(e)}))
    return out
