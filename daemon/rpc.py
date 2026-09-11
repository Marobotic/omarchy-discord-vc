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
import stat
import struct
import time
import uuid

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2
OP_PING = 3
OP_PONG = 4

# Upper bound on one frame's payload. The length field is a uint32 the peer
# controls, so without a cap a single header could make us try to buffer 4 GiB.
# Real frames are far smaller -- a full voice channel's roster is tens of KiB --
# and an oversized one is treated as a broken connection, not data.
MAX_FRAME = 1 << 20

# Once a frame's header starts arriving, the rest of it must follow within
# this many seconds. Real frames arrive in one burst; a peer that trickles
# bytes to hold us mid-frame is dropped instead of waited on.
FRAME_READ_SECONDS = 5.0

# The same for writes. A peer that stops reading -- say, while flooding us with
# PINGs it never collects the PONGs for -- fills the socket buffer; without a
# bound, the next send would block instead of the connection being dropped.
FRAME_WRITE_SECONDS = 5.0

# Events that arrive while a command is waiting for its reply are parked for
# the pump, but only this many and only this much in total. Normal traffic
# parks a handful at most, since replies take milliseconds; overflowing means
# the peer is flooding, and the connection is dropped rather than buffered.
MAX_PENDING_EVENTS = 256
MAX_PENDING_BYTES = 1 << 20

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
    """Candidate IPC socket paths, in the order Discord itself probes them.

    Only locations inside the per-user runtime directory are searched. Discord
    will also fall back to /tmp when it has no runtime directory, but /tmp is
    world-writable: any local user could park a fake discord-ipc socket there
    and collect the access token we authenticate with. Every candidate is also
    checked in connect() -- see _verify_peer().
    """
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    roots = [
        base,
        os.path.join(base, "app", "com.discordapp.Discord"),
        os.path.join(base, "snap.discord"),
    ]
    for root in roots:
        for i in range(10):
            yield os.path.join(root, f"discord-ipc-{i}")


def _verify_peer(path, sock):
    """Refuse a socket unless we own it and our own uid is serving it.

    The file check rejects a socket someone else created; SO_PEERCRED then
    asks the kernel which uid is on the far end of the connection, which a
    planted or swapped socket cannot fake. Both must match before any
    credentials cross the wire.
    """
    st = os.lstat(path)
    if not stat.S_ISSOCK(st.st_mode):
        raise RPCError(f"{path} is not a socket")
    if st.st_uid != os.getuid():
        raise RPCError(f"{path} is owned by uid {st.st_uid}, not us")
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                            struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    if uid != os.getuid():
        raise RPCError(f"{path} is served by uid {uid}, not us")


def _deadline(timeout):
    return None if timeout is None else time.monotonic() + timeout


def _remaining(deadline):
    return None if deadline is None else max(0.0, deadline - time.monotonic())


