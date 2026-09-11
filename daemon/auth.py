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
import stat
import sys
import urllib.error
import urllib.request

from rpc import RPC, DEFAULT_CLIENT_ID, RPCError

SCOPES = ["rpc", "rpc.voice.read"]

STREAMKIT_EXCHANGE = "https://streamkit.discord.com/overlay/token"
DISCORD_EXCHANGE = "https://discord.com/api/oauth2/token"
APP_REDIRECT = "http://localhost"


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(base, "omarchy-discord-vc")
    os.makedirs(path, mode=0o700, exist_ok=True)
    # makedirs' mode only applies when it creates the directory; tighten one
    # that already existed with looser permissions.
    os.chmod(path, stat.S_IRWXU)
    return path


def token_path():
    return os.path.join(state_dir(), "token.json")


def load_token():
    try:
        with open(token_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    token = data.get("access_token")
    return token if isinstance(token, str) and token else None


def save_token(token, client_id):
    path = token_path()
    tmp = path + ".tmp"
    # Create the file 0600 from the start rather than chmod-ing after the
    # write, so the token never exists in a file with umask permissions.
    # O_NOFOLLOW refuses to write through a symlink left at the temp path.
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"access_token": token, "client_id": client_id}, fh)
    os.replace(tmp, path)
    return path


def _post_json(url, payload, headers=None):
    body = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=merged)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_form(url, fields):
    from urllib.parse import urlencode
    body = urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    if not token:
        raise RPCError(
            "streamkit exchange returned no token "
            f"(reply: {data}). Authorization codes expire within about a "
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
    if not token:
        raise RPCError(f"discord exchange returned no token: {data}")
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
    if not code:
        raise RPCError(f"no authorization code returned: {data}")

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
