"""Runtime configuration, read from environment variables (optionally a .env file)."""
from __future__ import annotations

import json
import os
from datetime import date
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent

load_dotenv(PROJECT_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(_env("DATA_DIR", str(PROJECT_DIR / "data")))
        self.upload_dir = self.data_dir / "uploads"
        self.db_path = self.data_dir / "expense_agent.sqlite3"
        self.config_dir = Path(_env("POLICY_CONFIG_DIR", str(BACKEND_DIR / "config")))
        self.demo_dir = Path(_env("DEMO_RECEIPTS_DIR", str(PROJECT_DIR / "demo_receipts")))
        self.frontend_dist = Path(_env("FRONTEND_DIST", str(PROJECT_DIR / "frontend" / "dist")))

        # AI layer. EXTRACTION_MODE: auto (Claude if credentials exist, else offline),
        # claude (Claude only, fail loudly), offline (never call the API).
        self.extraction_mode = (_env("EXTRACTION_MODE", "auto") or "auto").lower()
        self.anthropic_model = _env("ANTHROPIC_MODEL", "claude-opus-5-5")
        self.anthropic_effort = _env("ANTHROPIC_EFFORT", "low")
        self.anthropic_timeout = float(_env("ANTHROPIC_TIMEOUT_SECONDS", "90"))
        self.use_refusal_fallback = (_env("ANTHROPIC_REFUSAL_FALLBACK", "true") or "true").lower() == "true"

        self.max_upload_bytes = int(_env("MAX_UPLOAD_MB", "10")) * 1024 * 1024
        self.max_files_per_upload = int(_env("MAX_FILES_PER_UPLOAD", "30"))
        self.worker_threads = int(_env("WORKER_THREADS", "4"))
        # Simulated per-document latency in offline mode so the processing pipeline is visible.
        self.offline_delay_seconds = float(_env("OFFLINE_PROCESSING_DELAY", "0.9"))

        self.employee_id = _env("DEMO_EMPLOYEE_ID", "EMP001")
        self.employee_name = _env("DEMO_EMPLOYEE_NAME", "Demo Employee")
        ref = _env("REFERENCE_DATE")
        self.reference_date_override = date.fromisoformat(ref) if ref else None

        self.mock_finance_failure_rate = float(_env("MOCK_FINANCE_FAILURE_RATE", "0"))

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    def today(self) -> date:
        """Reference 'today' for age/future-date rules. Overridable so the demo stays deterministic."""
        return self.reference_date_override or date.today()

    @property
    def has_anthropic_credentials(self) -> bool:
        return bool(_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN") or _env("ANTHROPIC_PROFILE"))

    @property
    def ai_enabled(self) -> bool:
        if self.extraction_mode == "offline":
            return False
        if self.extraction_mode == "claude":
            return True
        return self.has_anthropic_credentials


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_json_config(name: str) -> dict:
    path = get_settings().config_dir / name
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_policy() -> dict:
    """Policy is re-read on every evaluation so edits to policy.json apply without a restart."""
    return load_json_config("policy.json")
