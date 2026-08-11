# ◍ Origin

Origin is an autonomous agent you run as a **desktop app** (a native window,
like the Claude app) or from the terminal. You open it against a **project**
(e.g. "Everroot"), tell it what to do in plain language, and it operates
everything reachable from your machine — the web, your files, MCP servers, REST
APIs — orchestrating one or more AI models behind a single brain.

```
        ┌──────────────────────────────────────────┐
        │                   ORIGIN                    │
        │   desktop app  ·  projects  ·  one brain    │
        └───────────────┬───────────┬────────────────┘
             web + browser │  local shell │  MCP │  REST APIs │  model workers
```

Ask it things like:

> "Find the top-performing TikTok ads for eco skincare and tell me what's working."
> "Organize this folder by file type and clean up the filenames."
> "Research our top 3 competitors and summarize their marketing angles."

## Grounded in today's world (self-updating)

Origin answers by **researching live at the moment you ask** — not by reciting a
frozen model memory — so it stays current. Every finding is cached with a
**timestamp and its sources**; when you ask the same thing later, Origin checks
whether the info is stale or has **changed**, re-gathers if needed, and tells you
*what changed since last time*. Watched topics refresh **daily** on their own.

- `research(question)` — live, cited answer; auto-caches with freshness (TTL).
- `deep_research(question)` — wider/deeper pass for hard questions.
- `watch_topic(question)` — track it; Origin refreshes it daily and flags changes.
- `recall()` — everything Origin knows, with how fresh it is.

> Ask "what's the going CPM for TikTok ads in my niche?" today and Origin gathers
> it live. Ask again in two weeks — if it moved, Origin re-gathers and says
> "🔄 Updated since 14d ago," with the new figure and sources.

Honest bounds: Origin finds what's **publicly reachable** (search + pages + the
browser) — not paywalled or login-gated data — and it's "self-updating" in the
sense that its *retrieved knowledge* refreshes; the model's own weights don't.

## Becomes a master of anything, works like an executive, learns over time

Three things make Origin behave like a capable operator rather than a chatbot:

**Become any expert.** Not a fixed list of roles — Origin composes a top‑1%
practitioner persona for *any* domain on demand and grounds it in live research.
`/become growth marketing`, `/become Meta ad buying`, `/become options trading`,
`/become iOS design` — or type any expertise in the app's role selector, or let
Origin call the `become` tool itself mid‑task. (The five named roles —
researcher, marketer, product_designer, analyst, assistant — are just shortcuts.)

**Executive loop (missions).** For a real goal, Origin plans it, executes the
steps with tools + research + multi‑model collaboration, critiques whether the
goal is met, and iterates until it is — then hands you the deliverable. Use
`/mission <goal>` in the terminal or the **🎯 Mission** button in the app.

**Learns over time.** Origin keeps persistent memory of your preferences, goals,
facts, and lessons (`~/.origin/memory.db`). It pulls the relevant ones into
context before every task and saves new ones with `remember`, so it gets more
tailored and effective the more you use it. `/memory` shows what it knows.
(Honest bound: this is learning *by accumulation* — its retrieved knowledge and
your context grow; the model's own weights don't change.)

## Connect dozens of apps — shared by every model

Origin plugs into apps two ways, and **every worker (Claude *and* GPT) can use
whatever you connect**, because all tools live in one shared registry:

- **MCP servers** — the same connector ecosystem GPT plugs into (Slack, Google
  Drive, Notion, GitHub, …). Add them under `mcp_servers` in config.
- **REST APIs** — any app with an HTTP API (Meta / TikTok / Google Ads, Stripe,
  your own backend). Add them under `rest_apis` with your keys.

The example config ships commented‑out templates for ad platforms and popular
apps — uncomment, drop in your keys, and Origin can run and report on campaigns,
push to your tools, and pull your data. (Apps that use OAuth need you to supply a
token; Origin uses whatever credentials you give it, scoped to what you allow.)

## Setup in one command

```bash
python -m origin --doctor      # or:  python -m origin.bootstrap
```

