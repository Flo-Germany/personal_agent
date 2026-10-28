# Compa Bridge (Android)

Native Kotlin + Jetpack Compose companion for the desktop agent.

## Build

```powershell
cd android
.\gradlew.bat assembleDebug
```

The APK lands in `app/build/outputs/apk/debug/app-debug.apk`. A copy is placed
in `build/android/` by the repository build helper.

Requires JDK 21 and Android SDK 36 (both are present in this workspace).

## Pairing

1. On the desktop: **Settings -> System & Permissions -> Mobile bridge**, enable
   the LAN bridge and restart Compa.
2. Reveal/copy the pairing token.
3. In the app, enter the desktop IP (for example `172.16.1.210`) and the token.
4. Tap **Connect**. The foreground service keeps the WebSocket alive.

The bridge listens on `0.0.0.0:8765` and requires the token for every payload
endpoint. It is off by default.

## Battery

On first launch the app asks for notification permission and offers the
"ignore battery optimizations" exemption. The foreground service uses a
low-priority notification; if ColorOS/One UI still kills it, WorkManager polls
`/api/poll` every 15 minutes and after connection drops.

## Voice

The microphone button uses Android's on-device speech recognizer; the resulting
text is sent over the same task protocol, so the desktop needs no audio stack.
