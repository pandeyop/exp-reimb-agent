"""Structured processing-stage logging.

Events go to the application log as one JSON line each and are also stored per claim so the UI
can show an agent activity trace. Only identifiers, stage names and non-sensitive metadata are
logged — never receipt text, amounts tied to people, or card numbers.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("expense_agent")

STAGES = {
    "document_uploaded",
    "document_parsed",
    "document_failed",
    "expense_extracted",
    "classification_completed",
    "policy_check_completed",
    "duplicate_check_completed",
    "clarification_required",
    "expense_completed",
    "expense_updated",
    "claim_ready",
    "claim_submitted",
    "submission_failed",
    "ai_fallback",
}

_SAFE_KEYS = {
    "documentId", "expenseId", "claimId", "status", "category", "confidence", "mode",
    "pages", "fields", "questions", "score", "reason", "count", "reference", "error", "kind",
}


def log_event(stage: str, claim_id: str | None, message: str = "", **meta) -> dict:
    safe_meta = {k: v for k, v in meta.items() if k in _SAFE_KEYS}
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "stage": stage,
        "claimId": claim_id,
        "message": message,
        **safe_meta,
    }
    logger.info(json.dumps(event, default=str))
    if claim_id:
        # Imported lazily to avoid a cycle (db imports nothing from here, but keep it explicit).
        from . import db

        db.add_event(claim_id, event)
    return event
