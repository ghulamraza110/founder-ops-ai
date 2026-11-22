# Startup Incorporation & Compliance Engine

A small app that takes a US startup from a plain-English description to drafted incorporation documents and a cap table. Founders describe their company in a chat. A Manager Agent pulls out the terms and asks about anything missing or invalid. Once the terms are complete, two builder agents draft the legal documents and build the cap table at the same time. Every founder then approves each document, and every step is logged.

Built with FastAPI, LangGraph, Pydantic v2, SQLAlchemy (SQLite) and Streamlit.

> The documents it drafts are not legal advice. Have a lawyer review them before you sign or file anything.

## How it works

```
founder chat ─> Manager Agent ──(terms incomplete)──> follow-up questions, wait for the next message
                     └──(terms_ready)──┬─> Legal Drafter ─────┐
                                       └─> Cap Table Builder ─┴─> documents + cap table + exports
```

All three agents live in one LangGraph graph ([app/agents/graph.py](app/agents/graph.py)).

### 1. Manager Agent ([manager_agent.py](app/agents/manager_agent.py))

- **The LLM extracts, Python decides.** The LLM reads the conversation and fills in the terms: company name, entity type (`LLC` or `Delaware C-Corp`), founders, equity %, vesting months, cliff months and incorporation date. All validation then runs in plain Python:
  - every required field is present
  - founder equity adds up to exactly 100%
  - each founder's cliff is no longer than their vesting period
- **IRS 83(b) deadline:** incorporation date + 30 days. This assumes founder stock is granted on the incorporation date. If the deadline falls on a weekend, the summary says so.
- **Follow-ups:** if anything is missing or wrong, it asks short follow-up questions and stops there. It remembers the case between messages, so founders can answer over several turns.
- **Default vesting:** if a founder's vesting isn't stated, it uses 48 months with a 12-month cliff and says so in the summary.
- **Hand-off:** once everything is valid, it sets `terms_ready = True` and both builders run in parallel.

### 2. Legal Drafter ([legal_drafter.py](app/agents/legal_drafter.py))

Uses the LLM to draft three Markdown documents from the locked terms:

- **Founder IP Assignment Agreement**
- **Corporate Bylaws Summary.** For an LLC this becomes an Operating Agreement summary.
- **IRS Section 83(b) Election Form**, one per founder, pre-filled with name, share count, vesting terms and the 30-day deadline.

Share counts come from the cap-table code, so the documents and the cap table always match. Details it isn't given (address, SSN, fair market value) are left as placeholders such as `[ADDRESS]`.

### 3. Cap Table Builder ([cap_table_builder.py](app/agents/cap_table_builder.py))

No LLM; the results are fully deterministic.

- **Share counts:** splits 10,000,000 authorized shares by equity %. Rounding always adds up to exactly 10,000,000, even for uneven splits like three founders at a third each. An LLC counts "units" instead of shares.
- **Vesting:** a monthly schedule for each founder. Nothing vests before the cliff. At the cliff the portion earned so far vests at once (25% for a 1-year cliff on 4-year vesting), then the rest vests monthly.
- **Exports:** writes `cap_table.json`, `cap_table.csv`, `vesting_schedule.csv` and `cap_table.xlsx` (two sheets) to `exports/`.

### Two ways to start a case

- **Chat** (UI "New case from chat", or `POST /cases/chat`): the full three-agent graph. The locked terms are copied onto the case and its founders in the database.
- **Form** (UI "Founders" tab, then `POST /cases/{id}/run`): you enter founders by hand. The same Python checks run, then both builders. No LLM is used to read the terms.

### Multi-party sign-off ([app/state_machine.py](app/state_machine.py))

Each document has a `version` (starting at 1) and `approvals`, which maps each founder's name to the version they signed, or `null`. This part is plain Python, with no LLM.

```
DRAFT ──approve──> PENDING_APPROVAL ──every founder signed the current version──> EXECUTED
  │                   │      ▲                                                        │
  └──edit─────────────┴──> INVALIDATED ──(signatures reset)──┘ <─────────edit──────────┘
```

