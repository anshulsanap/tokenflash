# TokenQuick — Project Context

## Philosophy
Zero Cloud Spend & Zero Local Waste. No external API calls, ever, for inference.
Everything must run against local Ollama models on the user's own machine.

## Current stack
- Frontend: React + Vercel AI SDK (chat state, streaming)
- Backend: Python, FastAPI + Uvicorn
- Inference: Local Ollama — Llama 3.2 3B (general, 128K ctx), Qwen 2.5 Coder 7B (code, 32K ctx)
- Telemetry pulls real prompt_eval_count / eval_count directly from Ollama logs — never estimate
  token usage when ground truth is available.

## Built already
- Heuristic (non-AI) prompt compression: regex/stopword/whitespace trimming pre-inference
- SUBTASK_MAX_TOKENS = 8192 hard execution limit via num_predict
- Real-time dashboard: compression report, real token usage, cloud-cost-avoided estimate

## Product positioning
Single-machine, single-user or small-business tool. NOT a multi-tenant enterprise gateway
(that market is already served by Bifrost, Azure AI Gateway, etc.) — our differentiation is
provable on-device privacy, not fleet-scale governance. Every feature should reinforce
"nothing left this machine," not add cloud dependencies or multi-tenant complexity.

## Conventions
- All new backend logic goes in FastAPI, no new services unless a phase explicitly calls for one.
- Every new pipeline stage must be independently toggleable and independently benchmarkable
  (we need before/after numbers for each stage in the dashboard).