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
from rpc import RPC, Closed, RPCError, _obj, _str  # noqa: E402
from safefs import open_runtime_dir, write_file_atomic  # noqa: E402

RECONNECT_DELAY = 3.0
# Republish at least this often so the widget can tell a live daemon from a
# stale file left behind by a crash.
HEARTBEAT = 5.0

CONNECTED_STATES = {
    "VOICE_CONNECTED", "CONNECTED", "VOICE_CONNECTING", "CONNECTING",
    "AUTHENTICATING", "AWAITING_ENDPOINT", "ICE_CHECKING", "NO_ROUTE",
}
LIVE_STATES = {"VOICE_CONNECTED", "CONNECTED"}

# Everything below is sized by what the peer sends, so each has a ceiling.
# A voice channel holds 99 people; the headroom covers stage audiences, and
# a single 1 MiB roster frame could not describe many more than this anyway.
MAX_MEMBERS = 1000
# Guild names are looked up per guild id and cached; only a few are ever live.
MAX_GUILD_NAMES = 16
# An unknown speaker triggers a roster resync, at most this often, so a burst
# of events cannot turn into a burst of requests.
ROSTER_RESYNC_SECONDS = 2.0


STATE_NAME = "omarchy-discord-vc.json"


def _id(value):
    """A Discord snowflake as a string. Ids are digit strings; nothing else is one."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return (value if isinstance(value, str) and value.isascii() and value.isdigit()
            and len(value) <= 20 else "")


def _ms(value):
    """A ping in whole milliseconds, or None for anything not a sane number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not 0 <= value < 600_000:
        return None
    return int(value)


class Publisher:
    """Atomically writes the widget's state file, skipping no-op writes.

    The file is written relative to a descriptor for the runtime directory,
    opened once through open_runtime_dir() (ours, 0700, no symlinks), and
    re-opened only after a failed write.
    """

    def __init__(self):
        self._dir_fd = None
        self._last_payload = None
        self._last_write = 0.0

    def _dir(self):
        if self._dir_fd is None:
            self._dir_fd = open_runtime_dir()
        return self._dir_fd

    def _reset_dir(self):
        if self._dir_fd is not None:
            try:
                os.close(self._dir_fd)
            except OSError:
                pass
            self._dir_fd = None

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
        try:
            write_file_atomic(self._dir(), STATE_NAME,
                              json.dumps(payload).encode("utf-8"), mode=0o600)
        except OSError:
            # Retry from a fresh directory descriptor on the next write.
            self._reset_dir()


