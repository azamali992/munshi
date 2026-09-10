# Munshi for Android

A native Android shell (Capacitor 7) around the Munshi web app. The APK holds
one screen — *which Munshi server?* — and then loads the app from that server,
so every server update reaches phones without a reinstall. It adds what a
browser tab can't: a launcher icon and splash, a proper back button, the
microphone permission for voice orders, and a remembered server address.

## Install on a phone

Download `munshi-<version>.apk` from the GitHub Release, open it on the phone
(allow "install from this source" once), then enter the server address:

- your own PC on the same Wi-Fi: `http://192.168.x.x:8000` (run `docker compose up` in the repo)
- a hosted server: `https://munshi.yourbusiness.pk`

Change it later from **Settings → Change server**.

## Build it yourself

Needs JDK 17+, Android SDK (platform 35, build-tools 35), Node 20+.

```bash
cd mobile
npm ci
npx cap sync android
cd android && ./gradlew assembleDebug          # android/app/build/outputs/apk/debug/app-debug.apk
```

Release builds are signed with a keystore you keep outside the repo:

```bash
keytool -genkeypair -keystore keys/munshi-release.jks -alias munshi -keyalg RSA -keysize 2048 -validity 10000
cat > android/keystore.properties <<EOT
storeFile=../../keys/munshi-release.jks
storePassword=...
keyAlias=munshi
keyPassword=...
EOT
cd android && ./gradlew assembleRelease        # android/app/build/outputs/apk/release/app-release.apk
```

`keys/` and `keystore.properties` are git-ignored. Losing the keystore means
future builds can't update an installed app in place — back it up.

## What's inside

- `www/index.html` — the connect screen (server URL, health check, remembered servers)
- `capacitor.config.json` — `allowNavigation: ["*"]` so the WebView may load your server; `cleartext: true` for LAN `http://` servers
- `android/` — the generated Android project (committed, so builds are reproducible); icons and splash generated from `src/munshi/web/static/icon.svg`
- Permissions: INTERNET, RECORD_AUDIO (voice orders only when you tap the mic)

Play Store publishing: the same project builds an AAB with `./gradlew bundleRelease`; add a privacy-policy URL and screenshots from `docs/screens/`.
