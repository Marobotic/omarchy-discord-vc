import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Discord voice-call status for the Omarchy bar.
//
//   ◉ Maro 󰍭
//   │  │    └─ your muted/deafened warning, shown only beside your own name
//   │  └────── whoever is talking; your own name when the channel is quiet
//   └────────── fill is the call ping (white→amber→red); a green ring means
//               live audio, yours included, per Discord's own voice detection
//
// Clicks: left opens the panel (Panel.qml) -- the server and channel you are
// in, your ping, and everyone in the channel with the same speaking ring and
// their mute/deafen state; right focuses the Discord window; middle restarts
// the daemon. There is deliberately no hover tooltip: details are one click
// away and never pop up just from moving the pointer across the bar.
//
// Everything is read from the state file the companion daemon publishes at
// $XDG_RUNTIME_DIR/omarchy-discord-vc.json. The daemon owns all Discord RPC
// traffic; this widget only renders what it finds, so a dead daemon degrades
// to a hidden (or, with showWhenIdle, a muted) widget rather than an error.
//
// The file is watched *and* polled. watchChanges alone is not enough here:
// the daemon writes atomically via rename(), which replaces the inode a
// QFileSystemWatcher is holding, so a watch can go deaf after the first
// update. The poll is the floor that guarantees freshness; the watcher just
// makes the common case land sooner.
//
// shell.json settings (all optional):
//   goodPing      ms at or under which the dot is white          (default 98)
//   okPing        ms at or under which the dot is amber         (default 301)
//   silentShows   "self" | "last" -- label when nobody is talking (default "self")
//   maxNameLength characters before the name is elided           (default 24)
//   showName      show the speaker label                       (default true)
//   showMic       show the muted-mic glyph while muted         (default true)
//   showWhenIdle  keep a dim widget when not in a call        (default false)
BarWidget {
  id: root
  moduleName: "io.github.marobotic.discord-vc"

  // XDG_RUNTIME_DIR is set in any normal Wayland session; the uid lookup
  // below is the belt-and-braces path for the sessions where it is not, so
  // the widget never assumes uid 1000.
  property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") || ""
  readonly property string statePath: runtimeDir
    ? runtimeDir + "/omarchy-discord-vc.json" : ""

  Process {
    id: uidProbe
    command: ["/usr/bin/id", "-u"]
    running: root.runtimeDir === ""
    stdout: StdioCollector {
      onStreamFinished: {
        var uid = text.trim()
        if (uid) root.runtimeDir = "/run/user/" + uid
      }
    }
  }

  // The helper ships inside the plugin, so call it by its own path rather
  // than trusting ~/.local/bin to be on the shell session's PATH.
  readonly property string cli: {
    // resolvedUrl is percent-encoded; decode it back to the real path.
    var dir = decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, ""))
    return dir.replace(/\/$/, "") + "/bin/omarchy-discord-vc"
  }

  // Omarchy's own commands live outside /usr/bin; call them by path rather
  // than through whatever PATH the shell session happens to have.
  readonly property string omarchyBin: {
    var base = Quickshell.env("OMARCHY_PATH") || ""
    return (base.charAt(0) === "/" ? base : "/usr/share/omarchy") + "/bin"
  }

  // Settings, resolved once per change rather than per binding evaluation.
  readonly property int goodPing: setting("goodPing", 98)
  readonly property int okPing: setting("okPing", 301)
  readonly property string silentShows: setting("silentShows", "self")
  readonly property int maxNameLength: setting("maxNameLength", 24)
  readonly property bool showName: setting("showName", true)
  readonly property bool showMic: setting("showMic", true)
  readonly property bool showWhenIdle: setting("showWhenIdle", false)

  property var state: ({})

  readonly property bool daemonOk: state.ok === true
  readonly property bool needsAuth: state.needsAuth === true
  readonly property bool connected: daemonOk && state.connected === true
  readonly property bool live: daemonOk && state.live === true

  // A state file nobody has refreshed recently means the daemon died without
  // getting a chance to write its farewell. Treat it as disconnected.
  property double now: 0
  readonly property bool fresh: {
    var updated = Number(state.updated || 0)
    return updated > 0 && (now - updated) < 20
  }

  readonly property int ping: {
    var value = state.ping
    return typeof value === "number" && value >= 0 ? value : -1
  }

  readonly property color dotColor: {
    if (needsAuth) return Color.urgent
    if (!connected || !fresh) return Color.muted
    if (!live || ping < 0) return Color.muted
    if (ping <= root.goodPing) return root.fastColor
    if (ping <= root.okPing) return root.okColor
    return Color.urgent
  }

  function truncate(name) {
    if (!name) return ""
    if (name.length <= root.maxNameLength) return name
    return name.slice(0, Math.max(1, root.maxNameLength - 1)) + "…"
  }

  readonly property string speakerLabel: {
    if (needsAuth) return "setup"
    if (!connected || !fresh) return ""
    var speaking = String(state.speaker || "")
    if (speaking) return truncate(speaking)
    if (root.silentShows === "last") {
      var last = String(state.lastSpeaker || "")
      if (last) return truncate(last)
    }
    return truncate(String(state.self || ""))
  }

  // The label keeps a fixed width so the bar slot never re-sizes as different
  // people talk. The width is measured from the longest name actually in the
  // call, so names like "Ultranova Violet" fit uncut, and elide only rescues
  // someone absurd joining afterwards.
  readonly property string widestName: {
    var names = []
    var members = state.members
    if (Array.isArray(members)) {
      for (var i = 0; i < members.length; i++)
        names.push(String((members[i] || {}).name || ""))
    }
    var selfName = String(state.self || "")
    if (selfName) names.push(selfName)
    var speakerName = String(state.speaker || "")
    if (speakerName) names.push(speakerName)
    var lastName = String(state.lastSpeaker || "")
    if (lastName) names.push(lastName)
    names.push("setup")

    var worst = ""
    for (var j = 0; j < names.length; j++) {
      var n = root.truncate(names[j])
      if (n.length > worst.length) worst = n
    }
    return worst
  }

  TextMetrics {
    id: nameMetrics
    font: nameLabel.font
    text: root.widestName
  }

  readonly property real labelWidth: Math.max(24, nameMetrics.advanceWidth)

  // Ping fill runs white (fast) → amber → red. The ring keeps its own green
  // so the two signals never read as the same colour.
  readonly property color fastColor: "#ffffff"
  readonly property color okColor: "#ebcb8b"
  readonly property color speakingColor: "#75d785"

  // Live audio in the channel, yours included. The ring around the dot is the
  // only speaking indicator now, so it has to cover your own transmit too --
  // otherwise nothing shows that Discord is picking you up.
  readonly property bool anySpeaking: connected && fresh
    && (state.speaking === true || state.selfSpeaking === true)

  // The mic glyph is purely a muted warning: it appears only when you cannot
  // be heard, and is absent the rest of the time.
  readonly property bool inputMuted: connected && fresh
    && (state.mute === true || state.deaf === true)

  // ...but it is *your* state sitting next to a label that names whoever is
  // talking, so beside someone else's name it reads as "they are muted".
  // Show it only when the label is your own, or when no label is rendered
  // at all and there is nothing to misread.
  readonly property bool labelIsSelf: String(state.speaker || "") === ""
    && !(root.silentShows === "last" && String(state.lastSpeaker || ""))
  readonly property bool nameShown: root.showName && !root.vertical
    && root.speakerLabel !== ""

  readonly property string micGlyph: state.deaf === true ? "󰟎" : "󰍭"

  // Why the widget is not showing a call, for the panel to explain.
  readonly property string offlineReason: {
    if (needsAuth) return "Not authorized — click the widget to authorize"
    if (!daemonOk || !fresh)
      return String(state.reason || "daemon not running")
        + " — middle click to restart"
    return ""
  }

  visible: needsAuth || connected || (root.showWhenIdle && daemonOk)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Freshness clock. One second is plenty for a 20 second staleness window
  // and keeps the widget idle-cheap.
  Timer {
    interval: 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.now = Date.now() / 1000
  }

  FileView {
    id: stateFile
    path: root.statePath
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.state = Util.parseModuleJson(text())
    onLoadFailed: root.state = ({})
  }

  // See the header note: rename()-based writes can outrun watchChanges.
  Timer {
    interval: 250
    running: true
    repeat: true
    onTriggered: stateFile.reload()
  }

  Process { id: action }

  // Authorization is interactive -- Discord shows a modal and the helper
  // prints what it is doing -- so it needs a visible terminal, not a silent
  // background process. The launcher runs its argument through `bash -c`,
  // so the path is shell-quoted: a home directory with spaces or shell
  // metacharacters must stay one literal word.
  function runAuth() {
    action.command = [root.omarchyBin + "/omarchy-launch-floating-terminal-with-presentation",
                      Util.shellQuote(root.cli) + " auth"]
    action.running = true
  }

  function restartDaemon() {
    action.command = [root.cli, "restart"]
    action.running = true
  }

  function focusDiscord() {
    action.command = ["/usr/bin/hyprctl", "dispatch", "focuswindow", "class:discord"]
    action.running = true
  }

  // ---- roster panel. Bar.findPanelWidget routes summon/hide/toggle and
  //      click-to-switch between bar popups through open/close/opened on the
  //      bar-widget root, so those forward to the loaded Panel.qml.
  readonly property bool opened: panelLoader.item
    ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item
    ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function togglePanel() {
    if (!panelLoader.item) return
    panelLoader.item.toggle()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    target.bar = root.bar
    target.anchorItem = button
    target.hostWidget = root
  }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      // bar can still be null on the first pass; inject again once the
      // widget has been fully attached.
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    labelVisible: false
    hasVisualContent: true
    tooltipText: ""
    fixedWidth: root.vertical ? -1 : content.implicitWidth + scaledHorizontalMargin * 2
    fixedHeight: root.vertical ? content.implicitHeight + scaledVerticalPadding * 2 : -1

    onPressed: function (b) {
      if (root.needsAuth) { root.runAuth(); return }
      if (b === Qt.MiddleButton) root.restartDaemon()
      else if (b === Qt.RightButton) root.focusDiscord()
      else root.togglePanel()
    }

    Row {
      id: content
      anchors.centerIn: parent
      spacing: 6

      Rectangle {
        id: dot
        anchors.verticalCenter: parent.verticalCenter
        width: 8
        height: 8
        radius: width / 2
        color: root.dotColor

        // A green ring while there is live audio, so the dot carries both
        // signals: its fill is the ping, its ring is speech.
        Rectangle {
          anchors.centerIn: parent
          width: parent.width + 6
          height: parent.height + 6
          radius: width / 2
          color: "transparent"
          border.width: 1
          border.color: root.speakingColor
          opacity: root.anySpeaking ? 0.9 : 0
          visible: opacity > 0
          Behavior on opacity { NumberAnimation { duration: 140 } }
        }

        Behavior on color { ColorAnimation { duration: 200 } }
      }

      Text {
        id: nameLabel
        anchors.verticalCenter: parent.verticalCenter
        visible: root.showName && text !== "" && !root.vertical
        text: root.speakerLabel
        width: root.labelWidth
        elide: Text.ElideRight
        horizontalAlignment: Text.AlignLeft
        textFormat: Text.PlainText
        renderType: Text.NativeRendering
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        color: root.anySpeaking
          ? (root.bar ? root.bar.barForeground : Color.foreground)
          : Qt.darker(root.bar ? root.bar.barForeground : Color.foreground, 1.4)

        Behavior on color { ColorAnimation { duration: 200 } }
      }

      Text {
        anchors.verticalCenter: parent.verticalCenter
        visible: root.showMic && root.inputMuted
                 && (root.labelIsSelf || !root.nameShown)
        text: root.micGlyph
        textFormat: Text.PlainText
        renderType: Text.NativeRendering
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.body
        color: Color.urgent
      }
    }
  }
}
