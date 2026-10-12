"""ClaimManager: owns claim/document/expense state and runs the agent pipeline.

INGEST → UNDERSTAND → EXTRACT → NORMALISE → CLASSIFY → (revalidate:) INFER → GROUP → CHECK POLICY
→ CHECK DUPLICATES → IDENTIFY GAPS → PLAN QUESTIONS → STATUS. Every mutation (chat answer, edit,
duplicate or policy decision) re-runs `revalidate`, so derived state is never stale.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .. import db
from ..observability import log_event
from ..settings import get_settings, load_policy
from . import classifier, document_interpreter as interp, duplicates, enrich, planner
from .fields import FIELD_DEFS, fval, inr, make_field, short_date
from .normaliser import coerce, normalise
from .policy import evaluate

log = logging.getLogger("expense_agent.claims")

BLOCKING_STATUSES = {"NEEDS_INFORMATION", "POLICY_VIOLATION", "POSSIBLE_DUPLICATE", "PROCESSING"}
_executor: ThreadPoolExecutor | None = None
_tls = threading.local()


@contextmanager
def quiet():
    """Suppress chat announcements from nested revalidations (the caller announces once at the end)."""
    prev = getattr(_tls, "quiet", False)
    _tls.quiet = True
    try:
        yield
    finally:
        _tls.quiet = prev


class ClaimError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=get_settings().worker_threads, thread_name_prefix="docproc")
    return _executor


def _token(prefix: str, nbytes: int = 9) -> str:
    return f"{prefix}-{secrets.token_urlsafe(nbytes).replace('_', 'x').replace('-', 'y')}"


def sanitise_filename(name: str) -> str:
    name = Path(name or "upload").name
    name = re.sub(r"[^A-Za-z0-9._ ()-]", "_", name).strip(" .") or "upload"
    return name[:120]


# ================================================================== claims

def create_claim(title: str | None = None) -> dict:
    s = get_settings()
    n = db.next_counter("claim", start=1028)
    claim = {
        "id": f"CLM-{n}",
        "createdAt": db.now_iso(),
        "employee": {"id": s.employee_id, "name": s.employee_name},
        "title": title or "New expense claim",
        "titleSource": "user" if title else "auto",
        "groupNames": {},
        "groupOverrides": {},
        "askedQuestionIds": [],
        "answeredQuestionIds": [],
        "batches": {},
        "lastStatus": "DRAFT",
        "status": "DRAFT",
        "submission": None,
    }
    db.save_claim(claim)
    db.add_message(claim["id"], {
        "role": "assistant", "kind": "text",
        "text": ("Hi! Drop your receipts, bills, tickets and invoices here — photos, scans or PDFs, as many as you like. "
                 "I'll read them, categorise them, check them against policy, spot duplicates, and then ask only for what I couldn't work out."),
    })
    return claim


def get_claim_or_404(claim_id: str) -> dict:
    claim = db.get_claim(claim_id)
    if not claim:
        raise ClaimError("Claim not found", 404)
    return claim


def _assert_editable(claim: dict) -> None:
    if claim.get("status") == "SUBMITTED":
        raise ClaimError("This claim has already been submitted and can no longer be changed.", 409)


# ================================================================== documents

def add_documents(claim_id: str, files: list[tuple[str, bytes]]) -> list[dict]:
    s = get_settings()
    claim = get_claim_or_404(claim_id)
    _assert_editable(claim)
    if not files:
        raise ClaimError("No files were uploaded.")
    if len(files) > s.max_files_per_upload:
        raise ClaimError(f"Please upload at most {s.max_files_per_upload} files at a time.")

    batch_id = _token("BATCH", 6)
    accepted, rejected = [], []
    with db.lock():
        seq0 = db.count_documents(claim_id)
        for i, (filename, data) in enumerate(files):
            safe = sanitise_filename(filename)
            media = interp.sniff_type(data[:16])
            if len(data) == 0:
                rejected.append((safe, "the file is empty"))
                continue
            if len(data) > s.max_upload_bytes:
                rejected.append((safe, f"it is larger than {s.max_upload_bytes // (1024 * 1024)} MB"))
                continue
            if media is None:
                rejected.append((safe, "only JPG, PNG and PDF files are supported"))
                continue
            doc_id = _token("DOC")
            ext = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}[media]
            stored = f"{doc_id}{ext}"
            (s.upload_dir / stored).write_bytes(data)
            doc = {
                "id": doc_id, "claimId": claim_id, "seq": seq0 + i, "batchId": batch_id,
                "filename": safe, "storedName": stored, "mediaType": media, "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "uploadedAt": db.now_iso(),
                "status": "UPLOADED", "error": None, "pages": None, "expenseId": None,
            }
            db.save_document(doc)
            accepted.append(doc)
            log_event("document_uploaded", claim_id, "Document uploaded", documentId=doc_id)
        claim = db.get_claim(claim_id)
        claim["batches"][batch_id] = {"count": len(accepted), "announced": False, "rejected": rejected}
        claim["status"] = "PROCESSING" if accepted else claim["status"]
        db.save_claim(claim)

    if rejected:
        db.add_message(claim_id, {
            "role": "assistant", "kind": "text",
            "text": "I couldn't accept " + "; ".join(f"**{n}** ({why})" for n, why in rejected) + ".",
        })
    if accepted:
        db.add_message(claim_id, {
            "role": "assistant", "kind": "progress", "batchId": batch_id,
            "text": f"Got {len(accepted)} document{'s' if len(accepted) != 1 else ''}. Reading them now…",
        })
    for doc in accepted:
        executor().submit(_safe_process, doc["id"])
    return accepted


def replace_document(doc_id: str, filename: str, data: bytes) -> dict:
    """Re-upload a clearer copy: the old expense is dropped and the document is reprocessed."""
    s = get_settings()
    doc = db.get_document(doc_id)
    if not doc:
        raise ClaimError("Document not found", 404)
    claim = get_claim_or_404(doc["claimId"])
    _assert_editable(claim)
    media = interp.sniff_type(data[:16])
    if media is None or not data:
        raise ClaimError("Only JPG, PNG and PDF files are supported.")
    if len(data) > s.max_upload_bytes:
        raise ClaimError("That file is too large.")
    with db.lock():
        if doc.get("expenseId"):
            db.delete_expense(doc["expenseId"])
        old = s.upload_dir / doc["storedName"]
        if old.exists():
            old.unlink()
        ext = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}[media]
        batch_id = _token("BATCH", 6)
        doc.update({
            "filename": sanitise_filename(filename), "storedName": f"{doc_id}{ext}", "mediaType": media,
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "uploadedAt": db.now_iso(),
            "status": "UPLOADED", "error": None, "expenseId": None, "batchId": batch_id, "pages": None,
        })
        (s.upload_dir / doc["storedName"]).write_bytes(data)
        db.save_document(doc)
        claim = db.get_claim(doc["claimId"])
        claim["batches"][batch_id] = {"count": 1, "announced": False, "rejected": []}
        db.save_claim(claim)
    log_event("document_uploaded", doc["claimId"], "Replacement uploaded", documentId=doc_id)
    executor().submit(_safe_process, doc_id)
    return doc


def dismiss_document(doc_id: str) -> None:
    doc = db.get_document(doc_id)
    if not doc:
        raise ClaimError("Document not found", 404)
    claim = get_claim_or_404(doc["claimId"])
    _assert_editable(claim)
    with db.lock():
        doc["status"] = "DISMISSED"
        db.save_document(doc)
        if doc.get("expenseId"):
            exp = db.get_expense(doc["expenseId"])
            if exp:
                exp["excluded"], exp["excludedReason"] = True, "Document removed by employee"
                db.save_expense(exp)
    revalidate(doc["claimId"])


def _safe_process(doc_id: str) -> None:
    try:
        process_document(doc_id)
    except Exception:  # never let one document take down the worker or the claim
        log.exception("unexpected failure processing document")
        doc = db.get_document(doc_id)
        if doc:
            doc["status"], doc["error"] = "FAILED", "Something went wrong while processing this document. Please try re-uploading it."
            db.save_document(doc)
            log_event("document_failed", doc["claimId"], "Unexpected processing error", documentId=doc_id)
            _after_document(doc)


def process_document(doc_id: str) -> None:
    s = get_settings()
    doc = db.get_document(doc_id)
    if not doc or doc["status"] not in ("UPLOADED", "PROCESSING"):
        return
    claim_id = doc["claimId"]
    doc["status"] = "PROCESSING"
    db.save_document(doc)

    data = (s.upload_dir / doc["storedName"]).read_bytes()
    try:
        insp = interp.inspect_file(data)
    except interp.DocumentError as e:
        doc["status"], doc["error"] = "FAILED", str(e)
        db.save_document(doc)
        log_event("document_failed", claim_id, "Document could not be opened", documentId=doc_id, error=type(e).__name__)
        _after_document(doc)
        return
    doc["pages"] = insp.pages
    log_event("document_parsed", claim_id, f"Parsed {insp.kind}", documentId=doc_id, pages=insp.pages)

    started = time.monotonic()
    extraction = interp.extract(data, doc["sha256"], doc["filename"], insp)
    if extraction.mode != "claude" and s.offline_delay_seconds:
        # Keeps the offline demo's pipeline visible; roughly what a model call costs.
        time.sleep(max(0.0, s.offline_delay_seconds * (0.7 + 0.6 * secrets.randbelow(100) / 100) - (time.monotonic() - started)))
    if extraction.notes and extraction.mode != "claude":
        log_event("ai_fallback", claim_id, "Used offline extraction", documentId=doc_id, mode=extraction.mode)

    norm = normalise(extraction.raw)
    if not norm["isExpenseDocument"]:
        doc["status"], doc["error"] = "FAILED", "This doesn't look like a receipt, bill or invoice."
        db.save_document(doc)
        log_event("document_failed", claim_id, "Not an expense document", documentId=doc_id)
        _after_document(doc)
        return

    expense = {
        "id": _token("EXP", 5), "claimId": claim_id, "documentId": doc_id, "seq": doc["seq"],
        "createdAt": db.now_iso(), "fields": norm["fields"], "lineItems": norm["lineItems"],
        "documentType": norm["documentType"], "readability": norm["readability"],
        "extractionMode": extraction.mode, "extractionNotes": extraction.notes + norm["notes"],
        "categorySuggestion": norm["categorySuggestion"], "fileSha256": doc["sha256"],
        "imageHash": insp.ahash, "_text": norm["text"], "excluded": False, "excludedReason": None,
        "duplicateResolutions": {}, "policyDecision": None, "edits": [],
    }
    n_fields = sum(1 for f in expense["fields"].values() if f["value"] is not None)
    log_event("expense_extracted", claim_id, f"Extracted {n_fields} fields", documentId=doc_id,
              expenseId=expense["id"], fields=n_fields, mode=extraction.mode)

    cls = classifier.classify(expense)
    expense["category"] = {"value": cls["category"], "confidence": cls["confidence"], "source": "classified", "reason": cls["reason"]}
    scores = classifier.score_categories(expense)
    expense["_categoryRanking"] = [c for c, _ in sorted(scores.items(), key=lambda kv: -kv[1])]
    log_event("classification_completed", claim_id, cls["reason"], expenseId=expense["id"],
              category=cls["category"], confidence=cls["confidence"])

    with db.lock():
        current = db.get_document(doc_id)
        if not current or current.get("sha256") != doc["sha256"]:
            return  # replaced while we were working
        db.save_expense(expense)
        doc["status"], doc["expenseId"] = "PROCESSED", expense["id"]
        db.save_document(doc)
        revalidate(claim_id, announce=False)
        exp = db.get_expense(expense["id"])
        log_event("policy_check_completed", claim_id,
                  f"{len(exp.get('policyResults', []))} policy finding(s)", expenseId=exp["id"], status=exp["status"])
        log_event("duplicate_check_completed", claim_id,
                  "Possible duplicate" if exp.get("duplicate") else "No duplicate found", expenseId=exp["id"],
                  score=(exp.get("duplicate") or {}).get("score"))
        if exp["status"] == "NEEDS_INFORMATION":
            log_event("clarification_required", claim_id, "Missing: " + ", ".join(g["field"] for g in exp["gaps"]),
                      expenseId=exp["id"])
    _after_document(doc)


def _after_document(doc: dict) -> None:
    """When the last document of an upload batch finishes, post the agent's summary + questions."""
    claim_id = doc["claimId"]
    with db.lock():
        claim = db.get_claim(claim_id)
        batch = claim["batches"].get(doc["batchId"])
        docs = db.list_documents(claim_id)
        pending = [d for d in docs if d["status"] in ("UPLOADED", "PROCESSING")]
        batch_docs = [d for d in docs if d.get("batchId") == doc["batchId"]]
        if batch is None or batch["announced"] or any(d["status"] in ("UPLOADED", "PROCESSING") for d in batch_docs):
            if not pending:
                revalidate(claim_id)
            return
        batch["announced"] = True
        db.save_claim(claim)
        snap = revalidate(claim_id, announce=False)
        if not pending:
            _post_batch_summary(claim_id, batch_docs, snap)
            revalidate(claim_id, announce=True)


