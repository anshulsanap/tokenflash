# ⚡ TokenQuick — Local AI, Optimized

> *"Capable AI on your own laptop — $0 API cost, tokens optimized so a small model does more."*

TokenQuick is a **fully local** AI assistant with a dual-pane web UI. It runs
open-source models on your own machine via **Ollama** — no cloud, no per-token
bill — and layers **requirement scoping, token compression, and cost-aware
model routing** on top so a small local model punches above its weight.

It does two things automatically, depending on what you ask:

- **Build** software — a website, CLI tool, API, script, etc. → generates the code.
- **Perform** a task — research, structured notes, summaries, a poem, analysis →
  produces the actual result.

---

## Why

Cloud LLMs (GPT-4, Claude) charge per token, and most "cost optimizer" tools
*still call the expensive cloud model to do the optimizing* — you spend tokens to
save tokens. For a student or a small team, that bill is a barrier.

TokenQuick's bet: **every engineer has a decent laptop.** Run the model locally
for free, and optimize the prompt so the small local model goes further.

---

## How it works

```
┌───────────────────────────── Browser (dual-pane UI) ──────────────────────────┐
│  LEFT — Chat (Pre-Flight Scoping)        RIGHT — Live report                   │
│  interviews you to lock requirements     compression • routing • real tokens • │
│                                          the finished code / notes             │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                         │  Vercel AI SDK data-stream protocol
                                         ▼
┌──────────────────────────── FastAPI backend ───────────────────────────────────┐
│  1. Elicitation      — scope requirements (one question at a time)              │
│  2. Compression      — LLMLingua-2-style token compression (drop filler)        │
│  3. Intent routing   — BUILD (code pipeline) vs PERFORM (direct answer)         │
│  4. Decompose+route  — split into subtasks, route to cheapest capable model     │
│  5. Generate         — stream result; report REAL local token counts            │
└───────────────────────────────────────┬────────────────────────────────────────┘
                                         │  local HTTP
                                         ▼
                          ┌──────────────────────────────┐
                          │           Ollama              │
                          │  llama3.2:3b   (light tier)   │
                          │  qwen2.5-coder:7b (heavy tier)│
                          └──────────────────────────────┘
```

**The three differentiators**

1. **Pre-Flight Scoping** — instead of forwarding a vague prompt, TokenQuick
   interviews you first so a bad, expensive prompt is never processed.
2. **Physical token compression** — LLMLingua-2-style extractive compression
   drops low-signal filler while keeping technical terms; you watch dropped
   tokens struck through live.
3. **Cost-aware routing** — work is split into subtasks and routed to the
   cheapest local model that fits (light `llama3.2:3b` vs heavy `qwen2.5-coder:7b`).

Everything runs locally, so the UI reports **real token counts measured on-device**
and shows **the cloud cost you avoided** ($0 actually paid).

---

## Project structure

```
token-punk-records/
├── backend/
│   ├── main.py            # FastAPI app, /api/chat, elicitation + intent routing
│   ├── llm_provider.py    # provider dispatch: local (default) | bedrock
│   ├── local_client.py    # Ollama integration + real local token usage
│   ├── bedrock_client.py  # optional AWS Bedrock fallback
│   ├── compressor.py      # LLMLingua-2-style token compression engine
│   ├── task_router.py     # decompose → route → parallel execute → assemble
│   └── requirements.txt
├── frontend/              # Next.js + React dual-pane UI
│   └── app/page.tsx
├── .env.example           # copy to .env
├── run.sh                 # one-command setup + run
└── README.md
```

---

## Setup & run

### Prerequisites

| Tool    | Notes                                           |
|---------|-------------------------------------------------|
| Python  | ≥ 3.11                                          |
| Node.js | ≥ 18                                            |
| Ollama  | https://ollama.com/download (runs the models)   |

### 1 · Install Ollama and pull the models

```bash
# install Ollama (or use the macOS app from ollama.com)
curl -fsSL https://ollama.com/install.sh | sh

# pull the two local model tiers (~7 GB total, one-time)
ollama pull llama3.2:3b          # light tier
ollama pull qwen2.5-coder:7b     # heavy tier
```

### 2 · Configure

```bash
cp .env.example .env             # defaults to LLM_PROVIDER=local — no keys needed
```

### 3 · Run everything (one command)

```bash
./run.sh
```

This creates the Python venv, installs deps, installs frontend packages, and
starts both servers. Then open **http://localhost:3000**.

<details>
<summary>Manual run (two terminals)</summary>

```bash
# backend
cd backend && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a; . ../.env; set +a
uvicorn main:app --reload --port 8000

# frontend
cd frontend && npm install && npm run dev
```
</details>

---

## Using it

1. **Chat (left pane).** Say what you want — *"a CLI tool to rename files"* or
   *"research RAG and give me structured notes"*. It asks a couple of clarifying
   questions.
2. When it has enough, **⚡ Compress & Build Locally** appears (or say *"just do it"*).
3. **Right pane** shows the pipeline: the compression before/after diff, the model
   routing, **real local token usage**, cloud-cost-avoided, and the finished
   output — **code** (build) or **notes/text** (perform).

---

## Configuration (`.env`)

| Variable              | Default                        | Purpose                             |
|-----------------------|--------------------------------|-------------------------------------|
| `LLM_PROVIDER`        | `local`                        | `local` (Ollama) or `bedrock`       |
| `OLLAMA_HOST`         | `http://localhost:11434`       | Ollama endpoint                     |
| `ROUTER_MODEL_LIGHT`  | `llama3.2:3b`                  | light/fast tier                     |
| `ROUTER_MODEL_HEAVY`  | `qwen2.5-coder:7b`             | heavy/capable tier                  |
| `SUBTASK_MAX_TOKENS`  | `8192`                         | max output tokens per subtask       |
| `TOKENQUICK_OTEL_EGRESS` | `false`                     | opt-in gate for OTLP trace export (the one outbound path) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | *(unset)*              | OTLP-over-HTTP traces endpoint, used only when the gate is on |