`doctor` checks and sets up what Origin uses: installs the Playwright browser,
pulls a default **Ollama** model so there's a free local brain out of the gate,
and reports which API keys/workers are active.

## Run it as a desktop app

```bash
pip install -r requirements.txt
python -m playwright install chromium        # for click-and-retrieve browsing
./origin-app.sh                              # or:  python run_app.py
```

A native Origin window opens (falls back to your browser if pywebview isn't
installed). On the left you pick or create a **project**; each project has its
own working folder, its own chat history, and can be **shared** — the "Share ⇪"
button exports a `.originproj` bundle you send to someone, and "Import" opens
one. One-click **presets** (TikTok top ads, organize-folder, competitor scan)
sit above the message box.

To ship it as a **double-click installer** (`.app` / `.exe`), see
[`packaging/BUILD.md`](packaging/BUILD.md). Prefer the keyboard? `origin` still
launches the full terminal REPL.

### What it connects (all in one place)

- **Local shell / OS** — run any command, read/write files, orchestrate CLIs.
- **MCP servers** — connect any Model Context Protocol server; its tools show
  up automatically as `mcp__<server>__<tool>`.
- **REST / HTTP APIs** — wire in *any* API with a few lines of YAML (no code),
  reachable through a single `http_request` tool with auth applied for you.
- **Multiple LLM workers** — Claude, GPT, and a local model available at once,
  able to consult and collaborate with each other on one task (see below).
- **Web + browser** — token-free `web_search` / `web_fetch` and real
  click-and-retrieve browsing (`browse`, `browser_click`, …) that *any* worker,
  including a local model, can drive.

## The brain is its own layer

The **brain is not Claude or GPT** — it's a coordination layer (the
*coordinator*) that you point at whichever worker you choose, even a cheap local
model. Claude and GPT are *workers* the brain calls. That means a single task
can pull in both models — having them critique and build on each other's work —
without you hopping between two apps or paying to resend context to each.

```
        You ──▶ Coordinator brain (a worker you choose — local / claude / gpt)
                      │  decomposes the task, delegates, verifies
      ┌───────────────┼────────────────────────────┐
      ▼               ▼                             ▼
   consult(gpt)   collaborate([claude,gpt])    shell / MCP / REST
   one model      refine · debate · panel      act on real systems
```

Model-to-model tools the brain (or you) can call:

- `consult(worker, prompt)` — delegate a sub-task to one model (route cheap work
  to the local model, hard reasoning to Claude, etc.).
- `collaborate(task, workers, mode)` — run a structured multi-model exchange:
  - **refine** — one model proposes, another critiques, the author revises;
  - **debate** — models exchange and defend positions over N rounds;
  - **panel** — each answers independently, a synthesizer merges the best.

**Token economy:** workers get only the relevant slice of context (the sub-task
and the specific statements they need), never the whole conversation; simple
work routes to the cheapest capable model; a per-worker call counter (`/models`)
shows where spend goes.

The brain is **pluggable**: Anthropic Claude, OpenAI, or a local Ollama model.
It runs in **full autonomous mode** — it does not ask permission before acting —
with a lean **operator** persona that just executes what you tell it, and full
control over *what* it may do and *how* it behaves.

### About "no guardrails"

Worth being straight about this, because it affects how you get what you want:

- The **model's own safety training is server-side** (on Anthropic's / OpenAI's
  servers). No local wrapper can remove it, and trying to jailbreak it via
  prompts mostly just risks your API key. That layer is not something this tool
  fights.
- But most of the friction people mean by "guardrails" is the **assistant
  personality** — the hedging, moralizing, and permission-seeking bolted on top.
  That layer *is* local, and the built-in **operator** persona strips it: no
  unsolicited disclaimers, follows your method exactly, does the task the best
  way it can. It keeps only two guards, because they keep it *useful*: it never
  fabricates results, and it verifies its own work.
- If you want **no external policy layer at all**, run a **local model via
  Ollama** (already supported as a pluggable brain). You run the model yourself,
  so there's no server-side policy in the loop — set a profile's `provider:
  ollama` and it's yours.