def _post_batch_summary(claim_id: str, batch_docs: list[dict], snap: dict) -> None:
    exps = [e for e in snap["expenses"] if not e["excluded"]]
    c = Counter(e["status"] for e in exps)
    failed = [d for d in batch_docs if d["status"] == "FAILED"]
    open_q = [q for q in snap["questions"] if q["kind"] not in planner.DECISION_KINDS]
    n = len(batch_docs)
    lines = [f"I've processed {'all ' if n > 1 else ''}{n} document{'s' if n != 1 else ''}."]
    ready = c["COMPLETE"] + c["WARNING"]
    lines.append(f"✓ {ready} expense{'s are' if ready != 1 else ' is'} ready")
    if c["NEEDS_INFORMATION"]:
        lines.append(f"? {c['NEEDS_INFORMATION']} need{'s' if c['NEEDS_INFORMATION'] == 1 else ''} information")
    if c["POLICY_VIOLATION"]:
        lines.append(f"⚠ {c['POLICY_VIOLATION']} exceed{'s' if c['POLICY_VIOLATION'] == 1 else ''} policy")
    if c["POSSIBLE_DUPLICATE"]:
        lines.append(f"⊘ {c['POSSIBLE_DUPLICATE']} appear{'s' if c['POSSIBLE_DUPLICATE'] == 1 else ''} to be a duplicate")
    if failed:
        lines.append(f"✕ {len(failed)} couldn't be read: " + ", ".join(d["filename"] for d in failed))
    inferred = sum(1 for e in exps if e["fields"]["businessPurpose"].get("derived"))
    trip_groups = [g for g in snap["claim"]["groups"] if g["kind"] == "trip"]
    extras = []
    if trip_groups:
        g = trip_groups[0]
        extras.append(f"I grouped them as **{g['title']}** ({g['period']})" +
                      (f" and found the trip purpose on your booking, so I filled it in for {inferred} related expenses." if inferred else "."))
    if open_q:
        extras.append(f"I only need {len(open_q)} answer{'s' if len(open_q) != 1 else ''} from you before this claim can be completed.")
    elif c["POSSIBLE_DUPLICATE"]:
        extras.append("Just confirm the duplicate below and the claim is complete.")
    elif snap["claim"]["status"] == "READY_TO_SUBMIT":
        extras.append("Nothing else is needed — the claim is ready to submit.")
    db.add_message(claim_id, {"role": "assistant", "kind": "summary", "text": "\n".join(lines) + "\n\n" + " ".join(extras)})


