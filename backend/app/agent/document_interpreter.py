"""DocumentInterpreter: validates a stored file, identifies the document and extracts fields.

Extraction strategy (EXTRACTION_MODE):
  * Claude (multimodal + structured JSON output) when credentials are available.
  * Offline: replays recorded extractions for the bundled demo receipts (matched by SHA-256),
    then falls back to a text-layer heuristic parser for digital PDFs.
Any AI failure degrades to the offline path and, failing that, to a "needs attention"
result that asks the employee for the core values — it never crashes the claim.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from ..settings import get_settings, load_policy
from .fields import FIELD_DEFS
from . import openai_vision
from .llm import LLMError, structured_call

log = logging.getLogger("expense_agent.interpreter")

DOCUMENT_TYPES = [
    "air_ticket", "train_ticket", "ride_receipt", "taxi_receipt", "hotel_invoice", "restaurant_bill",
    "cafe_receipt", "fuel_receipt", "parking_slip", "toll_receipt", "telecom_bill", "retail_invoice",
    "conference_invoice", "saas_invoice", "metro_ticket", "bus_ticket", "other",
]
EXTRACTABLE_FIELDS = [f for f in FIELD_DEFS if f not in ("costCentre",)]


class DocumentError(Exception):
    """The file itself is unusable (corrupt, empty, encrypted...)."""


@dataclass
class InspectedFile:
    kind: str  # "pdf" | "image"
    media_type: str
    pages: int = 1
    ahash: str | None = None
    text: str = ""
    image_bytes: bytes | None = None  # normalised JPEG/PNG sent to the model


@dataclass
class Extraction:
    raw: dict
    mode: str  # "claude" | "replay" | "pdf-text" | "none"
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ file inspection

def sniff_type(head: bytes) -> str | None:
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return None


def _average_hash(img: Image.Image) -> str:
    small = img.convert("L").resize((16, 16))
    pixels = list(small.tobytes())
    avg = sum(pixels) / len(pixels)
    bits = "".join("1" if p > avg else "0" for p in pixels)
    return f"{int(bits, 2):064x}"


def inspect_file(data: bytes) -> InspectedFile:
    media_type = sniff_type(data[:16])
    if media_type is None:
        raise DocumentError("Unsupported file type — please upload a JPG, PNG or PDF.")
    if media_type == "application/pdf":
        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                raise DocumentError("This PDF is password-protected. Please upload an unlocked copy.")
            pages = len(reader.pages)
            if pages == 0:
                raise DocumentError("This PDF has no pages.")
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:10])
        except DocumentError:
            raise
        except (PdfReadError, ValueError, KeyError, TypeError, OSError) as e:
            raise DocumentError("This PDF appears to be corrupted and couldn't be opened.") from e
        return InspectedFile(kind="pdf", media_type=media_type, pages=pages, text=text)

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # Pillow raises many types for broken images
        raise DocumentError("This image couldn't be read — it may be corrupted.") from e
    img = ImageOps.exif_transpose(img)
    if img.width < 100 or img.height < 100:
        raise DocumentError("This image is too small to read. Please upload a clearer photo.")
    ahash = _average_hash(img)
    img.thumbnail((1800, 1800))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=88)
    return InspectedFile(kind="image", media_type="image/jpeg", ahash=ahash, image_bytes=buf.getvalue())


# ------------------------------------------------------------------ Claude extraction

def _field_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["value", "confidence"],
        "additionalProperties": False,
    }


def category_codes() -> list[str]:
    """Reimbursement categories the model may suggest — taken from policy.json, so a new category is config-only."""
    return list(load_policy()["categories"].keys())


def extraction_schema() -> dict:
    num_text = {"type": "string"}  # plain numbers as text; "" when absent (keeps the schema free of unions)
    return {
        "type": "object",
        "properties": {
            "isExpenseDocument": {"type": "boolean"},
            "documentType": {"type": "string", "enum": DOCUMENT_TYPES},
            "readability": {"type": "string", "enum": ["good", "fair", "poor", "unreadable"]},
            "fields": {
                "type": "object",
                "properties": {name: _field_schema() for name in EXTRACTABLE_FIELDS},
                "required": EXTRACTABLE_FIELDS,
                "additionalProperties": False,
            },
            "lineItems": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "quantity": num_text,
                        "unitPrice": num_text,
                        "amount": num_text,
                    },
                    "required": ["description", "quantity", "unitPrice", "amount"],
                    "additionalProperties": False,
                },
            },
            "categorySuggestion": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": category_codes()},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["category", "confidence", "reason"],
                "additionalProperties": False,
            },
            "multipleTotalsFound": {"type": "boolean"},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "isExpenseDocument", "documentType", "readability", "fields", "lineItems",
            "categorySuggestion", "multipleTotalsFound", "notes",
        ],
        "additionalProperties": False,
    }


EXTRACTION_SYSTEM = """You are the DocumentInterpreter inside a corporate expense-reimbursement agent for an Indian company.
You read one receipt, invoice, ticket or bill (photo, scan or PDF, possibly rotated, blurred or multi-page) and return structured JSON.

