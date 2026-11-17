"""Application configuration.

Every value can be overridden with a BT_-prefixed environment variable or in
backend/.env, so thresholds and labels change without redeployment (NFR-14).
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_prefix="BT_", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://bugtracker:bugtracker@localhost:5432/bugtracker"
    # pool_size + max_overflow must exceed the worker thread count (40 per process): a request
    # keeps its connection until its clean-up runs on a worker thread, so a smaller pool can
    # leave every thread waiting for a connection that only another waiting thread can free.
    db_pool_size: int = 20
    db_max_overflow: int = 40
    # Request handler threads per process. Capped at start-up to stay below the pool size (see main.py).
    worker_threads: int = 40

    secret_key: str = "dev-only-change-me"
    session_idle_hours: int = 8
    bcrypt_rounds: int = 12
    verification_code_hours: int = 24
    # Development convenience only: the verification code is returned by the
    # register endpoint so the flow can be exercised without a mail server.
    expose_verification_code: bool = False

    upload_dir: Path = BASE_DIR / "uploads"
    model_dir: Path = BASE_DIR.parent / "models"
    max_attachment_bytes: int = 10 * 1024 * 1024

    # AI
    ai_enabled: bool = True
    semantic_enabled: bool = True
    # Base encoder. If models/sbert-duplicates exists (written by ml.train_duplicates), that
    # fine-tuned copy is loaded instead.
    sbert_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    duplicate_threshold: float = 0.70      # at or above: flagged as probable duplicate
    duplicate_display_floor: float = 0.35  # below: not shown to the reporter at all
    duplicate_candidates: int = 100        # BM25 shortlist handed to the semantic stage
    duplicate_fusion: float = 0.7          # weight of semantic similarity in the final ordering
    severity_labels: str = "Low,Medium,High,Critical"

    # Cold start and retraining (FR-25, FR-29)
    cold_start_threshold: int = 100
    retrain_interval_days: int = 30
    retrain_after_verified: int = 200
    limited_history_threshold: int = 5
    default_capacity: int = 5

    # Notifications
    email_backend: str = "console"  # console | smtp | memory
    email_from: str = "bugtracker@localhost"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    ntfy_url: str = ""  # empty disables push; point at the self-hosted server
    ntfy_topic_prefix: str = "bugtracker"
    ntfy_token: str = ""

    @property
    def severity_label_list(self) -> list[str]:
        return [s.strip() for s in self.severity_labels.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
