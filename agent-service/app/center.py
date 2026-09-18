"""Diagnosis Center helpers: opaque, filter-bound cursors and input validation.

Cursors are keyset positions bound to the exact filter set that produced them
(design §3.1): changing filters invalidates the cursor instead of silently
returning a different result set.
"""

import base64
import hashlib
import json
from datetime import datetime
from typing import Any, Optional
from uuid import UUID


class CursorError(ValueError):
    """Cursor is malformed or does not match the current filters."""


VALID_STATUSES = ("queued", "investigating", "completed", "failed")
VALID_TRIGGERS = ("manual", "alert")


def filter_fingerprint(filters: dict[str, Any]) -> str:
    payload = json.dumps({k: filters.get(k) for k in sorted(filters)}, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def encode_cursor(fingerprint: str, **keys: Any) -> str:
    raw = json.dumps({"v": 1, "f": fingerprint, **keys}, separators=(",", ":"),
                     default=str).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, fingerprint: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:  # noqa: BLE001 - any decode failure is a 422
        raise CursorError("invalid cursor") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise CursorError("invalid cursor")
    if data.get("f") != fingerprint:
        raise CursorError("cursor does not match the current filters")
    return data


def parse_iso(value: str) -> str:
    """Validate an ISO-8601 timestamp (raises ValueError on bad input)."""
    datetime.fromisoformat(value)
    return value


def validate_viewer_id(viewer_id: str) -> str:
    """Viewer ids are opaque to the server but must be canonical UUIDv4 strings.

    Vague ids (non-UUID, non-canonical, wrong version) would make read receipts
    ambiguous across browsers, so they are rejected up front.
    """
    try:
        parsed = UUID(viewer_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("viewer_id must be a UUID") from exc
    if parsed.version != 4 or str(parsed) != (viewer_id or "").lower():
        raise ValueError("viewer_id must be a canonical UUIDv4")
    return viewer_id


def validate_enum(value: Optional[str], allowed: tuple[str, ...], name: str) -> Optional[str]:
    if value is None:
        return None
    if value not in allowed:
        raise ValueError(f"invalid {name}: {value!r}")
    return value