# ================================================================== revalidation

def revalidate(claim_id: str, announce: bool = True) -> dict:
    s = get_settings()
    policy = load_policy()
    announce = announce and not getattr(_tls, "quiet", False)
    with db.lock():
        claim = db.get_claim(claim_id)
        docs = db.list_documents(claim_id)
        expenses = db.list_expenses(claim_id)
        active = [e for e in expenses if not e.get("excluded")]
        by_id = {e["id"]: e for e in expenses}

        for e in expenses:
            for k in ("_inferenceBlocked",):
                e.pop(k, None)
            enrich.clear_derived(e)
            enrich.derive_fields(e)

        groups = enrich.build_groups(active, claim)
        enrich.infer_purposes(groups, by_id, policy)

        policy_results = evaluate(active, policy, s.today())
        dups = duplicates.detect(active, policy.get("duplicateThreshold", 0.75))

        for e in expenses:
            e["policyResults"] = policy_results.get(e["id"], []) if not e.get("excluded") else []
            e["duplicate"] = dups.get(e["id"])
            gaps, states = planner.detect_gaps(e, policy)
            e["_gaps"], e["gaps"], e["fieldStates"] = gaps, gaps, states
            if e.get("excluded"):
                status = "EXCLUDED"
            elif e["duplicate"]:
                status = "POSSIBLE_DUPLICATE"
            elif gaps:
                status = "NEEDS_INFORMATION"
            elif any(r["blocking"] for r in e["policyResults"]):
                status = "POLICY_VIOLATION"
            elif any(r["severity"] == "warning" for r in e["policyResults"]):
                status = "WARNING"
            else:
                status = "COMPLETE"
            if status == "COMPLETE" and e.get("status") not in (None, "COMPLETE") and announce:
                log_event("expense_completed", claim_id, "All checks passed", expenseId=e["id"])
            e["status"] = status
            e["explanations"] = _explanations(e)

        questions = planner.plan(active, groups, policy)
        for e in expenses:
            e.pop("_gaps", None)
            db.save_expense(e)

        # group titles + claim title
        for g in groups:
            g["autoTitle"] = enrich.group_title(g, g.get("purpose"))
            g["title"] = claim["groupNames"].get(g["id"], g["autoTitle"])
            g["total"] = round(sum(by_id[i].get("reimbursableAmount") or 0 for i in g["expenseIds"]), 2)
            g["period"] = (f"{short_date(g['start'])}–{short_date(g['end'])}" if g["kind"] == "trip"
                           else g.get("title") if g["kind"] != "month" else short_date(g["start"])[-3:] + " " + g["start"][:4])
        groups.sort(key=lambda g: (g["kind"] != "trip", -g["total"]))
        claim["groups"] = [{k: v for k, v in g.items() if k != "anchorIds"} for g in groups]
        if claim.get("titleSource") != "user" and groups:
            claim["title"] = groups[0]["title"]

        processing = any(d["status"] in ("UPLOADED", "PROCESSING") for d in docs)
        if claim.get("submission") and claim["submission"].get("status") == "SUBMITTED":
            status = "SUBMITTED"
        elif not docs:
            status = "DRAFT"
        elif processing:
            status = "PROCESSING"
        elif not active or any(e["status"] in BLOCKING_STATUSES for e in active):
            status = "ACTION_REQUIRED"
        else:
            status = "READY_TO_SUBMIT"
        claim["status"] = status
        claim["openQuestionIds"] = [q["id"] for q in questions]
        claim["openQuestions"] = questions

        newly_ready = status == "READY_TO_SUBMIT" and claim.get("lastStatus") != "READY_TO_SUBMIT"
        if status != "READY_TO_SUBMIT" or announce:
            claim["lastStatus"] = status  # a quiet pass must not swallow the "ready" announcement
        if announce and not processing:
            _post_new_questions(claim, questions)
        db.save_claim(claim)

        if newly_ready and announce:
            total = sum(e.get("reimbursableAmount") or 0 for e in active)
            log_event("claim_ready", claim_id, "All blocking issues resolved", count=len(active))
            db.add_message(claim_id, {
                "role": "assistant", "kind": "ready",
                "text": (f"✓ All blocking issues are resolved. **{claim['title']}** — {len(active)} expenses, {inr(total)} — "
                         f"is now **READY TO SUBMIT**. Review the summary and press Submit when you're happy; I won't submit without you."),
            })
        return snapshot(claim_id, questions=questions)