class RPC:
    def __init__(self, client_id=DEFAULT_CLIENT_ID):
        self.client_id = client_id
        self.sock = None
        self.user = None
        # Events that arrive while we are blocked waiting on a command
        # reply, as (payload, size) pairs, bounded by MAX_PENDING_*.
        self._pending_events = []
        self._pending_bytes = 0

    # -- connection ---------------------------------------------------------

    def connect(self):
        last = None
        for path in socket_paths():
            if not os.path.exists(path):
                continue
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Bounded even here: connect() on a unix socket blocks while the
            # listener's backlog is full.
            s.settimeout(FRAME_WRITE_SECONDS)
            try:
                s.connect(path)
                _verify_peer(path, s)
            except (OSError, RPCError) as exc:
                last = exc
                s.close()
                continue
            # From here on every read and write waits in select() against an
            # explicit deadline; nothing is left to block on the socket itself.
            s.setblocking(False)
            self.sock = s
            self._pending_events = []
            self._pending_bytes = 0
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
        """Write one frame within FRAME_WRITE_SECONDS, or drop the connection."""
        if self.sock is None:
            raise Closed("not connected")
        body = json.dumps(payload).encode("utf-8")
        data = memoryview(struct.pack("<II", op, len(body)) + body)
        deadline = time.monotonic() + FRAME_WRITE_SECONDS
        while data:
            left = _remaining(deadline)
            _, w, _ = select.select([], [self.sock], [], left) if left else ([], [], [])
            if not w:
                raise self._drop("peer stopped reading; send timed out")
            try:
                sent = self.sock.send(data)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise self._drop(str(exc)) from exc
            data = data[sent:]

    def _drop(self, reason):
        """Close the connection and return the error to raise.

        Every bound violation goes through here. Closing matters as much as
        raising: Closed is an RPCError, which some callers deliberately
        swallow, and a closed socket guarantees the next read fails with
        "not connected" and the daemon reconnects instead of carrying on
        with a stream it has stopped trusting.
        """
        self.close()
        return Closed(reason)

    def _read_exact(self, n, deadline):
        """Read exactly n bytes before `deadline`, or drop the connection.

        Only called mid-frame, where giving up leaves the stream at an
        unknown offset -- so running out of time is a disconnect, never a
        recoverable timeout.
        """
        out = bytearray()
        while len(out) < n:
            left = _remaining(deadline)
            if left <= 0:
                raise self._drop("peer stalled partway through a frame")
            r, _, _ = select.select([self.sock], [], [], left)
            if not r:
                raise self._drop("peer stalled partway through a frame")
            try:
                chunk = self.sock.recv(min(n - len(out), 65536))
            except BlockingIOError:
                continue
            except OSError as exc:
                raise self._drop(str(exc)) from exc
            if not chunk:
                raise self._drop("socket closed by peer")
            out += chunk
        return bytes(out)

    def _read_frame(self, deadline):
        """Read one frame by `deadline`: (op, payload, size), or None.

        The deadline is absolute. Handling a PING does not extend it, so a
        peer that keeps pinging cannot hold a caller past its timeout.
        """
        if self.sock is None:
            raise Closed("not connected")
        # A loop rather than recursion for PING: the peer decides how many
        # PINGs arrive in a row, so it must not decide our stack depth.
        while True:
            r, _, _ = select.select([self.sock], [], [], _remaining(deadline))
            if not r:
                return None
            within = time.monotonic() + FRAME_READ_SECONDS
            op, length = struct.unpack("<II", self._read_exact(8, within))
            if length > MAX_FRAME:
                raise self._drop(f"frame of {length} bytes exceeds the "
                                 f"{MAX_FRAME}-byte limit")
            body = self._read_exact(length, within) if length else b"{}"
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise self._drop(f"bad json frame: {exc}") from exc
            if not isinstance(payload, dict):
                raise self._drop("frame payload is not a JSON object")
            if op == OP_PING:
                self._send(OP_PONG, payload)
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                continue
            if op == OP_CLOSE:
                raise self._drop(f"discord closed the connection: {payload}")
            return op, payload, 8 + length

    def read_frame(self, timeout=None):
        """Read one frame. Returns (op, payload) or None on timeout."""
        frame = self._read_frame(_deadline(timeout))
        return None if frame is None else frame[:2]

    def _park(self, payload, size):
        """Keep an event that overtook a reply, within the pending budget."""
        if (len(self._pending_events) >= MAX_PENDING_EVENTS
                or self._pending_bytes + size > MAX_PENDING_BYTES):
            raise self._drop(
                f"more than {MAX_PENDING_EVENTS} events or "
                f"{MAX_PENDING_BYTES} bytes arrived while waiting for a reply")
        self._pending_events.append((payload, size))
        self._pending_bytes += size

    def _await_reply(self, nonce, what, timeout):
        """Wait for the frame carrying `nonce`, by one absolute deadline.

        Every other frame is parked for next_event(). Neither a stream of
        unrelated events nor PINGs can keep this waiting past `timeout`,
        and the parked backlog is bounded by _park().
        """
        deadline = _deadline(timeout)
        while True:
            frame = self._read_frame(deadline)
            if frame is None:
                raise TimeoutError(f"{what} timed out")
            _, payload, size = frame
            if payload.get("nonce") != nonce:
                self._park(payload, size)
                continue
            if payload.get("evt") == "ERROR":
                data = payload.get("data") or {}
                raise RPCError(
                    f"{what}: {data.get('message', 'unknown')} "
                    f"(code {data.get('code')})"
                )
            return payload

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
        return self._await_reply(nonce, cmd, timeout).get("data") or {}

    def authenticate(self, access_token, timeout=10.0):
        return self.request("AUTHENTICATE", {"access_token": access_token},
                            timeout=timeout)

    def subscribe(self, evt, args=None, timeout=10.0):
        nonce = str(uuid.uuid4())
        self._send(OP_FRAME, {"cmd": "SUBSCRIBE", "evt": evt,
                              "args": args or {}, "nonce": nonce})
        self._await_reply(nonce, f"SUBSCRIBE {evt}", timeout)
        return True

    def unsubscribe(self, evt, args=None):
        self._send(OP_FRAME, {"cmd": "UNSUBSCRIBE", "evt": evt,
                              "args": args or {}, "nonce": str(uuid.uuid4())})

    def next_event(self, timeout=1.0):
        """Return the next dispatched event payload, or None on timeout.

        Parked events drain first. Otherwise this reads at most one event
        frame, by an absolute deadline, so the caller gets control back at
        least every `timeout` seconds however fast the peer sends.
        """
        if self._pending_events:
            payload, size = self._pending_events.pop(0)
            self._pending_bytes -= size
            return payload
        frame = self._read_frame(_deadline(timeout))
        return None if frame is None else frame[1]