> ⚠️ **Autonomous mode runs real commands on your machine without confirmation.**
> That is exactly what you asked for, and it is powerful. Run it on a machine
> and under an account whose blast radius you're comfortable with, keep secrets
> in environment variables (not in prompts), and read the safety notes at the
> bottom before pointing it at production systems.

---

## Quick start

```bash
# 1. Install (from the project folder)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # core + all providers + mcp

# 2. Configure
cp origin.config.example.yaml origin.config.yaml
#   edit origin.config.yaml, then export your key:
export ANTHROPIC_API_KEY=sk-ant-...        # or OPENAI_API_KEY

# 3. Run
python -m origin
```

Or use the one-line launcher (creates the venv for you):

```bash
./hub-run.sh
```

Installing as a proper command:

```bash
pip install -e .        # gives you a `hub` command on your PATH
hub                     # interactive
hub -p "what's my disk usage and free space?"   # one-shot
```

---

## Web search, gathering & click-and-retrieve

These are **tools, not model intelligence** — the tool does the searching and
clicking; the model only decides *when* to call it. So even a small local model
can research the web, and it costs **no LLM tokens** (the default DuckDuckGo
search backend needs no API key either).

- `web_search(query)` — ranked results (DuckDuckGo by default; Tavily / Brave /
  SearXNG if you add a key/URL in config).
- `web_fetch(url)` — fetch a page and return its readable text (or JSON).
- `browse(url)`, `browser_read`, `browser_links`, `browser_click`,
  `browser_type`, `browser_screenshot` — a live Playwright browser session for
  true **click-and-retrieve**: open a page, see its links, click through, fill a
  search box, screenshot. Install once with:
  `pip install playwright && playwright install chromium`.

### Honest note on "as powerful as frontier models"

A local model's raw *intelligence* is fixed by its weights — no wrapper makes a
local model reason like the latest Claude/GPT. What the hub **can** do is make a
local model far more *capable* by giving it these free tools, and let you get
frontier-quality results only where it matters, cheaply:

> **The free/near-frontier pattern** — set the coordinator to your local model,
> so all the browsing, searching, file work, and orchestration runs at zero
> token cost. The local brain does the legwork and only calls
> `consult(claude|gpt, …)` or `collaborate(...)` for the genuinely hard
> reasoning. You pay tokens only for the hard parts, not the gathering.

```yaml
orchestrator:
  coordinator: local          # free brain does the legwork + tool use
  collab_workers: [claude, gpt]
  synthesizer: claude         # paid models only for the hard reasoning
```

## Using it

Once running you get a prompt. Just describe the goal:

```
origin› check which processes are using the most memory, then write the top 5 to ~/top5.txt
origin› clone the repo at https://github.com/me/project and run its test suite
origin› using the github API, list my open pull requests
origin› read the CSV in ~/data, summarize it, and email-format the summary
```

The agent plans, calls tools across all three connectors, checks results, and
retries when something fails.

### REPL commands

| command | what it does |
|---|---|
| `/help` | show help |
| `/tools` | list every tool available (with on/DENIED status) |
| `/connectors` | show connector status (shell / REST / MCP) |
| `/allow <tool>` / `/deny <tool>` | control *what* the agent may use, live |
| `/models` | list model workers + per-worker call counts |
| `/coordinator <w>` | set which worker is the lead brain |
| `/consult <w> <q>` | ask one worker directly |
| `/collab <task>` | make all workers collaborate (add `:debate` / `:panel`) |
| `/profiles` | list behavior profiles |
| `/profile <name>` | switch profile (can swap the brain too) |
| `/system <text>` | set a custom system prompt live (replace the persona) |
| `/instruct <text>` | add a standing instruction that overrides defaults |
| `/instructions` / `/clearinstructions` | show / clear standing instructions |
| `/verbose` `/normal` `/quiet` | how much tool activity is shown |
| `/reset` | clear the conversation (persona + connectors stay) |
| `/model` | show the active brain |
| `/exit` | shut down |

### Controlling what and how

This is the "I control what I want and how" part:

