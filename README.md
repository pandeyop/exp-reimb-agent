# Expense & Reimbursement Agent

> **Drop your receipts. Answer only what the AI couldn't determine. Submit.**

An AI-native expense assistant. It turns a pile of unstructured receipts (photos, scans, PDFs) into a validated, policy-checked reimbursement claim. The employee does as little work as possible.

![Processed claim](docs/screenshots/01-processed-claim.jpg)

---

## 1. Problem

Expense claims are mostly data entry: retyping merchant, date, amount and GST from receipts into a 20-field form, then reading the policy to work out what will be rejected. Employees hate it. Finance teams then reject claims for missing purposes, over-limit hotels and duplicate receipts.

## 2. Solution overview

The employee drops all their documents at once. The agent then:

1. **Reads every document**: multi-page PDFs, rotated and blurry photos, app screenshots.
2. **Extracts** more than 25 fields, each with a confidence score (merchant, GSTIN, totals, taxes, routes, nights, covers and more).
3. **Classifies** each expense into a policy category and explains why.
4. **Infers** what it reasonably can. It reads the trip purpose from the flight booking and applies it to the other trip expenses, and it groups the expenses into a trip.
5. **Checks policy** from a JSON config: daily/nightly/monthly limits, required fields, stale and future dates.
6. **Detects duplicates** across the claim and against previously reimbursed expenses.
7. **Asks only what's missing**, in a chat, with consolidated questions and one-tap answers.
8. **Revalidates** after every answer or edit. The claim moves to **READY TO SUBMIT** on its own.
9. **Submits** to a mocked corporate reimbursement API, but only when the employee explicitly confirms.

In the bundled demo, 12 documents produce 146 extracted fields and 19 inferred fields. The employee answers 3 questions and confirms 1 duplicate, so **98% of manual data entry is avoided**.

## 3. Architecture

```
frontend/ (React + Vite)                 backend/ (FastAPI, Python 3.12)
┌─────────────────────────┐   REST    ┌──────────────────────────────────────────────┐
│ Receipts │ Claim │ Chat │ ───────▶ │ app/main.py          API + serves built UI    │
│  drop    │ dash  │ cards│          │ app/agent/                                   │
│  status  │ table │ input│          │   document_interpreter  Claude vision/PDF ⇄ offline │
│ Drawer: receipt+fields  │          │   normaliser            validates AI output       │
│ Review ▸ Submit ▸ Done  │          │   classifier            category + confidence + why│
└─────────────────────────┘          │   enrich                inference + trip grouping  │
                                     │   policy                PolicyEvaluator (JSON)     │
                                     │   duplicates            DuplicateDetector          │
                                     │   planner               MissingInfo + Clarification│
                                     │   claim_manager         state machine, revalidate  │
                                     │   chat_agent            Claude tool-use ⇄ rules    │
                                     │   submission            SubmissionTool             │
                                     │ app/mock_finance.py     mock corporate API         │
                                     │ SQLite (data/)  ·  uploads (data/uploads, private) │
                                     └──────────────────────────────────────────────┘
```

* **Persistence:** SQLite, with one JSON document per claim, document and expense, plus chat messages, events and mock submissions.
* **Processing:** uploads return immediately. Documents are processed by a small thread pool, and the UI polls while documents are being processed. When the last document in an upload batch finishes, the agent posts a summary and its questions.
* **Derived state is never stored as truth.** Every mutation runs `revalidate()`, which recomputes inferences, groups, policy results, duplicates, gaps, questions and statuses from the facts. Facts are what was extracted, what the user said and what the user decided.

## 4. AI-agent design

```
INGEST → UNDERSTAND → EXTRACT → NORMALISE → CLASSIFY → INFER/GROUP → CHECK POLICY
      → CHECK DUPLICATES → IDENTIFY GAPS → ASK MINIMAL QUESTIONS → UPDATE → REVALIDATE → READY
```

