"""Standard tool response envelope for audit + consistent agent I/O.

Ported from ``reporanger/deep_research_agent/tool_plane/envelope.py``. Kept
narrow to what Outrider needs today; extensions land in a future shared
extraction.

Every tool-plane call — ``lead-content`` URL routing, connector fetches
from Linear / arXiv / GitHub — returns this envelope so the step-summary
audit block can render a uniform table of external I/O per run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Literal, Optional


TOOL_PLANE_VERSION = "0.1.0"

# ``ok``: successful fetch, payload / inline_snippet populated.
# ``not_found``: URL resolves to 404 or empty response (real absence).
# ``rate_limited``: connector's service is throttling us; retry later.
# ``timeout``: connector took longer than its budget.
# ``verification_failed``: fetch succeeded but semantic verifier rejected
#                          the content (author mismatch, title mismatch,
#                          content-vs-query mismatch).
# ``not_configured``: connector's auth is missing; caller should degrade
#                     gracefully (e.g., fall through to plain HTTP GET).
# ``error``: unclassified failure; ``error_code`` / ``inline_snippet``
#           carry the detail.
ToolStatus = Literal[
    "ok",
    "not_found",
    "rate_limited",
    "timeout",
    "verification_failed",
    "not_configured",
    "error",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ToolResponse:
    """Unified return shape for every tool-plane call.

    Fields intentionally match RepoRanger's envelope so downstream audit
    tooling built against RepoRanger's telemetry works against Outrider's
    tool-plane calls too. Extraction to a shared module tracked separately.
    """

    source_id: str
    connector: str
    retrieved_at: str
    connector_version: str
    latency_ms: float
    status: ToolStatus
    error_code: Optional[str] = None
    payload_ref: Optional[str] = None
    inline_snippet: Optional[str] = None
    data: Optional[dict[str, Any]] = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    @staticmethod
    def new_source_id(prefix: str = "src") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"


def tool_response_ok(
    *,
    connector: str,
    latency_ms: float,
    inline_snippet: Optional[str] = None,
    payload_ref: Optional[str] = None,
    data: Optional[dict[str, Any]] = None,
    connector_version: str = TOOL_PLANE_VERSION,
) -> ToolResponse:
    return ToolResponse(
        source_id=ToolResponse.new_source_id(connector),
        connector=connector,
        retrieved_at=_utc_now_iso(),
        connector_version=connector_version,
        latency_ms=latency_ms,
        status="ok",
        inline_snippet=inline_snippet,
        payload_ref=payload_ref,
        data=data,
    )


def tool_response_error(
    *,
    connector: str,
    latency_ms: float,
    error_code: str,
    message: Optional[str] = None,
    status: ToolStatus = "error",
    connector_version: str = TOOL_PLANE_VERSION,
) -> ToolResponse:
    return ToolResponse(
        source_id=ToolResponse.new_source_id(connector),
        connector=connector,
        retrieved_at=_utc_now_iso(),
        connector_version=connector_version,
        latency_ms=latency_ms,
        status=status,
        error_code=error_code,
        inline_snippet=message,
    )