- **Approve** sets `approvals[founder] = version`. The document becomes `EXECUTED` once every founder has signed the current version; until then it stays `PENDING_APPROVAL`. Approving twice changes nothing.
- **Edit** saves new Markdown and adds 1 to the version.
  - Every other founder's approval is reset to `null`. The editor's own approval moves to the new version, because editing counts as signing it.
  - The document passes through `INVALIDATED` and back to `PENDING_APPROVAL`. (A sole founder's edit goes straight to `EXECUTED`.)
  - An audit entry records who edited, the version change and whose signatures were invalidated, for example:
    `{"document": "Corporate Bylaws Summary", "version": "v1 -> v2", "edited_by": "Bob Lee", "invalidated_signatures": ["Alice Khan"]}`
  - Editing an `EXECUTED` document is allowed. It invalidates the other signatures in the same way.
- **Redrafting:** if the agents run again (a new chat message after the terms were locked, or the form's run button), each document gets a new version of the same record and **every** signature is reset.
- **Who can act:** only the case's founders can approve or edit. Anyone else gets HTTP 400.
- **Every status change** is written to the audit log.

### Case status

```
intake ──> needs_info         (missing or invalid terms; answer the questions or fix the founders, then retry)
       ──> awaiting_approval ──every document EXECUTED──> ready_to_file
```

If a document is edited after the case reached `ready_to_file`, the case goes back to `awaiting_approval`.

## Project structure

```
app/
  llm.py                 Shared LLM setup (DeepSeek or Groq)
  models.py              SQLAlchemy models, database setup, audit helper
  schemas.py             Pydantic v2 request/response schemas
  agents/
    state.py             Shared graph state, term schemas, export paths
    manager_agent.py     Manager Agent: extract, validate, 83(b) deadline, follow-ups
    legal_drafter.py     Builder 1: LLM-drafted legal documents
    cap_table_builder.py Builder 2: shares, vesting schedule, CSV/Excel/JSON export
    graph.py             The three-agent LangGraph graph
  workflow.py            Entry points the API calls (chat and form), SQLite chat memory
  state_machine.py       Multi-party sign-off: approve, edit, DRAFT/PENDING_APPROVAL/INVALIDATED/EXECUTED
  main.py                FastAPI app; creates the database on startup
ui/
  streamlit_app.py       Streamlit front end
test_graph.py            Command-line tester for the graph
exports/                 Generated files, one folder per case (git-ignored)
.env.example             Template for your settings
requirements.txt
```

### Database tables

| Table | Columns |
|---|---|
| `startup_cases` | id, name, entity_type, state, incorporation_date, status |
| `founders` | id, case_id, name, email, equity_percent, vesting_months, cliff_months |
| `documents` | id, case_id, doc_type, version, content_md, status, approvals_json |
| `audit_logs` | id, case_id, actor, action, details, timestamp |

The app database (`founder_ops.db`) is created automatically when the API starts. Chat history is stored separately in `checkpoints.db`, so conversations survive an API restart.

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
| `CHECKPOINT_DB` | SQLite file for chat memory | `checkpoints.db` |
| `EXPORTS_DIR` | Where generated files go | `./exports` |
| `API_URL` | Where the UI finds the API | `http://localhost:8000` |

### LLM providers

Both providers are called through `langchain_openai.ChatOpenAI`, because both offer OpenAI-compatible APIs. No requests go to OpenAI or Anthropic.

| Provider | Base URL | Default model |
|---|---|---|
| DeepSeek (main) | `https://api.deepseek.com` | `deepseek-chat` |
| Groq (used for testing for now) | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` |

To switch, change `LLM_PROVIDER` in `.env`. No code changes are needed.

Notes on Groq:

- `openai/gpt-oss-120b` is an open-weights model hosted by Groq.
- On a free Groq account, Llama 3.3 wasn't available, and Qwen's output limit (1,000 tokens per minute) was too small to draft a document.
- With gpt-oss, term extraction uses JSON-schema structured output, because its tool-calling mode failed.
- One completed chat uses 4 LLM calls (1 extraction + 3 documents), so many runs in quick succession can hit the free-tier rate limit.

## Running

Start the API and the UI in two separate terminals:

```bash
uvicorn app.main:app --reload
streamlit run ui/streamlit_app.py
```

- API: http://localhost:8000 (interactive docs at `/docs`)
- UI: http://localhost:8501. Click **New case from chat** and describe your startup.

### Command-line tester

```bash
python test_graph.py --unit   # deterministic checks, no API calls
python test_graph.py --ping   # one tiny LLM call to check your key and model
python test_graph.py --demo   # scripted two-message chat through the full graph
python test_graph.py          # chat with the Manager Agent yourself
```

The command-line tester saves its files to `exports/<company-name>/`. The API saves to `exports/case-<id>/`.

## API endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/cases/chat` | `{"message": "...", "case_id": 1}` runs the Manager/Builder graph on Groq. Leave out `case_id` to start a new case; the response includes it. |
| GET | `/cases/{id}` | Everything about a case: terms, 83(b) deadline, cap table, documents (status, version, approvals, pending signers) and the audit log |
| POST | `/documents/{id}/approve` | `{"founder_name": "Alice Khan"}` signs the current version |
| POST | `/documents/{id}/edit` | `{"founder_name": "Bob Lee", "content_md": "..."}` saves a new version; returns the document and `invalidated_signatures` |
| GET | `/cases/{id}/cap-table/download` | `?format=xlsx` (default), `csv`, `vesting_csv` or `json` |
| GET | `/health` | Health check |
| POST | `/cases` | Create an empty case (form path) |
| GET | `/cases` | List cases |
| POST | `/cases/{id}/founders` | Add a founder (form path; names must be unique) |
| POST | `/cases/{id}/run` | Form path: validate the founders, then run both builders |
| GET | `/cases/{id}/chat` | Chat history |
| GET | `/cases/{id}/exports` | List generated files |
| GET | `/cases/{id}/exports/{file}` | Download one generated file (including the Markdown documents) |

The first five are the main endpoints; the rest support the form path and the UI.

If the LLM provider fails (no balance, rate limit, missing key), the API returns HTTP 502 with the provider's error message.

## What has been tested

- **Logic checks** (`test_graph.py --unit`, 11/11 passing): equity validation, required-field questions, cliff longer than vesting, default vesting, the 83(b) date (including across a year end), 6,000,000/4,000,000 shares for a 60/40 split, an exact 10,000,000 total for a three-way split, and the cliff and full-vesting dates.
- **Full API flow** (23/23 passing, with the LLM replaced by a stub so no credits were spent): a chat that needs more information, then one that locks the terms; case and founders copied from the chat; chat history kept after a restart; cap table and Excel download; `ready_to_file`; form path with an LLC (units, its own state); exports kept separate per case; HTTP 502 on an LLM error; unknown files blocked.
- **Sign-off state machine** (37/37 passing, LLM stubbed):
  - new documents start as `DRAFT` at v1 with every approval `null`
  - a partial sign-off stays `PENDING_APPROVAL`; approving twice changes nothing
  - an edit bumps the version, resets the other founders, keeps the editor's signature and lists who lost theirs
  - the audit log shows who edited, whose signatures were invalidated, and the `INVALIDATED -> PENDING_APPROVAL` step
  - signatures on an old version don't count
  - `EXECUTED` once all founders sign the new version, and editing an `EXECUTED` document re-opens it
  - all documents `EXECUTED` makes the case `ready_to_file`, and a later edit sends it back to `awaiting_approval`
  - redrafting keeps the same document record and resets every signature
  - a sole founder's edit executes immediately
  - non-founders, empty edits and unchanged edits are rejected
  - cap table downloads work in all four formats
- **Live with Groq** (real API server and Streamlit UI):
  - The first message got the two expected follow-up questions (missing date; equity at 90%).
  - The second locked the terms with the 83(b) deadline set to 2026-12-31, drafted all three documents, and produced the cap table.
  - In the UI, Alice approved the Bylaws. Bob then edited them, which moved them to v2, reset Alice's signature and wrote the audit entry shown above.
  - No errors appeared in the UI.
- **DeepSeek:** the key is accepted, but the account has no balance yet (HTTP 402).

The API and sign-off tests ran from temporary scripts; they aren't committed as a test suite yet.

## Known limitations

- **US only:** Delaware C-Corp or LLC. Pakistan (SECP) and other countries aren't supported.
- **No authentication:** anyone using the API or UI can approve or edit as any founder by passing their name. A real deployment would need login and e-signatures.
- **Old databases:** documents created before this change, with list-style approvals and lowercase statuses, are read as unsigned `DRAFT`. Delete `founder_ops.db` for a clean start.
- **Database:** SQLite only, and tables are created with `create_all` (no migrations).
- **Outdated files:** re-running the workflow overwrites the files in `exports/`. Older document versions stay in the database.