| Capability | Module | How |
|---|---|---|
| DocumentInterpreter | `agent/document_interpreter.py` | Claude (`claude-opus-5-5` by default) receives the image or PDF and must answer in a strict **JSON schema** (`output_config.format`). The schema has a value and confidence for every field, plus the document type, readability, line items, a category suggestion and a `multipleTotalsFound` flag. On failure it retries once, then falls back to offline mode. |
| Validation | `agent/normaliser.py` | Every AI value is type-coerced (money, dates, currency codes) and range-checked. Unreadable values are dropped with a note. Card numbers are masked. Multiple totals lower the confidence. |
| ExpenseClassifier | `agent/classifier.py` | Two independent signals are combined: the model's suggestion and a configurable evidence scorer (`config/categories.json`). When they agree, confidence goes up. When they disagree, confidence drops below the review threshold and the employee is asked to confirm. |
| Inference & grouping | `agent/enrich.py` | Trips are anchored on flight and hotel bookings. A business purpose found on a booking is inherited by in-trip expenses at 0.8 confidence, marked *inferred* and explained. Inheritance is **deliberately blocked** for large or multi-cover meals ("could be client entertainment"). Nights are computed from check-in and check-out, and hotel invoice dates from check-out. |
| PolicyEvaluator | `agent/policy.py` | Per-item, per-event, per-night, per-day and per-month limits. Required fields. Future dates and non-positive amounts are blocking. Stale dates, missing invoices, foreign currency and missing recommended fields are warnings. Every result is written in plain language: *Claimed ₹24,570 · Policy limit ₹22,500 (₹7,500/night × 3) · Excess ₹2,070*. |
| DuplicateDetector | `agent/duplicates.py` | Weighted signals: same file hash, invoice number, amount, date, merchant similarity, perceptual image hash and route. Each pair gets a score and an explanation. Expenses are also compared with mock history (`config/history.json`). Nothing is removed without the employee's confirmation. |
| MissingInformationDetector | `agent/planner.py` | Each field is classified as **KNOWN / INFERRED / LOW_CONFIDENCE / MISSING / NOT_REQUIRED** using configurable thresholds (≥0.90 accept, 0.70–0.89 inferred, <0.70 ask). |
| ClarificationPlanner | `agent/planner.py` | It asks only when a field is required, missing or uncertain, and not inferable. Questions are consolidated: one per expense, and one per trip for a shared purpose. Each question carries quick-reply options built from context, such as the trip's days for a smudged date. Policy decisions wait until missing information is answered, because the answer can change the category and its limit. |
| Conversational agent | `agent/chat_agent.py` | Quick replies are applied deterministically. Free text goes to **Claude tool use** with strict tools: `set_expense_fields`, `set_category`, `confirm_field`, `resolve_duplicate`, `resolve_over_limit`, `exclude_expense` and `rename_claim`. Updated claim state is fed back after each tool call. There is **no submit tool**. Without credentials, or if the call fails, a deterministic interpreter routes the answer to the best-fitting open question. It understands phrases like "client dinner with X and Y", "the 23rd", "Wednesday", "cap it" and "yes it's the same ride". |
| ClaimManager | `agent/claim_manager.py` | Owns the expense statuses (`PROCESSING, COMPLETE, NEEDS_INFORMATION, WARNING, POLICY_VIOLATION, POSSIBLE_DUPLICATE, EXCLUDED`) and the claim statuses (`DRAFT, PROCESSING, ACTION_REQUIRED, READY_TO_SUBMIT, SUBMITTED`). Also handles edits, exclusions and replacement uploads, and builds the dashboard snapshot. |
| SubmissionTool | `agent/submission.py` | Re-validates, requires `confirm: true`, builds the corporate payload and calls the mock finance API. A failure leaves the claim unchanged and READY. |

**Explainability.** Every expense shows *why the agent decided this*: the classification reason, each inference and its source, missing-information reasons, the duplicate explanation and the policy reasoning. Each field shows a source badge (`Read 97%`, `Inferred 80%`, `You`).

