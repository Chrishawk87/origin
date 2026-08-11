# Install Origin

Origin is **self-contained** — the AI brain (llama.cpp) installs and runs inside
Origin. No Ollama, no separate app, no API key required to start.

## Mac (start here)

1. **Unzip** `origin.zip` somewhere you'll keep it, e.g. your home folder.
2. Open **Terminal** and go into the folder:
   ```bash
   cd ~/Downloads/origin        # wherever you unzipped it
   ```
3. **Run the installer:**
   ```bash
   bash install.sh
   ```
   It sets up everything — Python deps, the built-in AI engine, the browser, and
   your config — and downloads the built-in model (~1.9GB, one-time). Give it a
   few minutes.
4. **Launch Origin:**
   ```bash
   bash origin-app.sh
   ```
   The Origin window opens. Create a project, pick a working folder, and go.

> First launch is slower only because of the one-time model download.
> Prefer the keyboard? `./.venv/bin/python -m origin` opens the terminal version.
> On macOS, if the engine needs to compile, install Xcode tools once with
> `xcode-select --install`, then re-run `bash install.sh`.

## Any Linux server

Same steps — the installer auto-installs Ollama on Linux:
```bash
cd origin
./install.sh
```
A server has no desktop window, so run the terminal version:
```bash
./.venv/bin/python -m origin
```
Or run the app and reach its UI from your laptop over an SSH tunnel:
```bash
# on the server
./origin-app.sh                     # prints http://127.0.0.1:<port>
# on your laptop
ssh -L 8000:127.0.0.1:<port> you@server   # then open http://127.0.0.1:8000
```

## View & use Origin online (from your phone or another computer)

Origin can serve itself over the network so you're not tied to the Mac it runs on.

```bash
bash origin-serve.sh --lan
```

This prints a URL like `http://192.168.1.24:8000/?token=abc123` — open it on your
phone or laptop **on the same Wi-Fi**. The `token` in the URL is required (Origin
can run commands on your machine, so network access is locked behind it).

To reach it from **anywhere over the internet**, add a free public tunnel:

```bash
# one-time: install cloudflared (https://developers.cloudflare.com/cloudflare-one/…/downloads/)
bash origin-serve.sh --online
```

It prints a `https://…trycloudflare.com/?token=…` URL that works from any network.

**Security:** only share the token URL with yourself/people you trust. Anyone who
has it can operate your machine through Origin. Stop the server with Ctrl+C.

**Diagnostics you can share:** open `…/api/diagnostics?token=…` (or run
`./.venv/bin/python -m origin --doctor`) to get a status report you can paste back
for troubleshooting.

## Add Claude/GPT later (optional)

Origin runs free on the local brain. To let it also call Claude or GPT for hard
reasoning, put keys in a `.env` file next to the app:
```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```
Then in Origin they show up as workers you can `consult` / `collaborate`.

## Check your setup anytime
```bash
./.venv/bin/python -m origin --doctor
```
