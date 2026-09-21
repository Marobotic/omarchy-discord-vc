"""One-time OAuth authorization for the Discord VC bar widget.

Reading voice connection status, voice settings and speaking events over
Discord's local RPC requires an authenticated session. This walks the
authorization once and caches the access token; the daemon reuses it.

Two modes:

  streamkit (default)
      Uses Discord Streamkit's own application, which Discord whitelists for
      the rpc scopes. No setup: you click "Authorize" in Discord and the
      authorization code is exchanged at streamkit.discord.com (a Discord
      service). Nothing else sees the code, and the token stays on disk here.

  app
      Uses an application you register at discord.com/developers. The code is
      exchanged directly against discord.com. Set DISCORD_VC_CLIENT_ID and
      DISCORD_VC_CLIENT_SECRET, and add the redirect URI shown by --help-app.
"""

import json
import os
import sys
import urllib.error
import urllib.request

# Run as a script under `python3 -I`, which leaves the script's own directory
# off sys.path; add it explicitly so the sibling modules resolve.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rpc import RPC, DEFAULT_CLIENT_ID, RPCError, _obj  # noqa: E402
from safefs import open_private_dir, read_private_file, write_file_atomic  # noqa: E402

SCOPES = ["rpc", "rpc.voice.read"]

STREAMKIT_EXCHANGE = "https://streamkit.discord.com/overlay/token"
DISCORD_EXCHANGE = "https://discord.com/api/oauth2/token"
APP_REDIRECT = "http://localhost"

# The token-exchange reply is a few hundred bytes; don't buffer more than this.
MAX_EXCHANGE_REPLY = 64 * 1024

TOKEN_NAME = "token.json"
# A token file is ~100 bytes; anything near this is not one of ours.
MAX_TOKEN_BYTES = 16 * 1024


def state_dir_path():
    base = os.environ.get("XDG_STATE_HOME") or ""
    # XDG says a relative value is invalid and must be ignored.
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "omarchy-discord-vc")


def load_token():
    """The cached access token, or None.

    The directory is reached through open_private_dir() and the file read
    relative to that descriptor, so neither a symlinked state path nor a
    swapped token file can make this read anything but our own 0600 file.
    Any refusal reads as "not authorized", which prompts a fresh `auth`.
    """
    try:
        dfd = open_private_dir(state_dir_path())
    except OSError:
        return None
    try:
        raw = read_private_file(dfd, TOKEN_NAME, MAX_TOKEN_BYTES)
    except OSError:
        return None
    finally:
        os.close(dfd)
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("access_token")
    return token if isinstance(token, str) and token else None


def save_token(token, client_id):
    """Write the token 0600 into the 0700 state directory, atomically."""
    path = state_dir_path()
    body = json.dumps({"access_token": token, "client_id": client_id})
    dfd = open_private_dir(path)
    try:
        write_file_atomic(dfd, TOKEN_NAME, body.encode("utf-8"), mode=0o600)
    finally:
        os.close(dfd)
    return os.path.join(path, TOKEN_NAME)


def _post_json(url, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=merged)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return _read_reply(resp)


def _post_form(url, fields):
    from urllib.parse import urlencode
    body = urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return _read_reply(resp)


def _str_or(value, default="unknown"):
    return value if isinstance(value, str) and value else default


def _read_reply(resp):
    """Parse a bounded JSON object from an exchange reply."""
    raw = resp.read(MAX_EXCHANGE_REPLY + 1)
    if len(raw) > MAX_EXCHANGE_REPLY:
        raise RPCError("token exchange reply is unexpectedly large")
    try:
        return _obj(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RPCError(f"token exchange reply is not JSON: {exc}") from exc


def exchange_streamkit(code):
    # streamkit.discord.com sits behind Cloudflare, which rejects urllib's
    # default User-Agent outright (403, "error code: 1010") before the request
    # ever reaches the app. Presenting the same headers the Streamkit page
    # itself sends is what makes the exchange reachable.
    data = _post_json(STREAMKIT_EXCHANGE, {"code": code}, headers={
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
        "Origin": "https://streamkit.discord.com",
        "Referer": "https://streamkit.discord.com/overlay",
        "Accept": "application/json",
    })
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise RPCError(
            "streamkit exchange returned no token "
            f"(reply keys: {sorted(data)}). Authorization codes expire within about a "
            "minute -- run the command again and approve promptly.")
    return token


def exchange_app(code, client_id, client_secret):
    data = _post_form(DISCORD_EXCHANGE, {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": APP_REDIRECT,
    })
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise RPCError("discord exchange returned no token "
                       f"(error: {_str_or(data.get('error'))})")
    return token


def authorize(mode="streamkit"):
    if mode == "app":
        client_id = os.environ.get("DISCORD_VC_CLIENT_ID", "").strip()
        client_secret = os.environ.get("DISCORD_VC_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise SystemExit(
                "app mode needs DISCORD_VC_CLIENT_ID and "
                "DISCORD_VC_CLIENT_SECRET in the environment")
    else:
        client_id = DEFAULT_CLIENT_ID
        client_secret = None

    rpc = RPC(client_id)
    path = rpc.connect()
    user = rpc.handshake()
    print(f"connected to {path} as "
          f"{user.get('global_name') or user.get('username')}")

    print("\nDiscord is now showing an authorization prompt.")
    print("Click Authorize in the Discord window to continue.\n")

    # AUTHORIZE blocks until the user answers the in-client modal.
    #
    # It takes only client_id/scopes (plus optional rpc_token and username).
    # Passing redirect_uri here is rejected outright with
    # "Redirect URI cannot be used in the RPC OAuth2 Authorization flow"
    # (code 5000) -- the redirect belongs to the token exchange below, which
    # is a plain OAuth2 call, not an RPC one.
    data = rpc.request("AUTHORIZE",
                       {"client_id": client_id, "scopes": SCOPES},
                       timeout=300.0)
    code = data.get("code")
    if not isinstance(code, str) or not code:
        raise RPCError("no authorization code returned")

    token = (exchange_app(code, client_id, client_secret) if mode == "app"
             else exchange_streamkit(code))

    # Prove the token actually works before we persist it.
    rpc.authenticate(token)
    rpc.subscribe("VOICE_CONNECTION_STATUS")
    rpc.close()

    saved = save_token(token, client_id)
    print(f"authorized; token saved to {saved}")
    return token


def main(argv):
    mode = "streamkit"
    for arg in argv[1:]:
        if arg in ("--app", "-a"):
            mode = "app"
        elif arg in ("--help", "-h"):
            print(__doc__)
            return 0
        else:
            print(f"unknown argument: {arg}", file=sys.stderr)
            return 2
    try:
        authorize(mode)
    except RPCError as exc:
        print(f"authorization failed: {exc}", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        # The exchange is a separate hop from the RPC socket; saying so keeps
        # a CDN block from reading like a broken Discord client.
        print(f"token exchange rejected: HTTP {exc.code} {exc.reason}",
              file=sys.stderr)
        if mode != "app":
            print("If this persists, use your own Discord application:\n"
                  "  omarchy-discord-vc auth --app", file=sys.stderr)
        return 1
    except (OSError, TimeoutError) as exc:
        print(f"could not talk to Discord: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
