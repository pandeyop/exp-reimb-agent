"""FastAPI application: REST API, mock finance API and the built frontend."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, mock_finance
from .agent import chat_agent, claim_manager as cm, submission
from .settings import get_settings, load_policy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
settings = get_settings()
app = FastAPI(title="Expense & Reimbursement Agent", version="1.0.0")
app.include_router(mock_finance.router)


@app.exception_handler(cm.ClaimError)
async def claim_error_handler(_: Request, exc: cm.ClaimError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


async def _read_uploads(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    out = []
    for f in files:
        data = await f.read(settings.max_upload_bytes + 1)
        out.append((f.filename or "upload", data))
    return out


# ------------------------------------------------------------------ meta

@app.get("/api/health")
def health():
    return {"ok": True, "ai": {"mode": settings.extraction_engine or "offline", "model": settings.extraction_model,
                               "chat": "claude" if settings.ai_enabled else "rules"},
            "referenceDate": settings.today().isoformat()}


@app.get("/api/policy")
def policy():
    return load_policy()


# ------------------------------------------------------------------ claims

class CreateClaim(BaseModel):
    title: str | None = Field(default=None, max_length=120)


@app.post("/api/claims", status_code=201)
def create_claim(body: CreateClaim | None = None):
    claim = cm.create_claim(body.title if body else None)
    return cm.snapshot(claim["id"])


@app.get("/api/claims")
def list_claims():
    return [{"id": c["id"], "title": c["title"], "status": c["status"], "createdAt": c["createdAt"]} for c in db.list_claims()]


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: str):
    snap = cm.snapshot(claim_id)
    snap["messages"] = db.list_messages(claim_id)
    snap["events"] = db.list_events(claim_id, limit=80)
    return snap


class UpdateClaim(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    groupNames: dict[str, str] | None = None


@app.patch("/api/claims/{claim_id}")
def update_claim(claim_id: str, body: UpdateClaim):
    return cm.update_claim(claim_id, title=body.title, group_names=body.groupNames)


@app.get("/api/claims/{claim_id}/validation")
def validation(claim_id: str):
    snap = cm.revalidate(claim_id, announce=False)
    c = snap["claim"]
    return {"status": c["status"], "readyToSubmit": c["status"] == "READY_TO_SUBMIT",
            "blockingIssues": c["blockingIssues"], "warnings": c["warnings"], "openQuestions": snap["questions"]}


@app.get("/api/claims/{claim_id}/summary")
def summary(claim_id: str):
    snap = cm.snapshot(claim_id)
    return {"claim": snap["claim"], "payloadPreview": submission.build_payload(snap), "documents": len(snap["documents"])}


class SubmitBody(BaseModel):
    confirm: bool = False


@app.post("/api/claims/{claim_id}/submit")
def submit(claim_id: str, body: SubmitBody):
    return submission.submit(claim_id, body.confirm)


# ------------------------------------------------------------------ documents

@app.post("/api/claims/{claim_id}/documents", status_code=202)
async def upload_documents(claim_id: str, files: list[UploadFile] = File(...)):
    data = await _read_uploads(files)
    docs = cm.add_documents(claim_id, data)
    return {"accepted": [cm.public_document(d, None) for d in docs]}


@app.post("/api/claims/{claim_id}/demo-documents", status_code=202)
def load_demo_documents(claim_id: str):
    demo_dir = settings.demo_dir
    paths = sorted(p for p in demo_dir.iterdir() if p.suffix.lower() in (".pdf", ".png", ".jpg", ".jpeg"))
    if not paths:
        raise HTTPException(404, "No demo receipts found")
    docs = cm.add_documents(claim_id, [(p.name, p.read_bytes()) for p in paths])
    return {"accepted": [cm.public_document(d, None) for d in docs]}


@app.get("/api/claims/{claim_id}/documents")
def list_documents(claim_id: str):
    return cm.snapshot(claim_id)["documents"]


@app.get("/api/documents/{doc_id}/file")
def document_file(doc_id: str):
    doc = db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, "Document not found")
    path = settings.upload_dir / doc["storedName"]
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(path, media_type=doc["mediaType"],
                        headers={"Content-Disposition": f'inline; filename="{doc["filename"]}"',
                                 "Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})


@app.post("/api/documents/{doc_id}/replace", status_code=202)
async def replace_document(doc_id: str, file: UploadFile = File(...)):
    (name, data), = await _read_uploads([file])
    doc = cm.replace_document(doc_id, name, data)
    return cm.public_document(doc, None)


@app.delete("/api/documents/{doc_id}")
def dismiss_document(doc_id: str):
    cm.dismiss_document(doc_id)
    return {"ok": True}


# ------------------------------------------------------------------ expenses

@app.get("/api/claims/{claim_id}/expenses")
def list_expenses(claim_id: str):
    return cm.snapshot(claim_id)["expenses"]


@app.patch("/api/expenses/{expense_id}")
def patch_expense(expense_id: str, changes: dict):
    if not changes:
        raise HTTPException(400, "No changes supplied")
    cm.update_fields(expense_id, changes, via="edit")
    return cm.public_expense(db.get_expense(expense_id))


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: str):
    """Soft delete: the expense is excluded from the claim and can be restored."""
    cm.set_excluded(expense_id, True, "Removed by employee")
    return {"ok": True}


class DuplicateResolution(BaseModel):
    resolution: str


@app.post("/api/expenses/{expense_id}/duplicate-resolution")
def duplicate_resolution(expense_id: str, body: DuplicateResolution):
    return cm.resolve_duplicate(expense_id, body.resolution)


class PolicyOverride(BaseModel):
    action: str
    justification: str | None = Field(default=None, max_length=500)


@app.post("/api/expenses/{expense_id}/policy-override")
def policy_override(expense_id: str, body: PolicyOverride):
    return cm.policy_decision(expense_id, body.action, body.justification)


# ------------------------------------------------------------------ chat

class ChatBody(BaseModel):
    message: str | None = Field(default=None, max_length=2000)
    questionId: str | None = None
    option: str | None = None


@app.post("/api/claims/{claim_id}/chat")
def chat(claim_id: str, body: ChatBody):
    reply = chat_agent.handle(claim_id, body.message, body.questionId, body.option)
    return {"reply": reply}


# ------------------------------------------------------------------ frontend

dist: Path = settings.frontend_dist
if (dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith(("api/", "mock-finance/")):
            raise HTTPException(404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")