def _post_new_questions(claim: dict, questions: list[dict]) -> None:
    asked = set(claim.get("askedQuestionIds", []))
    for q in questions:
        if q["id"] in asked:
            continue
        db.add_message(claim["id"], {"role": "assistant", "kind": "question", "questionId": q["id"],
                                     "text": q["text"], "options": q["options"], "expenseIds": q["expenseIds"],
                                     "questionKind": q["kind"], "placeholder": q.get("placeholder", "")})
        claim.setdefault("askedQuestionIds", []).append(q["id"])
        if q["kind"] not in planner.DECISION_KINDS:
            log_event("clarification_required", claim["id"], q["kind"], kind=q["kind"])


def _explanations(e: dict) -> list[dict]:
    out = []
    if e["category"].get("reason"):
        out.append({"type": "classification", "text": e["category"]["reason"]})
    for name, f in e["fields"].items():
        if f.get("derived") and f.get("reason"):
            out.append({"type": "inference", "text": f"{FIELD_DEFS[name][0]}: {f['reason']}"})
    for t in e.get("_inferenceBlocked", []) or []:
        out.append({"type": "inference", "text": t})
    for g in e.get("gaps", []):
        name = "Category" if g["field"] == "category" else FIELD_DEFS[g["field"]][0]
        out.append({"type": "missing", "text": (f"{name} is required for this category but could not be found on the receipt."
                                                 if g["state"] == "MISSING" else f"{name} was read with low confidence and needs confirmation.")})
    if e.get("duplicate"):
        out.append({"type": "duplicate", "text": e["duplicate"]["explanation"]})
    for r in e.get("policyResults", []):
        if r["severity"] != "info" or r["code"].startswith("OVER_LIMIT"):
            out.append({"type": "policy", "text": f"{r['title']}: {r['message']}"})
    return out


