# Discord VC — an Omarchy bar widget

Live Discord voice-call status in the Omarchy bar. It appears when you join a
call and hides itself again when you leave.

![The widget in the Omarchy bar](preview.png)

```
◉ Alice 󰍭
│  │     └─ shown only while you are muted or deafened
│  └─────── who is talking right now (your own name when it is quiet)
└────────── fill is the call ping (white → amber → red);
            a green ring means live audio in the channel
```

Hovering shows the server and channel, the exact ping and its rolling average,
the voice endpoint you are routed through, and who is speaking. Resting states
— silence, an open mic — are deliberately left out, so a hover only ever tells
you something.

## Requirements

- Omarchy 4 (Quattro) with `omarchy-shell`
- The Discord desktop client (native, Flatpak, Snap, Vesktop, canary and PTB
  all work — the plugin talks to whichever one is running)
- `python3` — standard library only, no pip packages, no virtualenv

No sudo or pkexec is required. Everything the plugin installs lives under
`$HOME`.

## Install

```bash
omarchy plugin add https://github.com/Marobotic/omarchy-discord-vc.git
omarchy plugin enable io.github.marobotic.discord-vc
```

Then install the background service and authorize once:

```bash
~/.config/omarchy/plugins/io.github.marobotic.discord-vc/bin/omarchy-discord-vc install
~/.config/omarchy/plugins/io.github.marobotic.discord-vc/bin/omarchy-discord-vc auth
```

`install` writes a **user** systemd unit to
`~/.config/systemd/user/omarchy-discord-vc.service` and enables it. It touches
nothing else — no system units, no `/etc`, no existing config of yours.

To get the shorter command name, put it on your `PATH` once:

```bash
mkdir -p ~/.local/bin
ln -sf ~/.config/omarchy/plugins/io.github.marobotic.discord-vc/bin/omarchy-discord-vc \
       ~/.local/bin/omarchy-discord-vc
```

The widget itself never depends on that symlink — it calls the helper by its
own path inside the plugin directory.

## Uninstall

```bash
omarchy-discord-vc uninstall                        # stop + remove the service
rm -rf ~/.local/state/omarchy-discord-vc            # delete the access token
rm -f  ~/.local/bin/omarchy-discord-vc              # if you made the symlink
omarchy plugin remove io.github.marobotic.discord-vc
```

To revoke the token at Discord's end as well, remove the app under
**User Settings → Authorized Apps**.

## Authorization, and what it actually grants

Reading voice state over Discord's local RPC requires an authorized session.
Running `auth` opens a prompt **inside the Discord client**; approving it
yields a code that is exchanged for an access token stored at
`~/.local/state/omarchy-discord-vc/token.json` (mode `0600`).

The requested scopes are `rpc` and `rpc.voice.read` — **read-only voice
state**. The token cannot send messages, join or leave calls, or change
anything about your account.

Tokens expire after a while. When that happens the widget falls back to a red
dot reading `setup`; run `auth` again.

### Two exchange modes — please read before choosing

**`streamkit` (the default)** uses Discord Streamkit's own public application
ID, which Discord whitelists for the `rpc` scopes, and exchanges the code at
`streamkit.discord.com`. It needs zero setup, and the code goes to a Discord
service and nowhere else. Being straight with you about the trade-offs:

- The authorization prompt will say **"Streamkit Overlay"**, not this plugin.
  You are approving an application you do not control.
- That token-exchange endpoint is undocumented. Discord can change or remove
  it at any time, which would break `auth` until this plugin is updated.
- The request sends a browser `User-Agent`, because the endpoint sits behind
  Cloudflare and rejects the Python default outright.

**`app`** uses an application *you* register, exchanging directly against
`discord.com`. Nothing undocumented, nothing borrowed, and the prompt carries
your own app's name:

1. Create an application at <https://discord.com/developers/applications>.
2. Add `http://localhost` as an OAuth2 redirect URI.
3. Run it with your own credentials:

```bash
DISCORD_VC_CLIENT_ID=… DISCORD_VC_CLIENT_SECRET=… omarchy-discord-vc auth --app
```

If you would rather not authorize a third party's application, use `--app`.

## How it gets the data

A small daemon (`daemon/daemon.py`, standard library only) speaks Discord's
**local RPC protocol** over the `discord-ipc-*` unix socket in your runtime
directory, and republishes what it learns to
`$XDG_RUNTIME_DIR/omarchy-discord-vc.json`. The QML widget only ever reads
that file, so the bar never blocks on Discord and a dead daemon degrades to a
hidden widget rather than an error.

