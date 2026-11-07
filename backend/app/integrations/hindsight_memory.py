"""Hindsight Memory integration wrapping official hindsight-client SDK.

Provides:
- Semantic recall for incident symptom correlation and historical runbook retrieval
- Verified post-mortem retention (gated: unverified hypotheses are never promoted)
- Synthetic A/B experiment fixture seeding
- Automatic bank initialization with specialized SRE incident response mission profile
- Safe connection diagnostics that never expose credentials
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


class HindsightError(RuntimeError):
    pass


@dataclass
class Memory:
    text: str
    type: str | None = None
    id: str | None = None
    document_id: str | None = None
    metadata: dict[str, Any] | None = None
    score: float | None = None


class HindsightMemory:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 bank_id: str | None = None, timeout: float = 30.0, max_retries: int = 3):
        self.base_url = (base_url or os.environ.get("HINDSIGHT_BASE_URL", "")).rstrip("/")
        if not self.base_url:
            raise HindsightError("HINDSIGHT_BASE_URL not configured")
        self.bank_id = bank_id or os.environ.get("HINDSIGHT_BANK_ID", "opsmemory")
        self.api_key = api_key or os.environ.get("HINDSIGHT_API_KEY")
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = None

    def _get_client(self):
        if self._client is None:
            from hindsight_client import Hindsight
            self._client = Hindsight(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout
            )
        return self._client

    def check_connection(self) -> dict[str, Any]:
        """Safely test Hindsight connection and bank profile without exposing secrets."""
        t0 = time.monotonic()
        try:
            client = self._get_client()
            version_resp = client.get_version()
            latency = round(time.monotonic() - t0, 3)
            bank_status = "unknown"
            try:
                cfg = client.get_bank_config(self.bank_id)
                bank_status = "present" if cfg else "not_configured"
            except Exception:
                bank_status = "not_found_or_uninitialized"
            return {
                "configured": True,
                "connected": True,
                "base_url": self.base_url,
                "api_version": getattr(version_resp, "api_version", "unknown"),
                "bank_id": self.bank_id,
                "bank_status": bank_status,
                "latency_s": latency
            }
        except Exception as exc:
            return {
                "configured": True,
                "connected": False,
                "base_url": self.base_url,
                "bank_id": self.bank_id,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_s": round(time.monotonic() - t0, 3)
            }

    def ensure_bank_exists(self, bank_id: str | None = None) -> bool:
        """Ensure the target memory bank exists, initializing with SRE incident profile if needed."""
        target_bank = bank_id or self.bank_id
        client = self._get_client()
        try:
            client.get_bank_config(target_bank)
            return True
        except Exception:
            pass

        try:
            client.create_bank(
                bank_id=target_bank,
                retain_mission="Extract operational incident facts, symptoms, error signatures, confirmed root causes, and remediation runbooks.",
                reflect_mission="Analyze operational incident symptoms and recommend verified remediation runbooks based on past incident post-mortems."
            )
            return True
        except Exception as exc:
            raise HindsightError(f"failed to initialize memory bank '{target_bank}': {type(exc).__name__}") from exc

    def retain_post_mortem(self, incident_id: str, text: str, *, verified: bool,
                           resolved_by: str | None = None):
        """Verified status is stored in the memory itself so an unverified hypothesis
        can never be recalled later as a confirmed fact."""
        label = "VERIFIED post-mortem" if verified else "UNVERIFIED hypothesis (recovery not confirmed)"
        client = self._get_client()
        try:
            return client.retain(
                bank_id=self.bank_id,
                content=f"[{label}] {text}",
                context="incident post-mortem",
                timestamp=datetime.now(timezone.utc),
                document_id=f"incident-{incident_id}",
                metadata={"incident_id": incident_id, "verified": str(verified).lower(),
                          "resolved_by": resolved_by or ""},
            )
        except Exception as exc:
            raise HindsightError(f"Hindsight retain failed: {type(exc).__name__}") from exc

    def retain_experiment_seed(self, document_id: str, text: str):
        """Write an explicitly synthetic A/B fixture; never label it a verified incident."""
        client = self._get_client()
        try:
            return client.retain(
                bank_id=self.bank_id,
                content=f"[SYNTHETIC A/B EXPERIMENT FIXTURE - NOT PRODUCTION EVIDENCE] {text}",
                context="OpsMemory controlled memory experiment fixture",
                timestamp=datetime.now(timezone.utc),
                document_id=document_id,
                metadata={"evidence_type": "synthetic_experiment_fixture", "verified": "false"},
            )
        except Exception as exc:
            raise HindsightError(f"Hindsight retain experiment seed failed: {type(exc).__name__}") from exc

    def recall_similar(self, query: str, max_tokens: int = 2048, budget: str = "mid") -> list[Memory]:
        """Recall relevant historical incident memories matching symptoms and topology."""
        client = self._get_client()
        try:
            res = client.recall(bank_id=self.bank_id, query=query,
                                max_tokens=max_tokens, budget=budget)
            results = getattr(res, "results", None) or []
            return [
                Memory(
                    text=r.text,
                    type=getattr(r, "type", None),
                    id=getattr(r, "id", None),
                    document_id=getattr(r, "document_id", None),
                    metadata=getattr(r, "metadata", None),
                    score=getattr(r, "scores", {}).get("final") if isinstance(getattr(r, "scores", None), dict) else None
                )
                for r in results
            ]
        except Exception as exc:
            raise HindsightError(f"Hindsight recall failed: {type(exc).__name__}") from exc