# ================================================================== employee actions

def _load_expense(expense_id: str) -> tuple[dict, dict]:
    exp = db.get_expense(expense_id)
    if not exp:
        raise ClaimError("Expense not found", 404)
    claim = get_claim_or_404(exp["claimId"])
    _assert_editable(claim)
    return exp, claim


def update_fields(expense_id: str, changes: dict, via: str = "edit", revalidate_after: bool = True) -> dict:
    """Apply employee-provided values. Accepts field names plus 'category' and 'amount' (alias of total)."""
    with db.lock():
        exp, claim = _load_expense(expense_id)
        policy = load_policy()
        applied = {}
        for key, raw in changes.items():
            key = "total" if key == "amount" else key
            if key == "category":
                if raw not in policy["categories"]:
                    raise ClaimError(f"Unknown category: {raw}")
                old = exp["category"]["value"]
                exp["category"] = {"value": raw, "confidence": 1.0, "source": "user",
                                   "reason": f"Set by you ({policy['categories'][raw]['label']})."}
                if old != raw and exp.get("policyDecision"):
                    exp["policyDecision"] = None
                applied["category"] = raw
                continue
            if key == "excluded":
                exp["excluded"] = bool(raw)
                exp["excludedReason"] = "Removed by employee" if raw else None
                applied["excluded"] = bool(raw)
                continue
            if key == "groupId":
                claim = db.get_claim(exp["claimId"])
                claim.setdefault("groupOverrides", {})[expense_id] = raw or None
                if not raw:
                    claim["groupOverrides"].pop(expense_id, None)
                db.save_claim(claim)
                applied["groupId"] = raw
                continue
            if key not in FIELD_DEFS:
                raise ClaimError(f"Unknown field: {key}")
            value = coerce(key, raw)
            if raw not in (None, "") and value is None:
                raise ClaimError(f"“{raw}” isn't a valid {FIELD_DEFS[key][0].lower()}.")
            if key == "total" and value is not None and value != fval(exp, "total") and exp.get("policyDecision", {}) and exp["policyDecision"].get("type") == "cap":
                exp["policyDecision"] = None
            old = fval(exp, key)
            exp["fields"][key] = make_field(value, 1.0, "user", f"Provided by you via {via}.") if value is not None else make_field()
            exp["edits"].append({"field": key, "from": old, "to": value, "via": via, "at": db.now_iso()})
            applied[key] = value
        db.save_expense(exp)
        log_event("expense_updated", exp["claimId"], f"Updated via {via}", expenseId=expense_id,
                  fields=",".join(applied.keys()))
        if revalidate_after:
            revalidate(exp["claimId"])
        return applied


