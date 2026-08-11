# Building Origin into a downloadable desktop app

Origin runs today as a native app window via `origin-app.sh` (or `python run_app.py`).
To turn it into a **double-click installer** you (or a CI runner) build it **on the
target OS** — a macOS `.app`/`.dmg` must be built on macOS, a Windows `.exe` on
Windows. (This can't be produced from a Linux cloud sandbox.)

## 1. One-time setup on your machine

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium                   # for click-and-retrieve
pip install pyinstaller
```

## 2. Build

```bash
pyinstaller packaging/origin.spec
```

- **macOS** → `dist/Origin.app`  (drag to /Applications; make a .dmg with
  `hdiutil create -volname Origin -srcfolder dist/Origin.app -ov Origin.dmg`)
- **Windows** → `dist/Origin/Origin.exe`  (zip the folder, or wrap with Inno Setup
  for a real installer)
- **Linux** → `dist/Origin/Origin`

## 3. First run

Origin needs an AI brain. Either:
- run **Ollama** locally (`ollama pull llama3.1`) — the default free coordinator, or
- put an API key in a `.env` next to the app (or in `~/.origin/config.yaml`).

## Notes on signing (so it opens without warnings)

- **macOS**: `codesign --deep --force --sign "Developer ID Application: <you>" dist/Origin.app`
  then notarize with `xcrun notarytool`. Without this, users must right-click → Open once.
- **Windows**: sign `Origin.exe` with `signtool` and a code-signing certificate,
  or users get a SmartScreen prompt.

Signing needs *your* developer certificate, so it's the one step that must be done
by you — the build config above gets you everything up to that point.
