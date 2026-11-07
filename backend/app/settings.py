"""Backend settings. Read from the project's .env file and the environment; every name starts with AEISP_
(for example AEISP_OPERATOR_TOKEN). Secrets are never printed or returned by the API."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # backend/app/settings.py -> project root

VERSION = "0.5.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), env_prefix="AEISP_", extra="ignore")

    root: Path = ROOT

    # API tokens, one per role. Empty = that role is disabled. If NO token is set the API refuses everything.
    operator_token: str = ""   # may do everything, including approving deployments
    agent_token: str = ""      # for the AI agents: read, edit sources, build, propose (never approve)
    viewer_token: str = ""     # read-only (dashboard)

    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    build_workers: int = 1         # builds that may run at the same time
    proposal_ttl_s: int = 900      # how long a proposal waits for approval before it expires
    max_tail_lines: int = 2000     # most lines a log request may return

    @property
    def cors_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def db_path(self) -> Path:
        return self.root / "data" / "aeisp.db"

    @property
    def log_dir(self) -> Path:
        return self.root / "data" / "logs"

    @property
    def role_tokens(self) -> Dict[str, str]:
        """role -> token, only for roles that are enabled."""
        pairs = {"operator": self.operator_token, "agent": self.agent_token, "viewer": self.viewer_token}
        return {role: tok for role, tok in pairs.items() if tok}

    def public_dict(self) -> dict:
        """Everything that is safe to show: no tokens."""
        return {
            "version": VERSION,
            "root": str(self.root),
            "host": self.host,
            "port": self.port,
            "log_level": self.log_level,
            "cors_origins": self.cors_list,
            "build_workers": self.build_workers,
            "proposal_ttl_s": self.proposal_ttl_s,
            "roles_enabled": sorted(self.role_tokens),
            "db_path": str(self.db_path),
        }