## 5. Setup

Prerequisites: Python 3.10+ and Node 18+.

**One command** (it installs dependencies, builds the UI and starts the server):

```bash
./run.sh            # macOS / Linux
```
```powershell
.\run.ps1           # Windows PowerShell
```

Then open **http://127.0.0.1:8000**.

### Running frontend and backend separately (development)

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload --port 8000      # API on :8000
cd ../frontend && npm install && npm run dev                          # UI on :5173 (proxies /api)
```

Run the tests with `cd backend && .venv/bin/python -m pytest -q`. There are 16 tests, including the full demo journey through the API and the Claude code paths with the API mocked.

## 6. Environment variables

See [`.env.example`](.env.example). The important ones:

| Variable | Default | Purpose |
|---|---|---|
| `EXTRACTION_MODE` | `auto` | `auto` = Claude if credentials exist, else offline. `claude` = always use Claude. `offline` = never call the API. |
| `ANTHROPIC_API_KEY` | – | Read by the Anthropic SDK. `ANTHROPIC_AUTH_TOKEN` and `ant auth login` profiles also work. It stays server-side only. |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model for extraction and chat. |
| `ANTHROPIC_EFFORT` | `low` | Effort level. Extraction is a routine task, so raise this only if accuracy needs it. |
| `ANTHROPIC_REFUSAL_FALLBACK` | `true` | Enables the server-side refusal fallback (beta). |
| `REFERENCE_DATE` | today | Pins "today" so the 60-day and future-date rules give the same result whenever you demo. |
| `OFFLINE_PROCESSING_DELAY` | `0.9` | Simulated seconds per document in offline mode, so the pipeline is visible. |
| `MAX_UPLOAD_MB` / `MAX_FILES_PER_UPLOAD` | 10 / 30 | Upload limits. |
| `MOCK_FINANCE_FAILURE_RATE` | `0` | Set to e.g. `0.5` to demo the submission-failure path. |

## 7. Demo credentials

None. The app runs as **Demo Employee (EMP001)**. Authentication is out of scope for the challenge; see Production considerations.

## 8. Demo policy

Defined in [`backend/config/policy.json`](backend/config/policy.json). It is re-read on every evaluation, so you can edit it without restarting.

| Category | Limit | Requirements |
|---|---:|---|
| Meals | ₹1,500 / day | Business purpose (inherited from the trip only for meals ≤ ₹1,000 and ≤ 2 covers) |
| Client Entertainment | ₹5,000 / event | Purpose + attendees |
| Hotel | ₹7,500 / night | Invoice, check-in/out dates, purpose |
| Taxi / Cab | ₹3,000 / day | Purpose. Origin and destination recommended. Receipt above ₹500 |
| Air / Train | Actual | Invoice, travel date, purpose |
| Fuel | ₹5,000 / month | – |
| Mobile / Internet | ₹1,500 / month | Bill |
| Office Supplies | ₹5,000 / item | Business reason |
| Parking / Toll | ₹500 / ₹1,000 per day | – |
| Miscellaneous | ₹2,000 / item | Description |
| General | – | Older than 60 days → warning. Future-dated, zero or negative → blocking. Over-limit → cap, or justify for the approver. |

Categorisation signals live in [`backend/config/categories.json`](backend/config/categories.json).

## 9. Demo scenario (about 2 minutes)

The 12 documents in [`demo_receipts/`](demo_receipts) are all fictional. Either drag them onto the drop zone or click **Use the 12 demo receipts**.

| # | File | Demonstrates |
|---|---|---|
| 01 | `airvista_flight_invoice.pdf` | Clean digital invoice, air ticket. Its trip purpose is inherited by the rest of the trip |
| 02 | `orchid_grand_hotel_folio.pdf` | **Multi-page PDF**, hotel invoice, **over policy** (₹8,190/night vs ₹7,500) |
| 03 | `zipcab_airport_ride_rotated.jpg` | **Rotated** taxi receipt |
| 04 | `bean_street_coffee_blurry.jpg` | **Blurry** but readable. Date read at 78% confidence, marked inferred and not asked |
| 05 | `hops_and_barrels_dinner_photo.jpg` | **Phone photo** of a restaurant bill for 4 covers. Purpose deliberately not assumed |
| 06 | `ridenow_trip_receipt.pdf` | Taxi receipt with route |
| 07 | `zipcab_receipt_email_copy.pdf` | **Duplicate** of #03 (same invoice number, amount, date and route, 96% match) |
| 08 | `techpark_parking_slip.jpg` | **Missing information**: the date is smudged |
| 09 | `ridenow_airport_drop_screenshot.png` | App screenshot |
| 10 | `udupi_upahar_lunch.jpg` | Small meal; purpose inherited |
| 11 | `konnect_mobile_bill_aug.pdf` | Recurring bill, grouped separately from the trip |
| 12 | `city_stationers_scan.png` | Scanned retail invoice (office supplies) |

**Script**

1. Click **Use the 12 demo receipts** and watch the pipeline animate. The agent replies:
   *"I've processed all 12 documents. ✓ 8 ready · ? 2 need information · ⚠ 1 exceeds policy · ⊘ 1 duplicate … I grouped them as Bengaluru Customer Visit and found the trip purpose on your booking … I only need 3 answers."*
2. Type: **"It was a client dinner with Priya Shah and Rahul Mehta from Northwind Retail"**. The dinner is recategorised to Client Entertainment, the attendees are recorded, and it falls within the ₹5,000 limit.
3. Tap **Wed 23 Sep** for the parking slip, or type "the 23rd".
4. Tap **Cap at the limit (₹22,500)** for the hotel. Alternatively, type a justification and the full amount stays as a non-blocking warning.
5. Tap **Yes, it's a duplicate**. The claim turns **READY TO SUBMIT**.
6. Optional: click any row to see the original receipt beside the extracted fields, confidences and the agent's reasoning. Edit an amount and watch the totals revalidate.
7. Click **Submit**, review the structured summary, tick the confirmation box and submit. You get claim reference `ER-2026-00xxxx` and the "manual entry avoided" metrics.

To exercise the policy logic differently, answer **"Team meal"** for the dinner. It stays in Meals and is now ₹350 over the daily limit, so you get a new policy question.

To regenerate the dataset, run `backend/.venv/Scripts/python scripts/generate_demo_receipts.py`.

## 10. API overview

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/claims` | Create a claim |
| GET | `/api/claims/{id}` | Full snapshot: claim, dashboard, groups, documents, expenses, open questions, chat, agent events |
| PATCH | `/api/claims/{id}` | Rename the claim or its groups |
| POST | `/api/claims/{id}/documents` | Multipart upload (`files[]`), processed asynchronously |
| POST | `/api/claims/{id}/demo-documents` | Load the bundled demo receipts |
| GET | `/api/claims/{id}/documents` · `/expenses` | Lists |
| GET | `/api/documents/{docId}/file` | Original file (unguessable ID, `nosniff`) |
| POST | `/api/documents/{docId}/replace` | Re-upload a clearer copy |
| DELETE | `/api/documents/{docId}` | Dismiss a failed document |
| PATCH | `/api/expenses/{id}` | Edit fields (`merchant`, `expenseDate`, `amount`, `currency`, `category`, `businessPurpose`, `costCentre`, …) and revalidate |
| DELETE | `/api/expenses/{id}` | Exclude (soft delete, restorable) |
| POST | `/api/expenses/{id}/duplicate-resolution` | `{"resolution": "duplicate" \| "separate"}` |
| POST | `/api/expenses/{id}/policy-override` | `{"action": "cap" \| "justify" \| "clear", "justification": "…"}` |
| POST | `/api/claims/{id}/chat` | `{"message": "…"}` or `{"questionId": "…", "option": "…"}` |
| GET | `/api/claims/{id}/validation` | Blocking issues, warnings, open questions |
| GET | `/api/claims/{id}/summary` | Review summary and payload preview |
| POST | `/api/claims/{id}/submit` | `{"confirm": true}`. Returns `{"status": "SUBMITTED", "claimReference": "ER-2026-001029"}` |
| POST/GET | `/mock-finance/v1/reimbursements[/{ref}]` | Mock corporate reimbursement API |

