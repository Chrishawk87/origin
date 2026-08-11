# Deploy Origin to Railway

Origin is already on GitHub. This hosts it so you can use it from any browser,
anywhere — and updates deploy automatically when the repo changes.

## What you need
- A **Railway** account (https://railway.com) — sign in with GitHub.
- One AI key for the hosted brain: **ANTHROPIC_API_KEY** (Claude) or **OPENAI_API_KEY** (GPT).
  (The built-in llama.cpp brain is for your Mac; a cloud container uses an API brain.)

## Steps (all in the Railway website)
1. **New Project → Deploy from GitHub repo** → pick the `origin` repo.
2. Railway detects the `Dockerfile` and builds it automatically.
3. Open the service → **Variables** → add:
   - `ORIGIN_TOKEN` = a secret you choose (e.g. a long random phrase). Required — it
     locks the app so only you can use it.
   - At least one model key (add all you have so they can collaborate):
     - `ANTHROPIC_API_KEY` = Claude key
     - `OPENAI_API_KEY` = GPT key
     - `XAI_API_KEY` = Grok key
     - `GEMINI_API_KEY` = Gemini key
4. **Settings → Networking → Generate Domain** to get a public URL.
5. Open: `https://YOUR-APP.up.railway.app/?token=YOUR_ORIGIN_TOKEN`

That's it — Origin runs in the browser, protected by your token.

## Security
Origin can run commands *inside its own cloud container* (not your computer). The
`ORIGIN_TOKEN` keeps strangers out. Only share the `?token=…` URL with people you
trust, and rotate the token (change the variable) if it ever leaks.

## Health & diagnostics
- `https://YOUR-APP.up.railway.app/healthz` → should say `{"ok": true}`.
- `https://YOUR-APP.up.railway.app/api/diagnostics?token=YOUR_ORIGIN_TOKEN` →
  a status report (brain, tools, workers) you can paste for troubleshooting.

## Updates
When the GitHub repo updates, Railway redeploys automatically. No re-zipping.
