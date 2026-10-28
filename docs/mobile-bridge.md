# Phase 30: Android companion and LAN bridge

The Android app sends tasks to the desktop and receives push-style status
updates over the home network. The desktop API stays on loopback; a separate,
token-authenticated bridge listens on `0.0.0.0:8765`.

## Desktop bridge

| Piece | Location |
| --- | --- |
| Bridge app + registry | `backend/bridge.py` |
| Settings | `Settings -> System & Permissions -> Mobile bridge` |
| Token | `%LOCALAPPDATA%\com.personalagent.desktop\data\bridge-token.txt` |
| Live status API | `GET /bridge/status` (loopback) |
| Manual notification API | `POST /bridge/notify` (loopback) |

The bridge is **off by default**. Enabling it saves `bridge.enabled=true`; a
backend restart binds the socket. The UI shows the LAN URL, connection count,
pairing token and any bind error.

### Security

- Every WebSocket, poll and status payload requires the 256-bit pairing token
  (`hmac.compare_digest`, query parameter or `X-Compa-Token`).
- Task text is capped at 8 KB; unknown message types are rejected.
- The main HTTP API remains on `127.0.0.1`. Only four bridge routes are exposed:
  `/ws`, `/api/poll`, `/api/status`, `/health`.
- The Android app enables cleartext HTTP because the bridge is a LAN socket
  without TLS. Use it only on a trusted home network. Rotate the token by
  deleting `bridge-token.txt` and restarting.
- The first time the bridge binds `0.0.0.0`, Windows Firewall asks whether to
  allow Python. Allow **Private networks only**. If no prompt appears, add an
  inbound TCP rule for port 8765. Do not expose the port to the public profile.
  An elevated helper is included:

  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\allow_lan_bridge.ps1
  ```

  It allows only the packaged backend Python executable, only TCP 8765, only
  on the Private profile.

## Protocol

All frames are JSON.

| Direction | Type | Fields |
| --- | --- | --- |
| phone -> desktop | `hello` | `token` (only needed if not in query) |
| phone -> desktop | `task` | `text`, optional `conversation_id` |
| phone -> desktop | `approve` | `approval_id`, `approved` |
| phone -> desktop | `cancel` | `conversation_id` |
| phone -> desktop | `ping` | -> `pong` |
| desktop -> phone | `hello` | `version`, `cursor`, `addresses` |
| desktop -> phone | `task_accepted` | `conversation_id`, `text` |
| desktop -> phone | `status` / `final` / `error` / `action_required` | normal agent events |
| desktop -> phone | `notification` | `title`, `body`, `kind` |

Desktop turns are also forwarded: `stream_response` broadcasts `final`,
`error`, `action_required`, `display_required` and `cancelled` to paired
phones. `bridge.notify_finals` controls final-reply pushes.

### Poll fallback

`GET /api/poll?token=...&since=<cursor>&limit=100` returns retained events and
the next cursor. Android WorkManager uses this every 15 minutes and immediately
after a dropped socket.

## Android app

`android/` is a native Kotlin + Jetpack Compose project.

| Area | Implementation |
| --- | --- |
| Connection | OkHttp WebSocket, 20 s ping, exponential reconnect (1-30 s), network callback |
| Background | `BridgeService` foreground service, low-importance ongoing notification |
| Fallback | `PollWorker` periodic + one-time WorkManager jobs |
| Battery | first-launch explanation + `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` |
| Notifications | channels for connection, updates, approvals; `BigTextStyle` |
| UI | pairing fields, status, activity feed, task input, on-device voice input |
| Resume | `service_enabled` is persisted; reopening the app restarts the service |

### Build and install

```powershell
cd android
$env:JAVA_HOME="C:\Program Files\Android\Android Studio\jbr"
.\gradlew.bat testDebugUnitTest assembleDebug
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

The built APK is copied to `build/android/CompaBridge-debug.apk`.

### Pair

1. Enable **Mobile bridge** in desktop Settings and restart Compa.
2. Copy the token shown in that card.
3. Enter the desktop IP (e.g. `172.16.1.210`) and token in the app.
4. Tap **Connect**; accept notification permission and the battery exemption.

## Verification

Backend tests (`backend/tests/test_semantic_bridge.py`) cover token rejection,
authenticated tasks, polling, cancellation and status redaction.

Live bridge check: the gate server binds `0.0.0.0:8765`, advertises
`ws://172.16.1.210:8765/ws`, and accepted a real WebSocket client, a task and
a desktop notification.

Android gate (API 36 emulator, APK installed):

- Foreground service connects to the desktop bridge (`10.0.2.2:8765` from the
  emulator, which maps to the host).
- Desktop `POST /bridge/notify` produced native notification **Build finished /
  Compilation completed on the desktop** while the app was in the background.
- A task typed on the emulator reached the desktop (`task_accepted`, `status`,
  `final`) and produced **Compa finished / Mobile task received** as a native
  notification.
- `./gradlew testDebugUnitTest` passes the JSON protocol tests.

A physical phone on the `172.16.x.x` subnet was not attached to this
workstation; the app uses the same LAN URL the bridge advertised. The emulator
run validates the APK, service, reconnection, notification and task paths.

Real packaged run (not the gate server):

```text
bridge: ws://172.16.1.210:8765/ws, clients=1
desktop -> phone: "Real desktop / Notification from the packaged Compa backend"
phone -> desktop: final "bridge-ok" delivered as "Compa finished"
semantic task: "Which command releases the Phase 29 build?"
             -> "`pnpm build:desktop --phase29`" (retrieved from Qdrant)
```