def confirm_field(expense_id: str, field: str) -> None:
    with db.lock():
        exp, _ = _load_expense(expense_id)
        f = exp["fields"][field]
        if f["value"] is not None:
            f.update({"confidence": 1.0, "source": "user", "reason": "Confirmed by you.", "derived": False})
            db.save_expense(exp)
        revalidate(exp["claimId"])


def resolve_duplicate(expense_id: str, resolution: str) -> dict:
    if resolution not in ("duplicate", "separate"):
        raise ClaimError("resolution must be 'duplicate' or 'separate'")
    with db.lock():
        exp, _ = _load_expense(expense_id)
        dup = exp.get("duplicate")
        if not dup:
            raise ClaimError("This expense isn't flagged as a possible duplicate.")
        if resolution == "duplicate":
            exp["excluded"] = True
            exp["excludedReason"] = "Confirmed duplicate" + (f" of {dup['ofExpenseId']}" if dup.get("ofExpenseId") else f" of {dup.get('historyReference')}")
        else:
            key = dup.get("ofExpenseId") or dup.get("historyKey")
            exp["duplicateResolutions"][key] = "separate"
        db.save_expense(exp)
        log_event("expense_updated", exp["claimId"], f"Duplicate resolved: {resolution}", expenseId=expense_id)
        revalidate(exp["claimId"])
        return {"resolution": resolution}