Interactive docs are at `/docs` (FastAPI OpenAPI).

## 11. Known limitations

* **Offline mode is not OCR.** Without an Anthropic key, the 12 bundled receipts are replayed from recorded extractions, matched by SHA-256 in `demo_receipts/fixtures.json`. Other digital PDFs go through a conservative text-layer parser. Other images are marked "couldn't read automatically" and the agent asks for the core values. With a key, every document is read live by Claude.
* One expense per document. A PDF containing several unrelated receipts is treated as one expense.
* Currency is recorded but not converted. Foreign-currency expenses get a warning for finance.
* A single demo employee with no authentication. The mock finance API is in-process.
* Duplicate detection against history uses a small static mock (`config/history.json`).
* Groups are inferred per claim; there is no calendar or itinerary integration (P2).

## 12. Production considerations

* **Identity and authorisation:** SSO (OIDC/SAML). Scope every claim, document and expense to its owner, with approver and finance roles. Today, document IDs are unguessable but not access-controlled.
* **Storage:** move uploads to private object storage (S3/GCS/Azure Blob) with server-side encryption, short-lived signed URLs and a retention policy. Use Postgres instead of SQLite. Keep an audit log of every edit and decision. A per-expense edit trail already exists.
* **Malware scanning** of uploads, plus stricter content inspection (pdf.js sandboxing, image re-encoding, which is already done for model input).
* **Secrets** in a vault (AWS Secrets Manager, GCP Secret Manager or Key Vault). Never in the repo. `.env` is git-ignored.
* **Data protection:** confirm zero-data-retention and region settings with the model provider, and redact personal data before logging. Logs today contain only stage names and IDs, never receipt text or card numbers.
* **Reliability:** use a real job queue (Celery, RQ or SQS) instead of an in-process thread pool. Add idempotent submission keys, retries with backoff, and dead-letter handling.
* **Model quality:** build an evaluation set of real (anonymised) receipts with per-field accuracy, track confidence calibration, and tune `ANTHROPIC_EFFORT` and thresholds against it.
* **Integration:** replace `mock_finance.py` with the ERP connector (SAP Concur, Oracle or Workday), including approval-status webhooks.
* **Policy management:** add a UI with versioning and effective dates, and stamp each claim with the policy version it was evaluated against.

## 13. Screenshots

| Processed claim (desktop) | Ready to submit (narrow screen) | Submitted |
|---|---|---|
| ![](docs/screenshots/01-processed-claim.jpg) | ![](docs/screenshots/02-ready-to-submit-mobile.jpg) | ![](docs/screenshots/03-submitted.jpg) |

## 14. Why this is AI-native, not OCR + a form

* **The unit of work is the question, not the field.** The app doesn't show 20 fields to fill in. It works out which facts are required by policy, which are already known, which can be inferred, and which genuinely need a human. It then asks for only those, consolidated, with one-tap answers.
* **It reasons across documents.** The purpose on a flight booking explains the taxi, the coffee and the parking slip. The hotel's dates define the trip. Two receipts in different formats are recognised as the same ride.
* **It knows when not to assume.** A ₹1,850 bill for four people might be client entertainment, so the agent asks instead of guessing. The answer changes the category, and with it the policy limit.
* **Confidence is first-class.** Every value carries a source and a confidence. Low-confidence required values are confirmed. The AI never silently invents money, dates or merchants.
* **The employee stays in control.** Every decision is explained, editable and reversible. Duplicates are never auto-deleted. Nothing is submitted without an explicit click.