```
origin› /profile planner            # switch to a plan-first persona
origin› /system You are my deploy bot. Never touch main. Ask nothing.
origin› /instruct Always use ripgrep, never grep.
origin› /instruct Write outputs to ~/hub-out/ only.
origin› /deny write_file            # this session, no writing files
origin› /allow write_file           # …changed my mind
origin› /verbose                    # show me full tool output
```

Standing instructions and custom prompts **override** the defaults, so you
steer both the method and the boundaries. You can also bake any of this into
config (`profiles`, `agent.system_prompt`, `agent.tool_allow/deny`).

---

## Configuration

Config resolution order: `--config PATH` → `$ORIGIN_CONFIG` →
`./origin.config.yaml` → `~/.origin/config.yaml`. Any `${VAR}` in the file is
expanded from the environment (a `.env` file is loaded automatically).

### Pick the brain

```yaml
llm:
  provider: anthropic        # anthropic | openai | ollama
  anthropic: { model: claude-sonnet-4-5, api_key_env: ANTHROPIC_API_KEY }
  openai:    { model: gpt-4o,            api_key_env: OPENAI_API_KEY }
  ollama:    { model: llama3.1,          base_url: http://localhost:11434/v1 }
```

### Add an MCP server

Every enabled server is launched over stdio and its tools appear automatically.

```yaml
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "${HOME}"]
    enabled: true
  git:
    command: uvx
    args: ["mcp-server-git", "--repository", "${HOME}/myrepo"]
    enabled: true
```

### Add any REST API (no code)

```yaml
rest_apis:
  github:
    base_url: https://api.github.com
    description: "GitHub REST API"
    headers:
      Authorization: "Bearer ${GITHUB_TOKEN}"
```

The agent then calls it as, e.g., `http_request(api="github", path="/user/repos")`
and the auth header is applied for you.

---

## How it's built

```
hub/
  cli.py            # terminal REPL + one-shot mode (rich UI)
  agent.py          # the reasoning loop (coordinator ⇄ tools)
  orchestra.py      # WorkerPool + multi-model collaboration engine
  config.py         # YAML config + ${ENV} expansion + .env
  llm/
    base.py         # provider-agnostic message/tool format
    anthropic_provider.py
    openai_provider.py   # also powers Ollama (OpenAI-compatible)
    __init__.py     # provider factory
  tools/
    base.py         # Tool primitive
    shell.py        # local OS: shell / read_file / write_file
    rest.py         # generic http_request connector
    mcp_client.py   # MCP manager (stdio, background event loop)
    models.py       # model-to-model tools (consult / collaborate)
    web.py          # web_search / web_fetch (token-free, pluggable backend)
    browser.py      # Playwright click-and-retrieve session + tools
    registry.py     # unifies every connector into one tool table
selftest.py         # offline test (no API key needed, 14 checks)
```

**Extending it** is deliberately simple:
- New API → add YAML under `rest_apis`.
- New MCP server → add YAML under `mcp_servers`.
- New built-in tool → add a `Tool(...)` in `hub/tools/` and register it.
- New LLM provider → subclass `LLMProvider`, add it to the factory.

---

## Testing

```bash
python selftest.py
```

Runs offline (no API key, no network): it verifies config loading, connector
bootstrap, live shell execution, file round-trips, and the full agent loop
driven by a scripted fake LLM.

---

## Safety notes

- **Autonomous** means no confirmation prompts. To make it cautious, you can
  set `agent.autonomous: false` in config and extend `registry.execute` with a
  confirmation hook, or run under a restricted user / container.
- Keep API keys and tokens in environment variables or `.env`, referenced via
  `${VAR}` — never paste them into prompts.
- MCP servers and REST APIs run with whatever credentials you give them; scope
  those tokens to the minimum access you need.
- The shell tool runs as the user who launched the hub. Consider a dedicated
  account, a container, or a VM when granting it broad reach.

---

*Built for Chris — Python, pluggable brain (Claude / OpenAI / Ollama), full
autonomous, wired for local shell + MCP + REST out of the box.*
