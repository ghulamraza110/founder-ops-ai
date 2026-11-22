# Startup Incorporation & Compliance Engine

A small app that takes a startup from founder details to approved incorporation documents. It checks the founders' equity and vesting terms, uses an LLM to draft the documents, collects an approval from every founder, and logs each step.

Built with FastAPI, LangGraph, Pydantic v2, SQLAlchemy (SQLite) and Streamlit.

> The documents it drafts are not legal advice. Have a lawyer review them before you file anything.

## How it works

1. **Create a case:** company name, entity type (C-Corp, LLC or S-Corp), state and incorporation date.
2. **Add founders:** name, email, equity %, vesting months and cliff months.
3. **Run the workflow.** A LangGraph graph runs two steps:
   - **Compliance check** (fixed rules, no LLM): equity must add up to 100%, the cliff can't be longer than the vesting period, and emails must look valid. If any rule fails, the case is marked `blocked` and the problems are listed.
   - **Drafting** (LLM): writes a Certificate of Incorporation, Bylaws and a Founder Stock Purchase Agreement in Markdown. The case moves to `awaiting_approval`.
4. **Approve:** a document becomes `approved` once every founder has approved it. When the latest version of every document is approved, the case becomes `ready_to_file`.
5. **Audit log:** every action is recorded with who did it and when.

If you run the workflow again, it saves new versions of the documents (v2, v3, …) and keeps the old ones.

### Case status

```
intake ──run──> blocked             (compliance issues found; fix them and run again)
       ──run──> awaiting_approval ──every founder approves──> ready_to_file
```

## Project structure

```
app/
  llm.py             Shared LLM setup (DeepSeek or Groq)
  models.py          SQLAlchemy models, database setup, audit helper
  schemas.py         Pydantic v2 request/response schemas
  agents/
    compliance.py    Rule-based compliance checks
    drafter.py       LLM document drafter
  state_machine.py   LangGraph workflow (compliance -> draft)
  main.py            FastAPI app; creates the database on startup
ui/
  streamlit_app.py   Streamlit front end
.env.example         Template for your settings
requirements.txt
```

### Database tables

| Table | Columns |
|---|---|
| `startup_cases` | id, name, entity_type, state, incorporation_date, status |
| `founders` | id, case_id, name, email, equity_percent, vesting_months, cliff_months |
| `documents` | id, case_id, doc_type, version, content_md, status, approvals_json |
| `audit_logs` | id, case_id, actor, action, details, timestamp |

The SQLite file (`founder_ops.db` by default) is created automatically the first time the API starts.

## Setup

You need Python 3.12 (the version this was built and tested with).

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
cp .env.example .env          # then fill in your keys
```

### Settings (`.env`)

| Variable | Purpose | Default |
|---|---|---|
| `LLM_PROVIDER` | `deepseek` or `groq` | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek API key | — |
| `GROQ_API_KEY` | Groq API key | — |
| `LLM_MODEL` | Optional: use a different model | provider default |
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:///./founder_ops.db` |
| `API_URL` | Where the UI finds the API | `http://localhost:8000` |

### LLM providers

Both providers are called through `langchain_openai.ChatOpenAI`, because both offer OpenAI-compatible APIs. No requests go to OpenAI or Anthropic.

| Provider | Base URL | Default model |
|---|---|---|
| DeepSeek (main) | `https://api.deepseek.com` | `deepseek-chat` |
| Groq (fallback for testing) | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` |

The Groq default is `openai/gpt-oss-120b`, an open-weights model hosted by Groq. On a free Groq account, Llama 3.3 wasn't available and Qwen's output limit (1,000 tokens per minute) was too small to draft a document. Drafting makes three LLM calls in a row, so running the workflow several times quickly can hit Groq's free-tier rate limit.

## Running

Start the API and the UI in two separate terminals:

```bash
uvicorn app.main:app --reload
streamlit run ui/streamlit_app.py
```

- API: http://localhost:8000 (interactive docs at `/docs`)
- UI: http://localhost:8501

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/cases` | Create a case |
| GET | `/cases` | List cases |
| GET | `/cases/{id}` | Case details, including founders |
| POST | `/cases/{id}/founders` | Add a founder |
| POST | `/cases/{id}/run` | Run the compliance check and drafting workflow |
| GET | `/cases/{id}/documents` | List all document versions |
| POST | `/documents/{id}/approve` | Approve a document as `{"approver": "<founder email>"}` |
| GET | `/cases/{id}/audit` | Audit log |

## What has been tested

- **Requirements check (39/39 passing):** files and folders, dependencies, DeepSeek settings, table columns, database created on startup, and the full API flow: a case is blocked when equity is wrong, drafting runs when it's right, one founder's approval isn't enough, all founders' approvals make the case `ready_to_file`, re-running creates v2 documents, bad input returns 422, and an unknown case returns 404. The LLM was replaced with a stub for this run so no API credits were spent.
- **Live drafting with Groq:** produced all three documents (about 3k–12k characters each).
- **Streamlit UI:** loads against a running API with no errors, and shows a clear message when the API can't be reached.
- **DeepSeek:** the key is accepted, but the account has no balance yet (HTTP 402). Once it's topped up, set `LLM_PROVIDER=deepseek`.

There's no automated test suite in the repo yet.

## Known limitations

- An LLM error (no balance, rate limit) shows up as a generic HTTP 500 from `/cases/{id}/run`.
- No authentication: anyone using the UI can approve as any founder.
- SQLite only, and tables are created with `create_all` (no migrations).
