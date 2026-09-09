#!/usr/bin/env python3
"""Publish Discord voice-call state for the Omarchy bar widget.

Talks to the Discord desktop client over its local RPC socket and keeps a
small JSON file up to date:

    $XDG_RUNTIME_DIR/omarchy-discord-vc.json

The widget watches that file. Everything reported here is Discord's own view
of the call -- the ping is the client's measured RTT to the voice server, and
the mic indicator follows Discord's voice detection (after its gate, noise
suppression and push-to-talk), not the local PipeWire source.
"""

import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auth import load_token  # noqa: E402
from rpc import RPC, Closed, RPCError  # noqa: E402

RECONNECT_DELAY = 3.0
# Republish at least this often so the widget can tell a live daemon from a
# stale file left behind by a crash.
HEARTBEAT = 5.0

CONNECTED_STATES = {
    "VOICE_CONNECTED", "CONNECTED", "VOICE_CONNECTING", "CONNECTING",
    "AUTHENTICATING", "AWAITING_ENDPOINT", "ICE_CHECKING", "NO_ROUTE",
}
LIVE_STATES = {"VOICE_CONNECTED", "CONNECTED"}


def state_file():
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(base, "omarchy-discord-vc.json")


class Publisher:
    """Atomically writes the widget's state file, skipping no-op writes."""

    def __init__(self):
        self.path = state_file()
        self._last_payload = None
        self._last_write = 0.0

    def write(self, payload, force=False):
        now = time.time()
        comparable = dict(payload)
        comparable.pop("updated", None)
        if (not force and comparable == self._last_payload
                and now - self._last_write < HEARTBEAT):
            return
        self._last_payload = comparable
        self._last_write = now
        payload = dict(payload, updated=int(now))
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


class Session:
    """One authenticated RPC connection's worth of voice state."""

    def __init__(self, rpc, publisher):
        self.rpc = rpc
        self.pub = publisher
        self.self_id = str((rpc.user or {}).get("id") or "")
        self.self_name = ((rpc.user or {}).get("global_name")
                          or (rpc.user or {}).get("username") or "me")

        self.channel_id = None
        self.channel_name = ""
        self.guild_id = None
        self.guild_name = ""
        self.hostname = ""
        self.conn_state = "DISCONNECTED"
        self.ping = None
        self.avg_ping = None

        self.members = {}          # user_id -> display name
        self.speaking = set()      # user ids currently transmitting
        self.last_speaker_id = ""
        self.last_speaker_name = ""

        self.mute = False
        self.deaf = False
        self._guild_names = {}

    # -- naming -------------------------------------------------------------

    @staticmethod
    def _display_name(voice_state):
        user = voice_state.get("user") or {}
        return (voice_state.get("nick")
                or user.get("global_name")
                or user.get("username")
                or "someone")

    def name_for(self, user_id):
        user_id = str(user_id)
        if user_id == self.self_id:
            return self.self_name
        return self.members.get(user_id, "")

    # -- channel tracking ---------------------------------------------------

    def _subscribe_channel(self, channel_id):
        for evt in ("SPEAKING_START", "SPEAKING_STOP",
                    "VOICE_STATE_CREATE", "VOICE_STATE_UPDATE",
                    "VOICE_STATE_DELETE"):
            try:
                self.rpc.subscribe(evt, {"channel_id": channel_id})
            except RPCError:
                # A missing per-channel event is survivable; the rest of the
                # widget keeps working without it.
                pass

    def _unsubscribe_channel(self, channel_id):
        for evt in ("SPEAKING_START", "SPEAKING_STOP",
                    "VOICE_STATE_CREATE", "VOICE_STATE_UPDATE",
                    "VOICE_STATE_DELETE"):
            try:
                self.rpc.unsubscribe(evt, {"channel_id": channel_id})
            except (RPCError, Closed):
                pass

    def _guild_name(self, guild_id):
        if not guild_id:
            return ""
        if guild_id in self._guild_names:
            return self._guild_names[guild_id]
        name = ""
        try:
            data = self.rpc.request("GET_GUILD", {"guild_id": guild_id},
                                    timeout=8.0)
            name = data.get("name") or ""
        except (RPCError, TimeoutError):
            name = ""
        self._guild_names[guild_id] = name
        return name

    def load_channel(self, channel):
        """Adopt the channel returned by GET_SELECTED_VOICE_CHANNEL."""
        if not channel:
            if self.channel_id:
                self._unsubscribe_channel(self.channel_id)
            self.channel_id = None
            self.channel_name = ""
            self.guild_id = None
            self.guild_name = ""
            self.members = {}
            self.speaking.clear()
            self.last_speaker_id = ""
            self.last_speaker_name = ""
            return

        channel_id = str(channel.get("id") or "")
        if channel_id != self.channel_id:
            if self.channel_id:
                self._unsubscribe_channel(self.channel_id)
            self.speaking.clear()
            self.last_speaker_id = ""
            self.last_speaker_name = ""
            self.channel_id = channel_id
            if channel_id:
                self._subscribe_channel(channel_id)

        self.channel_name = channel.get("name") or ""
        self.guild_id = channel.get("guild_id")
        self.guild_name = self._guild_name(self.guild_id)
        self.members = {}
        for vs in channel.get("voice_states") or []:
            user = vs.get("user") or {}
            uid = str(user.get("id") or "")
            if uid:
                self.members[uid] = self._display_name(vs)

    def refresh_channel(self):
        try:
            data = self.rpc.request("GET_SELECTED_VOICE_CHANNEL", {},
                                    timeout=8.0)
        except (RPCError, TimeoutError):
            return
        self.load_channel(data or None)

    def refresh_voice_settings(self):
        try:
            data = self.rpc.request("GET_VOICE_SETTINGS", {}, timeout=8.0)
        except (RPCError, TimeoutError):
            return
        self.mute = bool(data.get("mute"))
        self.deaf = bool(data.get("deaf"))

    # -- events -------------------------------------------------------------

    def handle(self, payload):
        evt = payload.get("evt")
        data = payload.get("data") or {}

        if evt == "VOICE_CONNECTION_STATUS":
            self.conn_state = data.get("state") or "DISCONNECTED"
            self.hostname = data.get("hostname") or ""
            last = data.get("last_ping")
            avg = data.get("average_ping")
            self.ping = int(last) if isinstance(last, (int, float)) else None
            self.avg_ping = int(avg) if isinstance(avg, (int, float)) else None
            if self.conn_state not in CONNECTED_STATES:
                self.speaking.clear()

        elif evt == "VOICE_CHANNEL_SELECT":
            channel_id = data.get("channel_id")
            if not channel_id:
                self.load_channel(None)
            else:
                self.refresh_channel()

        elif evt == "VOICE_SETTINGS_UPDATE":
            self.mute = bool(data.get("mute"))
            self.deaf = bool(data.get("deaf"))

        elif evt == "SPEAKING_START":
            uid = str(data.get("user_id") or "")
            if uid:
                self.speaking.add(uid)
                name = self.name_for(uid)
                if not name:
                    # A member we have not seen yet -- resync the roster.
                    self.refresh_channel()
                    name = self.name_for(uid) or "someone"
                self.last_speaker_id = uid
                self.last_speaker_name = name

        elif evt == "SPEAKING_STOP":
            self.speaking.discard(str(data.get("user_id") or ""))

        elif evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE"):
            user = data.get("user") or {}
            uid = str(user.get("id") or "")
            if uid:
                self.members[uid] = self._display_name(data)

        elif evt == "VOICE_STATE_DELETE":
            user = data.get("user") or {}
            uid = str(user.get("id") or "")
            self.members.pop(uid, None)
            self.speaking.discard(uid)

    # -- output -------------------------------------------------------------

    def snapshot(self):
        connected = (self.channel_id is not None
                     and self.conn_state in CONNECTED_STATES)
        live = self.conn_state in LIVE_STATES
        others_speaking = [u for u in self.speaking if u != self.self_id]
        if others_speaking and self.last_speaker_id in others_speaking:
            current = self.last_speaker_name
        elif others_speaking:
            current = self.name_for(others_speaking[0]) or "someone"
        else:
            current = ""

        return {
            "ok": True,
            "needsAuth": False,
            "connected": connected,
            "live": live,
            "state": self.conn_state,
            "ping": self.ping,
            "avgPing": self.avg_ping,
            "hostname": self.hostname,
            "guild": self.guild_name,
            "channel": self.channel_name,
            "self": self.self_name,
            "speaker": current,
            "lastSpeaker": self.last_speaker_name,
            "speaking": bool(others_speaking),
            "selfSpeaking": self.self_id in self.speaking,
            "mute": self.mute,
            "deaf": self.deaf,
        }

    def publish(self, force=False):
        self.pub.write(self.snapshot(), force=force)


