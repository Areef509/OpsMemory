"""Groq chat-completions client (OpenAI-compatible endpoint), stdlib only.

Provides: auth via env, configurable model, timeout, retry with backoff on
429/5xx/network errors, JSON-mode output parsing, content normalization,
and safe diagnostics that never expose secrets.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMError(RuntimeError):
    pass


def _extract_json_content(content: str) -> str:
    """Strip markdown fences and reasoning tags that some models emit."""
    text = content.strip()
    if "<think>" in text and "</think>" in text:
        text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _safe_error_detail(e: urllib.error.HTTPError) -> str:
    """Extract safe error detail from provider response without exposing secrets."""
    try:
        body = e.read(1024).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and "error" in parsed:
                err = parsed["error"]
                if isinstance(err, dict) and "message" in err:
                    return str(err["message"])[:200]
                return str(err)[:200]
        except Exception:
            pass
        return body[:120].strip()
    except Exception:
        return ""


def mask_secret(secret: str | None) -> str:
    """Mask a secret key safely for logs and diagnostics (e.g. gsk_...a1b2)."""
    if not secret:
        return "<not set>"
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None,
                 url: str | None = None, timeout: float = 30.0,
                 max_retries: int = 3, backoff: float = 0.5):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not self.api_key:
            raise LLMError("GROQ_API_KEY not configured")
        self.model = model or os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        self.url = url or os.environ.get("GROQ_API_URL", DEFAULT_URL)
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff

    def chat_json(self, system: str, user: str) -> tuple[dict, dict]:
        """Send chat completion request expecting JSON object response.

        Returns (parsed_json, meta) where meta contains the model id, latency, and attempts.
        """
        body = json.dumps({
            "model": self.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ]
        }).encode("utf-8")

        last = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                self.url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "OpsMemory/1.0"
                }
            )
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    payload = json.loads(r.read())
                raw_content = payload["choices"][0]["message"]["content"]
                clean_content = _extract_json_content(raw_content)
                parsed = json.loads(clean_content)
                return parsed, {
                    "model": payload.get("model", self.model),
                    "latency_s": round(time.monotonic() - t0, 3),
                    "attempts": attempt + 1
                }
            except urllib.error.HTTPError as e:
                detail = _safe_error_detail(e)
                detail_suffix = f": {detail}" if detail else ""
                last = f"HTTP {e.code}{detail_suffix}"
                if e.code not in (429, 500, 502, 503, 504):
                    raise LLMError(f"non-retryable {last}") from e
                if e.code == 429 and attempt < self.max_retries:
                    retry_after = e.headers.get("Retry-After")
                    if retry_after:
                        try:
                            sleep_s = min(float(retry_after), 10.0)
                            time.sleep(sleep_s)
                            continue
                        except (ValueError, TypeError):
                            pass
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"network/timeout: {e}"
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                raise LLMError(f"malformed provider response: {e}") from e

            if attempt < self.max_retries:
                time.sleep(self.backoff * (2 ** attempt))

        raise LLMError(f"gave up after {self.max_retries + 1} attempts ({last})")

    def check_connection(self) -> dict[str, Any]:
        """Safely test provider connectivity without exposing credentials."""
        t0 = time.monotonic()
        try:
            parsed, meta = self.chat_json(
                system="You are a health probe. Reply with single JSON object: {\"status\": \"ok\"}.",
                user="ping"
            )
            return {
                "configured": True,
                "connected": True,
                "model": meta.get("model", self.model),
                "latency_s": meta.get("latency_s", round(time.monotonic() - t0, 3)),
                "attempts": meta.get("attempts", 1),
                "response": parsed
            }
        except Exception as exc:
            return {
                "configured": True,
                "connected": False,
                "model": self.model,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_s": round(time.monotonic() - t0, 3)
            }
