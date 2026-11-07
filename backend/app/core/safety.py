"""Untrusted-data handling: event text is DATA, never instructions or commands."""
import re

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_INJECTION = re.compile(r"(?i)(ignore (all |any )?(previous|prior) instructions|system prompt|you are now|"
                        r"disregard .{0,30}(rules|instructions)|run the following command|\bsudo\b|rm -rf)")


def sanitize_untrusted(text: str, limit: int = 500) -> tuple[str, bool]:
    """Strip control chars, truncate; return (clean_text, looks_like_injection)."""
    clean = _CTRL.sub("", text)[:limit]
    return clean, bool(_INJECTION.search(clean))