def policy_decision(expense_id: str, action: str, justification: str | None = None) -> dict:
    if action not in ("cap", "justify", "clear"):
        raise ClaimError("action must be cap, justify or clear")
    policy = load_policy()
    with db.lock():
        exp, _ = _load_expense(expense_id)
        if action == "cap" and not policy["overLimit"].get("allowCap", True):
            raise ClaimError("Policy does not allow capping for this expense.")
        if action == "justify" and not policy["overLimit"].get("allowJustification", True):
            raise ClaimError("Policy does not allow over-limit justification.")
        exp["policyDecision"] = None if action == "clear" else {
            "type": action, "justification": (justification or "").strip()[:500] or None, "at": db.now_iso()}
        db.save_expense(exp)
        log_event("expense_updated", exp["claimId"], f"Policy decision: {action}", expenseId=expense_id)
        revalidate(exp["claimId"])
        return exp["policyDecision"] or {}


def set_excluded(expense_id: str, excluded: bool, reason: str | None = None) -> None:
    with db.lock():
        exp, _ = _load_expense(expense_id)
        exp["excluded"] = excluded
        exp["excludedReason"] = (reason or "Removed by employee") if excluded else None
        db.save_expense(exp)
        log_event("expense_updated", exp["claimId"], "Excluded" if excluded else "Restored", expenseId=expense_id)
        revalidate(exp["claimId"])


def update_claim(claim_id: str, title: str | None = None, group_names: dict | None = None) -> dict:
    with db.lock():
        claim = get_claim_or_404(claim_id)
        _assert_editable(claim)
        if title is not None:
            claim["title"], claim["titleSource"] = (title.strip()[:120], "user") if title.strip() else (claim["title"], "auto")
        if group_names:
            for gid, name in group_names.items():
                if name and name.strip():
                    claim["groupNames"][gid] = name.strip()[:80]
                else:
                    claim["groupNames"].pop(gid, None)
        db.save_claim(claim)
    return revalidate(claim_id)


# ================================================================== views

PUBLIC_EXPENSE_KEYS = {
    "id", "claimId", "documentId", "seq", "createdAt", "fields", "lineItems", "documentType", "readability",
    "extractionMode", "extractionNotes", "category", "excluded", "excludedReason", "duplicateResolutions",
    "policyDecision", "edits", "policyResults", "duplicate", "gaps", "fieldStates", "status",
    "reimbursableAmount", "groupId", "explanations",
}


def public_expense(e: dict) -> dict:
    return {k: v for k, v in e.items() if k in PUBLIC_EXPENSE_KEYS}


def public_document(d: dict, expense: dict | None) -> dict:
    status = d["status"]
    if status == "PROCESSED" and expense and expense.get("status") in ("NEEDS_INFORMATION", "POLICY_VIOLATION", "POSSIBLE_DUPLICATE"):
        status = "NEEDS_ATTENTION"
    return {
        "id": d["id"], "filename": d["filename"], "mediaType": d["mediaType"], "size": d["size"],
        "uploadedAt": d["uploadedAt"], "status": status, "error": d.get("error"), "pages": d.get("pages"),
        "expenseId": d.get("expenseId"), "url": f"/api/documents/{d['id']}/file",
    }


def compute_metrics(claim: dict, expenses: list[dict]) -> dict:
    active = [e for e in expenses if not e.get("excluded")]
    extracted = inferred = manual = 0
    for e in active:
        for f in e["fields"].values():
            if f["value"] is None:
                continue
            if f["source"] == "user":
                manual += 1
            elif f["source"] == "inferred":
                inferred += 1
            elif f["source"] == "extracted":
                extracted += 1
        if e["category"]["source"] == "user":
            manual += 1
        else:
            inferred += 1
    decisions = sum(1 for e in expenses if e.get("policyDecision")) + sum(
        1 for e in expenses if e.get("duplicateResolutions") or (e.get("excludedReason") or "").startswith("Confirmed duplicate"))
    questions_asked = sum(1 for q in claim.get("askedQuestionIds", []) if not q.startswith("duplicate:"))
    automated = extracted + inferred
    total = automated + manual
    return {
        "fieldsExtracted": extracted, "fieldsInferred": inferred, "manualFieldsEntered": manual,
        "questionsAsked": questions_asked, "decisionsMade": decisions,
        "manualEntryAvoidedPct": round(100 * automated / total) if total else 0,
    }


