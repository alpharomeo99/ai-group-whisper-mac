# AI Group Whisper (macOS)

A standalone macOS app that watches your Telegram groups, keeps everything in a local SQLite database, writes AI summaries and reply drafts, and can auto-reply when you're mentioned.

## Architecture

```text
Electron (electron/main.cjs)
  ├─ window → renderer/ (plain HTML/JS, no build step)
  ├─ spawns  → daemon/whisperd.py  (Python, Telethon, aiohttp on 127.0.0.1:<random port>)
  ├─ powerMonitor suspend/resume → POST /power  (sleep/wake queue protection)
  └─ GitHub releases/latest check every 6h + "Check for updates" link
daemon/
  store.py   SQLite (WAL): settings, groups, messages, summaries, queue
  ai.py      fal.ai chat client — pick any model (Claude, GPT, Gemini, Llama…) in Settings
```

All data lives in `~/Library/Application Support/AI Group Whisper/data/` (`whisper.db`, `telegram.session`, `daemon.log`).

## Sleep / wake queue protection
- All outgoing work (summaries, drafts, sends) goes through a persistent `queue` table with a unique `dedupe_key`, so a message is never sent twice.
- On **suspend** the daemon stops claiming work, moves `in_flight` jobs back to `pending`, and disconnects Telegram.
- On **resume** (3s delay for Wi-Fi) it reconnects, fetches messages missed while asleep, then restarts the queue.
- On startup any job left `in_flight` by a crash or power loss is recovered.
- Telegram flood-waits and AI 429/5xx errors back off; AI 402/403 pauses the queue until you press **Resume**.

## Run from source
```bash
npm install
npm run setup:python      # creates daemon/.venv with Telethon
npm start
```
Then open **Settings**: enter your Telegram API ID/hash (my.telegram.org), fal.ai key and model, and sign in with your phone.

## Package a macOS app
```bash
npm run setup:python
npm run package:mac       # → release/AI Group Whisper-darwin-*/
```
Unsigned builds: right-click → Open the first time. For distribution, sign and notarize with `@electron/osx-sign` / `@electron/notarize`.

## Updates
Updates come from github.com/alpharomeo99/ai-group-whisper-mac. Bump `version` in `package.json`, then publish GitHub releases tagged `v1.0.1` etc.; the app compares the tag with its version and shows a download banner.