Rules:
- Report what the document shows. Never invent or estimate financial values. If the merchant, date, total or currency cannot be read reliably, return an empty string "" for the value and confidence 0.
- confidence is your probability (0-1) that the value is exactly right as written. Use <0.7 when you are guessing between readings, 0.7-0.9 when legible but imperfect (blur, partial occlusion, handwriting), >=0.9 when clearly printed.
- Dates: ISO format YYYY-MM-DD. Money: plain numbers without symbols or thousands separators (e.g. "1850.00"). Currency: ISO 4217 code (₹ / Rs → INR).
- total is the final amount payable including taxes. If you see more than one candidate total (e.g. a pre-tip and post-tip total, or totals that disagree), set multipleTotalsFound=true, pick the final payable amount, lower its confidence and explain in notes.
- cardLast4: only the last four digits of a payment card. Never output a full card number anywhere.
- For travel documents fill origin, destination, travelDate (outbound) and returnDate if present. For hotels fill checkIn, checkOut and nights. For restaurants fill covers (number of guests) if printed.
- Any field that is not on the document: value "" and confidence 0.
- businessPurpose / attendees: only if the document itself states them (e.g. a corporate travel booking with a "trip purpose" field).
- city: the city where the expense was incurred.
- categorySuggestion: the reimbursement category, with a one-sentence reason grounded in what the document shows (e.g. "Issued by a hotel and contains room-night charges")."""


def _claude_extract(insp: InspectedFile, data: bytes, filename: str) -> dict:
    if insp.kind == "pdf":
        block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": base64.standard_b64encode(data).decode()},
        }
    else:
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": insp.media_type, "data": base64.standard_b64encode(insp.image_bytes).decode()},
        }
    content = [
        block,
        {"type": "text", "text": f"Original filename: {filename}\nExtract the reimbursement-relevant data from this document."},
    ]
    return structured_call(EXTRACTION_SYSTEM, content, extraction_schema(), max_tokens=6000)


def _openai_extract(insp: InspectedFile, data: bytes, filename: str) -> dict:
    if insp.kind == "pdf":
        block = {"type": "input_file", "filename": filename,
                 "file_data": "data:application/pdf;base64," + base64.standard_b64encode(data).decode()}
    else:
        block = {"type": "input_image", "detail": "high",
                 "image_url": f"data:{insp.media_type};base64," + base64.standard_b64encode(insp.image_bytes).decode()}
    content = [
        block,
        {"type": "input_text", "text": f"Original filename: {filename}\nExtract the reimbursement-relevant data from this document."},
    ]
    return openai_vision.structured_call(EXTRACTION_SYSTEM, content, extraction_schema())



# ------------------------------------------------------------------ offline extraction

def _load_fixtures() -> dict:
    path = get_settings().demo_dir / "fixtures.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("documents", {})
    except (OSError, json.JSONDecodeError):
        log.warning("demo fixtures could not be loaded")
        return {}


_MONEY = r"(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*\.?[0-9]{0,2})"


def _find(pattern: str, text: str, flags=re.I) -> str | None:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _parse_any_date(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _pdf_text_extract(text: str) -> dict | None:
    """Heuristic parser for digital PDFs with a text layer. Deliberately conservative."""
    if len(text.strip()) < 40:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    totals = re.findall(r"(?:grand\s+total|total\s+amount|amount\s+payable|total\s+payable|net\s+payable|total)\s*[:\-]?\s*" + _MONEY, text, re.I)
    total_vals = sorted({float(t.replace(",", "")) for t in totals if t}, reverse=True)
    date_raw = _find(r"(?:invoice\s+date|bill\s+date|date)\s*[:\-]?\s*([0-9]{1,2}[\-/ ][A-Za-z0-9]{2,9}[\-/ ][0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})", text)
    inv = _find(r"(?:invoice|bill|receipt)\s*(?:no\.?|number|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{3,})", text)
    gstin = _find(r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z])\b", text, 0)
    tax = _find(r"(?:total\s+gst|gst|igst|cgst\s*\+\s*sgst)\s*(?:@\s*[0-9.]+%)?\s*[:\-]?\s*" + _MONEY, text)
    merchant = lines[0] if lines else None

    def fld(v, c):
        return {"value": v, "confidence": c if v is not None else 0.0}

    fields = {name: fld(None, 0) for name in EXTRACTABLE_FIELDS}
    fields["merchant"] = fld(merchant, 0.72)
    fields["invoiceNumber"] = fld(inv, 0.85)
    fields["expenseDate"] = fld(_parse_any_date(date_raw), 0.8)
    fields["gstin"] = fld(gstin, 0.9)
    fields["tax"] = fld(tax.replace(",", "") if tax else None, 0.75)
    if total_vals:
        fields["total"] = fld(f"{total_vals[0]:.2f}", 0.8 if len(total_vals) == 1 else 0.6)
    if "₹" in text or re.search(r"\b(INR|Rs\.?)\b", text) or gstin:
        fields["currency"] = fld("INR", 0.9)
    return {
        "isExpenseDocument": True,
        "documentType": "other",
        "readability": "fair",
        "fields": fields,
        "lineItems": [],
        "categorySuggestion": None,
        "multipleTotalsFound": len(total_vals) > 1,
        "notes": ["Read from the PDF text layer by the offline parser (no AI model available)."],
        "_text": text[:4000],
    }


def _empty_extraction(note: str) -> dict:
    return {
        "isExpenseDocument": True,
        "documentType": "other",
        "readability": "unreadable",
        "fields": {name: {"value": None, "confidence": 0.0} for name in EXTRACTABLE_FIELDS},
        "lineItems": [],
        "categorySuggestion": None,
        "multipleTotalsFound": False,
        "notes": [note],
    }


def extract(data: bytes, sha256: str, filename: str, insp: InspectedFile) -> Extraction:
    settings = get_settings()
    notes: list[str] = []
    # The bundled demo receipts always replay their recorded extraction, even when an AI is enabled,
    # so the public demo stays deterministic and never spends API credits.
    fixtures = _load_fixtures()
    if sha256 in fixtures:
        return Extraction(raw=fixtures[sha256]["extraction"], mode="replay", notes=notes)

    engine = settings.extraction_engine
    if engine:
        for attempt in (1, 2):
            try:
                raw = (_openai_extract if engine == "openai" else _claude_extract)(insp, data, filename)
                return Extraction(raw=raw, mode=engine)
            except LLMError as e:
                log.warning("%s extraction failed (attempt %s): %s", engine, attempt, e)
                if attempt == 2 or "rejected" in str(e) or "declined" in str(e):
                    notes.append(f"AI extraction failed ({e}); used the offline fallback.")
                    break
        if settings.extraction_mode in ("claude", "openai"):
            return Extraction(raw=_empty_extraction(notes[-1] if notes else "AI extraction failed."), mode="none", notes=notes)
    if insp.kind == "pdf":
        parsed = _pdf_text_extract(insp.text)
        if parsed:
            return Extraction(raw=parsed, mode="pdf-text", notes=notes)
        if len(insp.text.strip()) < 40:  # same threshold as _pdf_text_extract
            msg = "This PDF is a scanned image with no text layer; offline mode can't read it. Please enter the key details."
            return Extraction(raw=_empty_extraction(msg), mode="none", notes=notes)
    msg = (
        "I couldn't read this document automatically"
        + (" (offline mode has no OCR for unknown images)" if not engine else "")
        + ". Please enter the key details or upload a clearer copy."
    )
    return Extraction(raw=_empty_extraction(msg), mode="none", notes=notes)