AWS Bedrock is available as an optional fallback (`LLM_PROVIDER=bedrock` + AWS keys).

---

## Power / Energy telemetry (optional measured tier)

TokenQuick can attribute a per-request **average power (W)** and **energy (J)**
figure to each generation and stream it to the dashboard's Power / Energy panel.
By default it uses an **`estimated`** tier — `psutil` reads CPU utilization
locally (no network) and multiplies it by a TDP model. Every figure is tagged
`measured | estimated | unavailable`, so an estimate is never presented as a
measurement.

On Apple Silicon the true **`measured`** tier comes from the system
`powermetrics` binary, which requires root. TokenQuick **never** writes sudoers,
never invokes `sudo` interactively, and never prompts for a password during a
request. It only **detects** the measured tier via a gated, non-interactive
probe: it runs the probe *only* when you start the backend with
`POWER_TRY_MEASURED=1`, and even then uses `sudo -n` (non-interactive), which
fails closed if passwordless sudo is not configured. With the flag unset (the
default) the backend never invokes `sudo` at all and goes straight to the
`estimated` tier.

### Unlocking the measured tier (one-time, out-of-band operator opt-in)

To enable the `measured` tier, grant the backend's user passwordless `sudo` for
**only** the `powermetrics` binary. Create the snippet with
`sudo visudo -f /etc/sudoers.d/tokenquick-powermetrics` and add a single line
scoped to the exact binary path and one named user (replace `<youruser>`):

```
<youruser> ALL=(root) NOPASSWD: /usr/bin/powermetrics
```

> ⚠️ **This grants passwordless execution of `/usr/bin/powermetrics` only — not
> general `sudo`.** Scoping the `NOPASSWD` rule to that exact binary path (and a
> single user) matters because it limits blast radius: a blanket
> `NOPASSWD: ALL` would hand the account passwordless root for *every* command,
> whereas this rule authorizes just the one power-telemetry binary. Keep the
> scope this tight — never widen it to `ALL`.

The backend invokes `powermetrics --samplers cpu_power -n 1 -i 200`; the
`/usr/bin/powermetrics` scoping above covers that invocation. Once the snippet
is in place, the next backend startup (with `POWER_TRY_MEASURED=1`) runs the
probe, finds `sudo -n powermetrics` succeeds within its timeout, and selects the
`powermetrics` source at the `measured` tier.

**Security caveat:** even the `measured` tier requires this explicit,
out-of-band operator action. TokenQuick never creates or modifies the sudoers
file, never prompts, and defaults to the `estimated` tier via `psutil` when the
snippet is absent. Power telemetry is independently toggleable (Power Telemetry
switch in the settings panel) like the other pipeline stages.

---

## Distributed tracing (OpenTelemetry)

TokenQuick emits standard **OpenTelemetry trace spans** for the generate
pipeline. Every `/api/chat` generate request produces one parent
`tokenquick.generate` span with child spans for each stage: **redaction**,
**cache lookup**, **compression**, **inference**, and **power attribution**.

By default this makes **zero outbound network calls** — consistent with
TokenQuick's 100%-local, $0, deny-outbound identity. Spans are written to a
**local, append-only JSONL file** at `backend/logs/traces.jsonl` (the same
append-only discipline as the other telemetry logs). Span attributes carry only
the same scalars the existing JSONL logs record — counts, scores, quality flags,
token counts — and **never** raw prompts, redacted text, or generated code.

### Opt-in OTLP export (the single sanctioned egress)

To view traces in a collector such as Jaeger or a Prometheus/Tempo stack, set
**both** environment variables before starting the backend:

```bash
export TOKENQUICK_OTEL_EGRESS=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces
```

`OTEL_EXPORTER_OTLP_ENDPOINT` is the standard OTLP-over-HTTP traces endpoint.
When the gate is off (the default), the OTLP exporter is **never even imported**
and no endpoint is contacted — it fails closed to local-only.

A minimal way to see traces: run a local Jaeger all-in-one container that exposes
OTLP on port `4318`, start the backend with the two env vars above set, then open
the Jaeger UI and look for the `tokenquick.generate` traces. (The collector-run
details are generic on purpose — you bring your own collector.)

**Security note:** this OTLP path is the **one** outbound network connection
tracing can make, and it is **OFF by default**. Even with it on, span attributes
contain only scalar metadata — the same zero-raw-value guarantee as the JSONL
logs. No prompt content ever leaves the machine.

---

## Tech stack

Next.js + React (frontend) · FastAPI + Python (backend) · Ollama running
Llama 3.2 3B and Qwen 2.5 Coder 7B locally · streaming over the Vercel AI SDK
data-stream protocol.

---

## Honest notes

- **Compression is a heuristic** classifier inspired by LLMLingua-2 (token scoring
  + threshold), a stand-in for the paper's fine-tuned XLM-RoBERTa model; the
  "Real Local Token Usage" panel shows ground-truth counts from the model itself.
- **Local models are smaller than frontier cloud models** and can't browse the web,
  so "perform" tasks answer from the model's own knowledge (it's told to flag
  uncertainty rather than invent facts). The win is *free, private, offline, and
  good enough for a huge range of tasks* — not beating GPT-4 on raw quality.
- The bigger cost lever is **running locally at $0** vs. the cloud; input
  compression is a smaller, honestly-reported lever.

---

## License

MIT — build freely, run locally, pay nothing.
