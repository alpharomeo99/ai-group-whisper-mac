# AI Group Whisper (macOS)

A Mac app that watches your Telegram groups, writes AI summaries and draft replies (fal.ai, choose your model), and keeps everything on your Mac in a local database.

## Install

**Easiest (no security warnings):** open Terminal, paste this, press Enter:

```bash
curl -fsSL https://raw.githubusercontent.com/alpharomeo99/ai-group-whisper-mac/main/install.sh | bash
```

It puts **AI Group Whisper** in your Applications folder and opens it. From then on, open it like any app (Launchpad, Spotlight, Dock).

**Or download** `AI-Group-Whisper-Mac.zip` from Releases, unzip, drag the app into Applications, then right-click → Open the first time.

The first launch takes about a minute while it sets itself up. It needs Python 3 (macOS offers to install it if missing).

## Updates

Click **Check for updates** at the bottom-left, then **Update now**. The app downloads the new version, installs it and reopens. Your settings and Telegram login are kept.

## How it works

- Native Mac window using Apple's built-in WebKit (no Electron, nothing for macOS to block).
- Telegram engine (Telethon) with a local SQLite queue: pauses before the Mac sleeps, resumes and catches up on missed messages when it wakes.
- Data: `~/Library/Application Support/AI Group Whisper/` · Log: `~/Library/Logs/AI Group Whisper.log`
