import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Call details and voice-channel roster for the Discord VC widget.
//
//   My Server
//   GENERAL
//   ─────────────────────
//   ◉ Alice          󰍬
//   ● Maro (you)     󰍭
//   ● zed            󰟎
//   ─────────────────────
//   Voice server  rotterdam1234.discord.media
//   Ping          42 ms · avg 45
//
// Each row carries the same circle as the bar: a green ring while that person
// is transmitting. Your own row's fill is the bar dot's ping colour, since the
// ping is yours; everyone else's is neutral, because Discord only reports your
// connection. The trailing glyph is always present -- microphone when they can
// be heard, struck-through microphone when muted, struck-through headphones
// when deafened -- so every row answers "can they hear / be heard" at a glance.
//
// Everything is read from the host widget's state; the panel holds none of its
// own. Dismissal (click outside, Escape) is KeyboardPanel's.
Panel {
  id: root
  moduleName: "io.github.marobotic.discord-vc"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  readonly property var hostState: hostWidget ? hostWidget.state : ({})
  readonly property bool inCall: !!hostWidget && hostWidget.connected && hostWidget.fresh
  readonly property var members: inCall && Array.isArray(hostState.members)
    ? hostState.members : []

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // The Discord server is the headline; a DM or group call has none.
  readonly property string titleText: {
    if (!inCall) return "Discord VC"
    return String(hostState.guild || "") || "Direct call"
  }

  readonly property string captionText: {
    if (!inCall) return "Not in a voice call"
    return String(hostState.channel || "") || "Voice channel"
  }

  readonly property int ping: hostWidget ? hostWidget.ping : -1

  readonly property string pingText: {
    if (!inCall) return ""
    if (ping < 0) return "— (" + String(hostState.state || "connecting").toLowerCase() + ")"
    var text = ping + " ms"
    var avg = hostState.avgPing
    if (typeof avg === "number" && avg >= 0) text += " · avg " + avg
    return text
  }

  // Same thresholds as the bar dot, but "fast" is plain foreground text
  // rather than the dot's white, which would vanish on a light theme.
  readonly property color pingColor: {
    if (!hostWidget || ping < 0) return dim
    if (ping <= hostWidget.goodPing) return foreground
    if (ping <= hostWidget.okPing) return hostWidget.okColor
    return Color.urgent
  }

  // Every voice host ends in .discord.media; the part before it (region
  // code and node) is the informative bit, and fits the card.
  readonly property string hostText: inCall
    ? String(hostState.hostname || "").replace(/\.discord\.media$/, "") : ""

  // Offline, unauthorized or stale: say why, since there is no tooltip.
  readonly property string reasonText: hostWidget ? hostWidget.offlineReason : ""

  // Route Tab-switching through the host widget, which is what the bar
  // registered -- this Panel is an implementation detail behind it.
  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  KeyboardPanel {
    id: popup
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(320))
    contentHeight: popup.fittedContentHeight(column.implicitHeight, Style.space(480))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      // A big channel outgrows the card; scroll inside it rather than
      // spilling past the screen edge.
      Flickable {
        id: scroller
        anchors.fill: parent
        contentWidth: column.width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: scroller.width
          spacing: Style.space(12)

          Column {
            width: parent.width
            spacing: Style.space(2)

            Text {
              width: parent.width
              text: root.titleText
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
            }

            Text {
              width: parent.width
              text: root.captionText.toUpperCase()
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
            }
          }

          Text {
            width: parent.width
            visible: !root.inCall && root.reasonText !== ""
            text: root.reasonText
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.Wrap
          }

          PanelSeparator {
            visible: root.members.length > 0
            foreground: root.foreground
          }

          Column {
            width: parent.width
            spacing: Style.space(8)

            Repeater {
              model: root.members

              MemberRow {
                required property var modelData
                width: column.width
                member: modelData
              }
            }
          }

          PanelSeparator {
            visible: root.inCall
            foreground: root.foreground
          }

          Column {
            width: parent.width
            visible: root.inCall
            spacing: Style.space(4)

            DetailRow {
              label: "Voice server"
              value: root.hostText || "—"
            }
            DetailRow {
              label: "Ping"
              value: root.pingText
              valueColor: root.pingColor
            }
          }
        }
      }
    }
  }

  component DetailRow: Item {
    id: detail

    property string label: ""
    property string value: ""
    property color valueColor: root.foreground

    width: column.width
    implicitHeight: Math.max(labelText.implicitHeight, valueText.implicitHeight)

    Text {
      id: labelText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(96)
      text: detail.label
      textFormat: Text.PlainText
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      id: valueText
      anchors.left: labelText.right
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: detail.value
      textFormat: Text.PlainText
      color: detail.valueColor
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      elide: Text.ElideRight
    }
  }

  component MemberRow: Item {
    id: row

    property var member: ({})

    readonly property bool speaking: member.speaking === true
    readonly property bool isSelf: member.self === true
    readonly property bool deaf: member.deaf === true
    readonly property bool mute: member.mute === true

    implicitHeight: Math.max(nameText.implicitHeight, glyph.implicitHeight)

    Rectangle {
      id: dot
      anchors.left: parent.left
      anchors.leftMargin: 3
      anchors.verticalCenter: parent.verticalCenter
      width: 8
      height: 8
      radius: width / 2
      color: row.isSelf && root.hostWidget ? root.hostWidget.dotColor : root.dim

      Rectangle {
        anchors.centerIn: parent
        width: parent.width + 6
        height: parent.height + 6
        radius: width / 2
        color: "transparent"
        border.width: 1
        border.color: root.hostWidget ? root.hostWidget.speakingColor : "#75d785"
        opacity: row.speaking ? 0.9 : 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: 140 } }
      }

      Behavior on color { ColorAnimation { duration: 200 } }
    }

    Text {
      id: nameText
      anchors.left: dot.right
      anchors.leftMargin: Style.space(12)
      anchors.right: glyph.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: (row.member.name || "someone") + (row.isSelf ? " (you)" : "")
      textFormat: Text.PlainText
      // Same rule as the bar label: full strength while transmitting.
      color: row.speaking ? root.foreground : Qt.darker(root.foreground, 1.3)
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      elide: Text.ElideRight

      Behavior on color { ColorAnimation { duration: 200 } }
    }

    Text {
      id: glyph
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      text: row.deaf ? "󰟎" : (row.mute ? "󰍭" : "󰍬")
      textFormat: Text.PlainText
      color: row.deaf || row.mute ? Color.urgent : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }
  }
}