class Session:
    """One authenticated RPC connection's worth of voice state."""

    def __init__(self, rpc, publisher):
        self.rpc = rpc
        self.pub = publisher
        user = _obj(rpc.user)
        self.self_id = _id(user.get("id"))
        self.self_name = (_str(user.get("global_name"))
                          or _str(user.get("username")) or "me")

        self.channel_id = None
        self.channel_name = ""
        self.guild_id = None
        self.guild_name = ""
        self.hostname = ""
        self.conn_state = "DISCONNECTED"
        self.ping = None
        self.avg_ping = None

        self.members = {}          # user_id -> {"name", "mute", "deaf"}
        self.speaking = set()      # user ids currently transmitting
        self.last_speaker_id = ""
        self.last_speaker_name = ""

        self.mute = False
        self.deaf = False
        self._guild_names = {}
        self._last_resync = float("-inf")

    # -- naming -------------------------------------------------------------

    @staticmethod
    def _display_name(voice_state):
        user = _obj(voice_state.get("user"))
        return (_str(voice_state.get("nick"))
                or _str(user.get("global_name"))
                or _str(user.get("username"))
                or "someone")

    @classmethod
    def _member(cls, voice_state):
        """Reduce an RPC voice-state object to what the widget renders.

        Muted covers every way someone can end up unheard: muting themselves,
        a server mute, and stage/AFK suppression. Deafened covers self and
        server deafen. The top-level "mute" -- *you* muting them locally -- is
        deliberately ignored: it is your setting, not their state.
        """
        vs = _obj(voice_state.get("voice_state"))
        return {
            "name": cls._display_name(voice_state),
            "mute": bool(vs.get("self_mute") or vs.get("mute")
                         or vs.get("suppress")),
            "deaf": bool(vs.get("self_deaf") or vs.get("deaf")),
        }

    def name_for(self, user_id):
        user_id = str(user_id)
        if user_id == self.self_id:
            return self.self_name
        return (self.members.get(user_id) or {}).get("name", "")

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
            name = _str(data.get("name"))
        except (RPCError, TimeoutError):
            name = ""
        if len(self._guild_names) >= MAX_GUILD_NAMES:
            self._guild_names.clear()
        self._guild_names[guild_id] = name
        return name

    def load_channel(self, channel):
        """Adopt the channel returned by GET_SELECTED_VOICE_CHANNEL."""
        channel = _obj(channel)
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

        channel_id = _id(channel.get("id"))
        if channel_id != self.channel_id:
            if self.channel_id:
                self._unsubscribe_channel(self.channel_id)
            self.speaking.clear()
            self.last_speaker_id = ""
            self.last_speaker_name = ""
            self.channel_id = channel_id
            if channel_id:
                self._subscribe_channel(channel_id)

        self.channel_name = _str(channel.get("name"))
        self.guild_id = _id(channel.get("guild_id")) or None
        self.guild_name = self._guild_name(self.guild_id)
        self.members = {}
        states = channel.get("voice_states")
        for vs in (states if isinstance(states, list) else [])[:MAX_MEMBERS]:
            vs = _obj(vs)
            user = _obj(vs.get("user"))
            uid = _id(user.get("id"))
            if uid:
                self.members[uid] = self._member(vs)

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
        data = _obj(payload.get("data"))

        if evt == "VOICE_CONNECTION_STATUS":
            self.conn_state = _str(data.get("state")) or "DISCONNECTED"
            self.hostname = _str(data.get("hostname"))
            self.ping = _ms(data.get("last_ping"))
            self.avg_ping = _ms(data.get("average_ping"))
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
            uid = _id(data.get("user_id"))
            if uid:
                name = self.name_for(uid)
                if not name:
                    # A member we have not seen yet -- resync the roster, but
                    # not more than once per ROSTER_RESYNC_SECONDS.
                    now = time.monotonic()
                    if now - self._last_resync >= ROSTER_RESYNC_SECONDS:
                        self._last_resync = now
                        self.refresh_channel()
                        name = self.name_for(uid)
                # Only people in the roster are tracked, so the speaking set
                # is bounded by it rather than by whatever ids arrive.
                if name:
                    self.speaking.add(uid)
                    self.last_speaker_id = uid
                    self.last_speaker_name = name

        elif evt == "SPEAKING_STOP":
            self.speaking.discard(_id(data.get("user_id")))

        elif evt in ("VOICE_STATE_CREATE", "VOICE_STATE_UPDATE"):
            user = _obj(data.get("user"))
            uid = _id(user.get("id"))
            if uid and (uid in self.members or len(self.members) < MAX_MEMBERS):
                self.members[uid] = self._member(data)

        elif evt == "VOICE_STATE_DELETE":
            user = _obj(data.get("user"))
            uid = _id(user.get("id"))
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
            "members": self.roster() if connected else [],
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

    def roster(self):
        """Everyone in the channel, alphabetical, for the widget's panel.

        Order is by name rather than by activity so rows never jump around
        while people talk. Your own row takes its mute/deafen from voice
        settings, which update the instant you toggle them, rather than from
        the voice-state echo that follows.
        """
        people = dict(self.members)
        if self.self_id:
            me = dict(people.get(self.self_id) or {"name": self.self_name})
            me.update(mute=self.mute, deaf=self.deaf)
            people[self.self_id] = me
        rows = [{
            "id": uid,
            "name": m.get("name") or "someone",
            "self": uid == self.self_id,
            "speaking": uid in self.speaking,
            "mute": bool(m.get("mute")),
            "deaf": bool(m.get("deaf")),
        } for uid, m in people.items()]
        rows.sort(key=lambda r: r["name"].casefold())
        return rows

    def publish(self, force=False):
        self.pub.write(self.snapshot(), force=force)


def offline_payload(reason, needs_auth=False):
    return {
        "ok": False,
        "needsAuth": needs_auth,
        "members": [],
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
        # A socket that accepted us but never answered is a bad peer, not a
        # dead end: blacklist it so the next attempt uses another client.
        rpc.mark_bad(str(exc))
        pub.write(offline_payload(f"discord unavailable: {exc}"))
        rpc.close()
        time.sleep(RECONNECT_DELAY)
        return

    try:
        rpc.authenticate(token)
    except RPCError as exc:
        # An explicit rejection (bad token, wrong scopes) is the token's
        # fault, not the socket's: report it without blacklisting the peer.
        pub.write(offline_payload(f"authorization rejected: {exc}",
                                  needs_auth=True))
        rpc.close()
        time.sleep(RECONNECT_DELAY * 3)
        return
    except (Closed, TimeoutError, OSError) as exc:
        # The peer answered the handshake but never answered AUTHENTICATE
        # (or dropped mid-flight). Blacklist it so the next attempt lands on
        # another client instead of hanging on the same socket forever.
        rpc.mark_bad(str(exc))
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
