"""The knowledge service: keeps the knowledge base fresh and answers questions about past work."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List, Optional

from .collect import collect_all
from .store import KnowledgeStore, as_context

STALE_S = 20.0          # collect again before a lookup if the last collection is older than this


class KnowledgeService:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.store = KnowledgeStore(db_path)
        self._last = 0.0
        self._lock = threading.Lock()

    def collect(self) -> dict:
        with self._lock:
            out = collect_all(self.db_path, self.store)
            self._last = time.monotonic()
            return out

    def fresh(self) -> None:
        if time.monotonic() - self._last > STALE_S:
            self.collect()

    def search(self, text: str, kind: Optional[str] = None, project: Optional[str] = None, limit: int = 10) -> list:
        self.fresh()
        return self.store.search(text, kind, project, limit)

    def related(self, text: str, project: Optional[str] = None, limit: int = 5) -> List[dict]:
        self.fresh()
        return self.store.related(text, project, limit)

    def context_for(self, text: str, project: Optional[str] = None, limit: int = 5) -> tuple:
        """(context text, ids used) for a new task."""
        hits = self.related(text, project, limit)
        return as_context(hits), [h["id"] for h in hits]