Subscribed events:

| Event | Gives us |
|---|---|
| `VOICE_CONNECTION_STATUS` | connection state, voice hostname, `last_ping`, `average_ping` |
| `VOICE_CHANNEL_SELECT` | joining and leaving a channel |
| `VOICE_SETTINGS_UPDATE` | mute / deafen |
| `SPEAKING_START` / `SPEAKING_STOP` | who is transmitting, including you |
| `VOICE_STATE_*` | the member roster, to turn user ids into names |

The green ring is driven by `SPEAKING_START` / `SPEAKING_STOP` for everyone in
the channel *including you*. That is Discord's own decision about who is
transmitting: it already accounts for push-to-talk, the input-sensitivity gate,
Krisp noise suppression and server mute. It is deliberately *not* the local
PipeWire source level, so a mic that is muted in Discord never lights the ring
even while the hardware is live. Because the ring covers your own transmit, it
doubles as "Discord is hearing me".

The mic glyph is only a warning: it appears when you are muted (`󰍭`) or
deafened (`󰟎`) and is absent otherwise, so an unremarkable call shows just a
dot and a name.

### What lands on disk

| Path | Contents |
|---|---|
| `$XDG_RUNTIME_DIR/omarchy-discord-vc.json` | current server, channel, ping, and the display names of people in your call |
| `~/.local/state/omarchy-discord-vc/token.json` | the access token, mode `0600` in a `0700` directory |
| `~/.config/systemd/user/omarchy-discord-vc.service` | the user service |

Nothing is sent anywhere. The only outbound network request this plugin ever
makes is the one-time token exchange during `auth`, to `streamkit.discord.com`
or `discord.com`. Your runtime directory is mode `0700`, so the state file is
readable only by you.

## Commands

```bash
omarchy-discord-vc state       # dump what the widget is currently reading
omarchy-discord-vc restart     # restart the daemon
omarchy-discord-vc log         # journal for the daemon
omarchy-discord-vc status      # service status
omarchy-discord-vc run         # run in the foreground, for debugging
```

## Settings

Per-widget overrides go in the widget's entry in `~/.config/omarchy/shell.json`:

```json
{ "id": "io.github.marobotic.discord-vc", "goodPing": 98, "okPing": 301, "silentShows": "self" }
```

| Setting | Default | Meaning |
|---|---|---|
| `goodPing` | `98` | ms at or below which the dot is white |
| `okPing` | `301` | ms at or below which the dot is amber; above it, red |
| `silentShows` | `"self"` | label when nobody is talking — `"self"` for your own name, `"last"` to keep the last speaker |
| `maxNameLength` | `14` | characters before a name is elided |
| `showName` | `true` | show the speaker label at all |
| `showMic` | `true` | show the muted/deafened glyph at all |
| `showWhenIdle` | `false` | keep a dim widget visible when not in a call |

## Clicks

- **Left** — focus the Discord window (or start authorization when unconfigured)
- **Middle** — restart the daemon

## Troubleshooting

**A red dot reading `setup`** — there is no usable token, or it expired. Click
it, or run `omarchy-discord-vc auth`.

**Nothing in the bar at all** — either you are not in a call (that is the
normal resting state; set `showWhenIdle` to `true` if you want it visible
anyway) or the daemon is not publishing. Check `omarchy-discord-vc log`.

**`no Discord IPC socket found`** — the Discord client is not running, or it
is a sandboxed build that puts its socket somewhere unusual. `ls
$XDG_RUNTIME_DIR/discord-ipc-*` should list at least one socket while Discord
is open.

**`auth` fails with an HTTP error** — the Streamkit endpoint is refusing the
exchange. Authorization codes also expire in about a minute, so approve the
prompt promptly. If it persists, use `--app` as described above.

The daemon reconnects on its own when Discord restarts, and the widget treats
a state file older than 20 seconds as stale, so a killed daemon degrades to
"not in a call" rather than a frozen ping.

**Edits to `BarWidget.qml` not showing up** — the shell's plugin hot-reload
logs `Local plugin changed, reloading` and can still render the previously
compiled component. `omarchy restart shell` clears it.

## Support

If this is useful to you: [ko-fi.com/marobotic](https://ko-fi.com/marobotic) ☕

## License

MIT — see [LICENSE](LICENSE).
