"""Minimal Discord local-RPC client (stdlib only).

Discord's desktop client listens on a unix socket at
$XDG_RUNTIME_DIR/discord-ipc-<n>. Frames are:

    <op:uint32 LE><length:uint32 LE><json payload>

ops: 0 HANDSHAKE, 1 FRAME, 2 CLOSE, 3 PING, 4 PONG.

Only the pieces this plugin needs are implemented: handshake, authenticate,
request/response by nonce, subscribe, and an event pump.
"""

import json
import os
import select
import socket
import struct
import uuid

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

# Discord Streamkit's public application. It is whitelisted by Discord for the
# rpc / rpc.voice.read scopes, which is what lets a local overlay read voice
# state without the user registering their own application. Overridable for
# people who would rather use their own app (see auth.py).
DEFAULT_CLIENT_ID = "207646673902501888"


class RPCError(Exception):
    pass


class Closed(RPCError):
    pass


def socket_paths():
    """Candidate IPC socket paths, in the order Discord itself probes them."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    roots = [
        base,
        os.path.join(base, "app", "com.discordapp.Discord"),
        os.path.join(base, "snap.discord"),
        "/tmp",
    ]
    for root in roots:
        for i in range(10):
            yield os.path.join(root, f"discord-ipc-{i}")


class RPC:
    def __init__(self, client_id=DEFAULT_CLIENT_ID):
        self.client_id = client_id
        self.sock = None
        self.user = None
        self._buf = b""
        # Events that arrive while we are blocked waiting on a command reply.
        self._pending_events = []

    # -- connection ---------------------------------------------------------

    def connect(self):
        last = None
        for path in socket_paths():
            if not os.path.exists(path):
                continue
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(path)
            except OSError as exc:
                last = exc
                s.close()
                continue
            # select() decides when to read; this only stops a truncated
            # frame from parking recv() forever. A timeout surfaces as OSError
            # and the daemon reconnects rather than going silently stale.
            s.settimeout(30.0)
            self.sock = s
            self._buf = b""
            self._pending_events = []
            return path
        raise Closed(f"no reachable discord-ipc socket ({last})")

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    # -- framing ------------------------------------------------------------

    def _send(self, op, payload):
        if self.sock is None:
            raise Closed("not connected")
        body = json.dumps(payload).encode("utf-8")
        try:
            self.sock.sendall(struct.pack("<II", op, len(body)) + body)
        except OSError as exc:
            raise Closed(str(exc)) from exc

    def _read_exact(self, n, deadline_select):
        """Read exactly n bytes, honouring a select-based timeout."""
        out = b""
        while len(out) < n:
            if not deadline_select():
                raise TimeoutError("rpc read timed out")
            try:
                chunk = self.sock.recv(n - len(out))
            except OSError as exc:
                raise Closed(str(exc)) from exc
            if not chunk:
                raise Closed("socket closed by peer")
            out += chunk
        return out

    def read_frame(self, timeout=None):
        """Read one frame. Returns (op, payload) or None on timeout."""
        if self.sock is None:
            raise Closed("not connected")

        def ready():
            r, _, _ = select.select([self.sock], [], [], timeout)
            return bool(r)

        if not ready():
            return None
        header = self._read_exact(8, lambda: True)
        op, length = struct.unpack("<II", header)
        body = self._read_exact(length, lambda: True) if length else b"{}"
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise RPCError(f"bad json frame: {exc}") from exc
        if op == OP_PING:
            self._send(OP_PONG, payload)
            return self.read_frame(timeout)
        if op == OP_CLOSE:
            raise Closed(f"discord closed the connection: {payload}")
        return op, payload

    # -- protocol -----------------------------------------------------------

    def handshake(self, timeout=10.0):
        self._send(OP_HANDSHAKE, {"v": 1, "client_id": self.client_id})
        frame = self.read_frame(timeout)
        if frame is None:
            raise TimeoutError("handshake timed out")
        _, payload = frame
        if payload.get("evt") != "READY":
            raise RPCError(f"unexpected handshake reply: {payload}")
        self.user = (payload.get("data") or {}).get("user") or {}
        return self.user

    def request(self, cmd, args=None, timeout=10.0):
        """Send a command and wait for the reply with the matching nonce."""
        nonce = str(uuid.uuid4())
        self._send(OP_FRAME, {"cmd": cmd, "args": args or {}, "nonce": nonce})
        while True:
            frame = self.read_frame(timeout)
            if frame is None:
                raise TimeoutError(f"{cmd} timed out")
            _, payload = frame
            if payload.get("nonce") != nonce:
                # An event overtook our reply; keep it for the pump.
                self._pending_events.append(payload)
                continue
            if payload.get("evt") == "ERROR":
                data = payload.get("data") or {}
                raise RPCError(
                    f"{cmd}: {data.get('message', 'unknown')} "
                    f"(code {data.get('code')})"
                )
            return payload.get("data") or {}

    def authenticate(self, access_token, timeout=10.0):
        return self.request("AUTHENTICATE", {"access_token": access_token},
                            timeout=timeout)

    def subscribe(self, evt, args=None, timeout=10.0):
        nonce = str(uuid.uuid4())
        self._send(OP_FRAME, {"cmd": "SUBSCRIBE", "evt": evt,
                              "args": args or {}, "nonce": nonce})
        while True:
            frame = self.read_frame(timeout)
            if frame is None:
                raise TimeoutError(f"SUBSCRIBE {evt} timed out")
            _, payload = frame
            if payload.get("nonce") != nonce:
                self._pending_events.append(payload)
                continue
            if payload.get("evt") == "ERROR":
                data = payload.get("data") or {}
                raise RPCError(
                    f"SUBSCRIBE {evt}: {data.get('message', 'unknown')} "
                    f"(code {data.get('code')})"
                )
            return True

    def unsubscribe(self, evt, args=None):
        self._send(OP_FRAME, {"cmd": "UNSUBSCRIBE", "evt": evt,
                              "args": args or {}, "nonce": str(uuid.uuid4())})

    def next_event(self, timeout=1.0):
        """Return the next dispatched event payload, or None on timeout."""
        if self._pending_events:
            return self._pending_events.pop(0)
        frame = self.read_frame(timeout)
        if frame is None:
            return None
        return frame[1]
