"""
Persistent memory for PatchPilot, backed by Firestore.

This gives the agent fleet a per-repo "case history": every diagnosis and
patch attempt is recorded, so future runs can recognize recurring failure
patterns (e.g. "this repo's tests flake on the same race condition every
few weeks") instead of re-deriving the same fix from scratch.

Collection layout:
  repos/{repo_full_name}/incidents/{run_id}
    - created_at, root_cause, patch_summary, outcome, pr_url
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass, field

from google.cloud import firestore

logger = logging.getLogger("patchpilot.memory_store")


@dataclass
class Incident:
    run_id: int
    repo_full_name: str
    root_cause: str
    patch_summary: str
    outcome: str  # "pr_opened" | "no_fix_found" | "validation_failed"
    pr_url: str | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())


class MemoryStore:
    def __init__(self, client: firestore.Client | None = None):
        self._db = client
        if self._db is None:
            try:
                self._db = firestore.Client()
            except Exception as exc:  # pragma: no cover - exercised in tests by patching Client
                logger.warning("Firestore unavailable; continuing in local/offline mode: %s", exc)
                self._db = None

    def _available(self) -> bool:
        return self._db is not None

    def record_incident(self, incident: Incident) -> None:
        if not self._available():
            logger.debug("Skipping incident write because Firestore is unavailable")
            return
        doc_ref = (
            self._db.collection("repos")
            .document(incident.repo_full_name.replace("/", "__"))
            .collection("incidents")
            .document(str(incident.run_id))
        )
        doc_ref.set(asdict(incident))

    def recent_incidents(self, repo_full_name: str, limit: int = 5) -> list[dict]:
        """Return the most recent past incidents for this repo, newest first.

        Used as few-shot context for the LogAnalyzerAgent so it can recognize
        recurring failures instead of treating every run as novel.
        """
        if not self._available():
            return []
        col = (
            self._db.collection("repos")
            .document(repo_full_name.replace("/", "__"))
            .collection("incidents")
        )
        docs = (
            col.order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [d.to_dict() for d in docs]

    def similar_root_causes(self, repo_full_name: str, needle: str, limit: int = 20) -> list[dict]:
        """Cheap client-side substring match over recent root causes.

        A production version would embed root causes with a Vertex AI
        embedding model and do a vector similarity lookup; this keeps the
        hackathon scope tractable while preserving the same interface.
        """
        candidates = self.recent_incidents(repo_full_name, limit=limit)
        needle_lower = needle.lower()
        return [c for c in candidates if needle_lower in c.get("root_cause", "").lower()]