def snapshot(claim_id: str, questions: list[dict] | None = None) -> dict:
    s = get_settings()
    policy = load_policy()
    claim = get_claim_or_404(claim_id)
    docs = db.list_documents(claim_id)
    expenses = db.list_expenses(claim_id)
    by_id = {e["id"]: e for e in expenses}
    active = [e for e in expenses if not e.get("excluded")]
    if questions is None:
        questions = claim.get("openQuestions", [])

    status_counts = Counter(e["status"] for e in active)
    doc_status = Counter(public_document(d, by_id.get(d.get("expenseId")))["status"] for d in docs if d["status"] != "DISMISSED")
    cat_totals: dict[str, dict] = defaultdict(lambda: {"amount": 0.0, "count": 0})
    for e in active:
        c = e["category"]["value"]
        cat_totals[c]["amount"] += e.get("reimbursableAmount") or 0
        cat_totals[c]["count"] += 1
    total_claimed = round(sum(fval(e, "total") or 0 for e in active), 2)
    total_reimbursable = round(sum(e.get("reimbursableAmount") or 0 for e in active), 2)

    blocking, warnings = [], []
    for e in active:
        who = f"{fval(e, 'merchant') or 'Expense'} {inr(fval(e, 'total'))}"
        if e["status"] == "POSSIBLE_DUPLICATE":
            blocking.append({"expenseId": e["id"], "text": f"{who}: possible duplicate"})
        for g in e.get("gaps", []):
            name = "category" if g["field"] == "category" else FIELD_DEFS[g["field"]][0].lower()
            blocking.append({"expenseId": e["id"], "text": f"{who}: {name} {'missing' if g['state'] == 'MISSING' else 'needs confirmation'}"})
        for r in e.get("policyResults", []):
            if r["blocking"]:
                blocking.append({"expenseId": e["id"], "text": f"{who}: {r['title']}"})
            elif r["severity"] == "warning":
                warnings.append({"expenseId": e["id"], "text": f"{who}: {r['title']}"})

    dates = sorted(fval(e, "expenseDate") for e in active if fval(e, "expenseDate"))
    public_docs = [public_document(d, by_id.get(d.get("expenseId"))) for d in docs if d["status"] != "DISMISSED"]
    return {
        "claim": {
            "id": claim["id"], "title": claim["title"], "titleSource": claim.get("titleSource"),
            "employee": claim["employee"], "status": claim["status"], "createdAt": claim["createdAt"],
            "currency": policy.get("currency", "INR"),
            "totalClaimed": total_claimed, "totalAmount": total_reimbursable,
            "period": {"from": dates[0], "to": dates[-1]} if dates else None,
            "dashboard": {
                "receipts": len(public_docs),
                "processed": sum(v for k, v in doc_status.items() if k in ("PROCESSED", "NEEDS_ATTENTION")),
                "processing": doc_status["PROCESSING"] + doc_status["UPLOADED"],
                "failed": doc_status["FAILED"],
                "expenses": len(active),
                "complete": status_counts["COMPLETE"] + status_counts["WARNING"],
                "needsInformation": status_counts["NEEDS_INFORMATION"],
                "policyViolations": status_counts["POLICY_VIOLATION"],
                "warnings": len(warnings),
                "duplicates": status_counts["POSSIBLE_DUPLICATE"],
                "requiringAttention": status_counts["NEEDS_INFORMATION"] + status_counts["POLICY_VIOLATION"] + status_counts["POSSIBLE_DUPLICATE"] + doc_status["FAILED"],
                "excluded": sum(1 for e in expenses if e.get("excluded")),
            },
            "categoryTotals": sorted(
                [{"category": c, "label": policy["categories"].get(c, {}).get("label", c), "amount": round(v["amount"], 2), "count": v["count"]}
                 for c, v in cat_totals.items()], key=lambda x: -x["amount"]),
            "groups": claim.get("groups", []),
            "blockingIssues": blocking,
            "warnings": warnings,
            "metrics": compute_metrics(claim, expenses),
            "submission": claim.get("submission"),
        },
        "documents": public_docs,
        "expenses": [public_expense(e) for e in expenses],
        "questions": questions,
        "ai": {"mode": s.extraction_engine or "offline", "model": s.extraction_model,
               "chat": "claude" if s.ai_enabled else "rules"},
        "policy": {"name": policy["name"], "categories": {k: {"label": v["label"], "limit": v.get("limit")} for k, v in policy["categories"].items()}},
    }
