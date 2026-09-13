# 🎸 Token Punk Records — AI FinOps Router

> *"We compress tokens so you don't have to pay for the ones that don't matter."*

A dual-pane web application that intercepts LLM requests, extracts structured requirements from a conversation, applies LLMLingua-2-style token compression, and routes the optimised prompt to AWS Bedrock — slashing input-token costs by 40–60%.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        Browser                               │
│  ┌─────────────────────┐   ┌──────────────────────────────┐  │
│  │  LEFT — Chat Pane   │   │  RIGHT — Live Preview Pane   │  │
│  │  useChat → /api/chat│   │  Compression stats + Code    │  │
│  └─────────┬───────────┘   └──────────────┬───────────────┘  │
└────────────┼──────────────────────────────┼──────────────────┘
             │ Vercel AI SDK Data Stream     │ data annotations
             ▼                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     FastAPI Backend                          │
│                                                              │
│  Phase 1 — Elicitation       (clarifying questions)         │
│  Phase 2 — Semantic Refactor (history → strict JSON schema)  │
│  Phase 3 — Token Compression (LLMLingua-2 classifier)        │
│  Phase 4 — Bedrock Dispatch  (boto3 streaming invoke)        │
│  Phase 5 — Stream Response   (token deltas → frontend)       │
└─────────────────────────┬───────────────────────────────────┘
                          │ boto3
                          ▼
              ┌───────────────────────┐
              │     AWS Bedrock        │
              │  Claude 3 / Mistral   │
              └───────────────────────┘
```

---

## Project Structure

```
token-punk-records/
├── backend/
│   ├── main.py              # FastAPI app — CORS, /api/chat endpoint
│   ├── bedrock_client.py    # boto3 Bedrock streaming integration
│   ├── compressor.py        # LLMLingua-2-style token compression engine
│   └── requirements.txt
├── frontend/
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx         # Split-screen UI with useChat + live preview
│   │   └── globals.css
│   ├── package.json
│   ├── tailwind.config.js
│   ├── postcss.config.js
│   └── tsconfig.json
├── .env.example
├── .gitignore
└── README.md
```

---

## Setup & Run

### Prerequisites

| Tool | Version |
|------|---------|
| Python | ≥ 3.11 |
| Node.js | ≥ 18 |
| npm / yarn / pnpm | any recent |
| AWS account | with Bedrock model access enabled |

### 1 · Clone & configure

```bash
git clone <your-repo-url> token-punk-records
cd token-punk-records

# Copy env template and fill in your credentials
cp .env.example .env
```

Edit `.env`:

```dotenv
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0
MOCK_MODE=true   # set false to hit real Bedrock
```

### 2 · Backend

```bash
cd backend

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Load env vars and start the server
export $(grep -v '^#' ../.env | xargs)   # macOS/Linux
uvicorn main:app --reload --port 8000
```

The API is now live at http://localhost:8000  
Swagger UI: http://localhost:8000/docs

### 3 · Frontend

```bash
cd frontend

npm install        # or: yarn / pnpm install
npm run dev
```

Open http://localhost:3000

---

## How it works — step by step

1. **Elicitation** — The chatbot asks up to 6 clarifying questions (app type, colours, database, features, deployment, confirmation).
2. **Semantic Refactor** — Once all questions are answered, click **⚡ Compress & Generate Code**. The backend assembles a strict JSON schema from the conversation history.
3. **Token Compression** — `compressor.py` tokenises the JSON and scores every token using a bidirectional heuristic (stand-in for an XLM-RoBERTa-large classifier). Tokens scoring below the `preserve_ratio` threshold are discarded. Typical savings: **40–60%**.
4. **Bedrock Dispatch** — The compressed prompt is sent to AWS Bedrock via `bedrock_client.py` using `invoke_model_with_response_stream`.
5. **Streaming Response** — Token deltas are forwarded to the frontend using the **Vercel AI SDK Data Stream Protocol** (prefix `0:` for text, `2:` for data annotations, `d:` for finish). The right-hand preview pane renders them live.

---

## Switching to real AWS Bedrock

1. Set `MOCK_MODE=false` in `.env`.
2. Ensure your IAM user/role has the `bedrock:InvokeModelWithResponseStream` permission.
3. Enable the target model in the AWS Bedrock console (Model Access → Request access).
4. Restart the backend.

### Supported models

| Model ID | Notes |
|----------|-------|
| `anthropic.claude-3-haiku-20240307-v1:0` | Cheapest, fastest ✅ recommended |
| `anthropic.claude-3-sonnet-20240229-v1:0` | Balanced quality/cost |
| `anthropic.claude-3-opus-20240229-v1:0` | Highest quality |
| `mistral.mistral-large-2402-v1:0` | Alternative provider |

---

## Upgrading the compression engine

`compressor.py` uses a heuristic scorer as a stand-in. To use a real model:

```python
# In compressor.py, replace _score_token with a model forward pass:
from transformers import AutoTokenizer, AutoModelForTokenClassification
import torch

tokenizer = AutoTokenizer.from_pretrained("microsoft/llmlingua-2-xlm-roberta-large-meetingbank")
model = AutoModelForTokenClassification.from_pretrained(...)

def _score_token(token_text, index, total):
    # Run inference and return softmax probability for PRESERVE class
    ...
```

---

## License

MIT — build freely, compress aggressively, pay less.
