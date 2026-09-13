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

AWS Bedrock is available as an optional fallback (`LLM_PROVIDER=bedrock` + AWS keys).

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
