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
- **Conflicts:** approve and edit carry the version the founder was looking at (`version` / `base_version`). If the document has moved on, the API returns **HTTP 409** and changes nothing. Truly simultaneous requests are caught by database-level optimistic locking.

Why it works this way, and how edit conflicts are handled, is written up in [ADR-001-signoff-state-machine.md](ADR-001-signoff-state-machine.md).

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
ADR-001-signoff-state-machine.md  Design record for the sign-off state machine
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
- UI: http://localhost:8501

### Demo walkthrough

The UI ([ui/streamlit_app.py](ui/streamlit_app.py)) has a sidebar and four tabs:

- **Sidebar:**
  - **Active User** switcher (Founder A / Founder B / …). Approvals and edits are made as this founder.
  - **Compliance Deadlines** panel: a countdown of days left to file the 83(b) election, the 30-day progress, the 83(b) form's sign-off status and the next vesting cliff.
- **Manager Chat:** negotiate entity type, equity and vesting with the Manager Agent. The tab shows which LLM is active (from `/health`).
- **Cap Table:** shares, ownership %, shares vested today, a month-by-month vesting schedule per founder, and Download CSV / Excel buttons.
- **Document Review & Sign-Off:**
  - Pick a document to see its version, status badge and each founder's signature.
  - **Approve as [Active User]** signs the current version.
  - **Edit Clause** lets you rewrite one section. Saving creates the next version, and the other founder's card turns to ❌ "Signature invalidated by …'s edit".
- **Audit Trail:** every approval, edit, signature invalidation and status change, oldest first, with filters.

A 3-minute demo:

1. Click **New case** and paste the example prompt shown on screen. The Manager Agent asks for the missing date and points out that equity adds to 90%.
2. Reply: `Bob has 40%. Incorporation date 2026-12-01.` The terms lock, the 83(b) countdown appears, and the documents and cap table are generated.
3. **Cap Table:** show the 6,000,000 / 4,000,000 split and the vesting schedule, then download the Excel file.
4. **Document Review:** as Founder A, approve the Bylaws.
5. Switch to Founder B, edit a clause and save. Founder A's signature turns to ❌ invalidated, and the document is now v2.
6. **Audit Trail:** show the approval, the edit and the invalidation.
7. Switch back to A and approve. Then, as B, approve too: the document becomes **EXECUTED**.

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
| POST | `/documents/{id}/approve` | `{"founder_name": "Alice Khan", "version": 1}` signs that version; 409 if the document has moved on |
| POST | `/documents/{id}/edit` | `{"founder_name": "Bob Lee", "content_md": "...", "base_version": 1}` saves a new version; returns the document and `invalidated_signatures`; 409 if `base_version` is stale |
| GET | `/cases/{id}/cap-table/download` | `?format=xlsx` (default), `csv`, `vesting_csv` or `json` |
| GET | `/health` | Health check, plus the active LLM provider and model |
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
- **Sign-off state machine** (43/43 passing, LLM stubbed):
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
  - approving or editing a stale version returns 409 and changes nothing
  - a true race (two sessions, one edits while the other approves) raises a conflict instead of applying the stale signature
- **Live demo with Groq** (real API server driving the Streamlit UI):
  - The first message got the two expected follow-up questions.
  - The second locked the terms. The sidebar showed the 83(b) countdown ("28 days left", due Dec 20, 2026) and the next cliff date.
  - Cap Table showed 10,000,000 shares and the three download buttons.
  - Founder A approved the Bylaws. The switcher was moved to Founder B, who edited a clause and saved v2. Founder A's card turned to "Signature invalidated by Bob Lee's edit (v1 → v2)".
  - Audit Trail listed the approval, the edit, the invalidation and every status change in order.
  - No errors appeared in the UI.
- **DeepSeek:** the key is accepted, but the account has no balance yet (HTTP 402).

The API and sign-off tests ran from temporary scripts; they aren't committed as a test suite yet.

## Known limitations

- **US only:** Delaware C-Corp or LLC. Pakistan (SECP) and other countries aren't supported.
- **No authentication:** anyone using the API or UI can approve or edit as any founder by passing their name. A real deployment would need login and e-signatures.
- **Old databases:** documents created before this change, with list-style approvals and lowercase statuses, are read as unsigned `DRAFT`. Delete `founder_ops.db` for a clean start.
- **Database:** SQLite only, and tables are created with `create_all` (no migrations).
- **Outdated files:** re-running the workflow overwrites the files in `exports/`. Older document versions stay in the database.