def offline_payload(reason, needs_auth=False):
    return {
        "ok": False,
        "needsAuth": needs_auth,
        "connected": False,
        "live": False,
        "state": "OFFLINE",
        "reason": reason,
        "ping": None,
        "avgPing": None,
        "hostname": "",
        "guild": "",
        "channel": "",
        "self": "",
        "speaker": "",
        "lastSpeaker": "",
        "speaking": False,
        "selfSpeaking": False,
        "mute": False,
        "deaf": False,
    }


def run_once(pub):
    """One connection lifetime. Returns when the connection drops."""
    token = load_token()
    if not token:
        pub.write(offline_payload("not authorized", needs_auth=True))
        time.sleep(RECONNECT_DELAY * 3)
        return

    rpc = RPC()
    try:
        rpc.connect()
        rpc.handshake()
    except (Closed, RPCError, TimeoutError, OSError) as exc:
        pub.write(offline_payload(f"discord unavailable: {exc}"))
        rpc.close()
        time.sleep(RECONNECT_DELAY)
        return

    try:
        rpc.authenticate(token)
    except RPCError as exc:
        pub.write(offline_payload(f"authorization rejected: {exc}",
                                  needs_auth=True))
        rpc.close()
        time.sleep(RECONNECT_DELAY * 3)
        return
    except (Closed, TimeoutError, OSError) as exc:
        pub.write(offline_payload(f"discord unavailable: {exc}"))
        rpc.close()
        time.sleep(RECONNECT_DELAY)
        return

    session = Session(rpc, pub)
    try:
        for evt in ("VOICE_CONNECTION_STATUS", "VOICE_CHANNEL_SELECT",
                    "VOICE_SETTINGS_UPDATE"):
            rpc.subscribe(evt)
        session.refresh_voice_settings()
        session.refresh_channel()
        session.publish(force=True)

        while True:
            payload = rpc.next_event(timeout=1.0)
            if payload is not None:
                session.handle(payload)
            session.publish()
    except (Closed, RPCError, TimeoutError, OSError) as exc:
        pub.write(offline_payload(f"disconnected: {exc}"))
    finally:
        rpc.close()
    time.sleep(RECONNECT_DELAY)


def main():
    pub = Publisher()
    stopping = {"now": False}

    def stop(_signum, _frame):
        stopping["now"] = True
        pub.write(offline_payload("daemon stopped"), force=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping["now"]:
        try:
            run_once(pub)
        except Exception as exc:  # never let the supervisor loop die
            pub.write(offline_payload(f"internal error: {exc}"))
            time.sleep(RECONNECT_DELAY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
