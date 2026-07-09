"""Process-local Web IQ health recorder for the admin diagnostics panel.

The Web IQ capability fails **soft**: a categorized :class:`WebSearchError`
(``auth`` / ``permission`` / ``rate_limit`` / ``status`` / ``connection`` /
``unknown``) is turned into a clean, user-safe ``{"error": ...}`` and the turn
continues. That is correct for the model, but it also means a *misconfiguration*
(the common one: web search enabled with no API key, so the api's managed identity
falls back to EntraID but is not entitled to Web IQ) is invisible — the errors are
returned to the model and then dropped.

This recorder makes those failures visible to an app admin **without adding a
store**. It is a derived, rebuildable diagnostic signal:

* Process-local and in-memory (per replica). It is NOT canonical data — it is lost
  on restart and is not aggregated across replicas, which is fine for a "is web
  search healthy right now / why is it failing" panel. The durable, cross-replica
  view remains App Insights (the capability also logs each failure category).
* Never raises: recording is best-effort so it can never affect a chat turn.
* Carries no user identity and caps the free-text ``detail`` — the snapshot is an
  admin-plane, de-identified operational summary.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone

from pydantic import BaseModel, Field

# Stable display order for the categories emitted by ``websearch.client``. Any
# unrecognized category sorts last (alphabetically) so the panel never drops a row.
CATEGORY_ORDER: tuple[str, ...] = (
    "auth",
    "permission",
    "rate_limit",
    "status",
    "connection",
    "unknown",
)

# Bound the recent-failure ring buffer and the retained free-text detail so the
# snapshot stays small and demo-safe.
RECENT_MAX = 20
DETAIL_MAX = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WebSearchFailure(BaseModel):
    """One recent categorized failure (no user identity)."""

    category: str
    detail: str | None = None
    at: datetime


class WebSearchCategoryCount(BaseModel):
    category: str
    count: int


class WebSearchHealthReport(BaseModel):
    """Admin-plane snapshot of Web IQ call health for this replica.

    ``enabled`` / ``authMode`` are config posture filled in by the admin endpoint
    (not the recorder) so the panel can explain *why* calls fail — e.g. enabled
    with ``authMode == "managed_identity"`` and a run of ``auth`` failures points
    straight at a managed identity that is not entitled to Web IQ.
    """

    enabled: bool = False
    # "api_key" | "managed_identity" | "unconfigured" (never the key itself).
    authMode: str = "unconfigured"

    startedAt: datetime
    generatedAt: datetime = Field(default_factory=_now)

    totalCalls: int = 0
    successes: int = 0
    failures: int = 0
    lastSuccessAt: datetime | None = None
    lastFailureAt: datetime | None = None

    byCategory: list[WebSearchCategoryCount] = Field(default_factory=list)
    recent: list[WebSearchFailure] = Field(default_factory=list)


class WebSearchHealth:
    """Bounded, in-memory counters + a ring buffer of recent categorized failures.

    Constructed once at startup and shared between the per-turn capability (which
    records) and the admin endpoint (which snapshots). Recording is best-effort and
    never raises; a short non-reentrant lock guards the counters (held only for the
    field updates, never across an await).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = _now()
        self._successes = 0
        self._failures = 0
        self._last_success: datetime | None = None
        self._last_failure: datetime | None = None
        self._by_category: dict[str, int] = {}
        self._recent: deque[WebSearchFailure] = deque(maxlen=RECENT_MAX)

    def record_success(self) -> None:
        """Count a successful Web IQ call (best-effort; never raises)."""
        try:
            with self._lock:
                self._successes += 1
                self._last_success = _now()
        except Exception:  # noqa: BLE001 - diagnostics must never affect a turn
            pass

    def record_failure(self, category: str | None, detail: str | None = None) -> None:
        """Count a categorized failure + push it onto the recent ring buffer.

        ``detail`` is single-lined and length-capped; no user identity is stored.
        """
        try:
            cat = str(category or "unknown")
            clean = (
                str(detail or "").replace("\n", " ").replace("\r", " ").strip()[:DETAIL_MAX]
                or None
            )
            now = _now()
            with self._lock:
                self._failures += 1
                self._last_failure = now
                self._by_category[cat] = self._by_category.get(cat, 0) + 1
                self._recent.appendleft(
                    WebSearchFailure(category=cat, detail=clean, at=now)
                )
        except Exception:  # noqa: BLE001 - diagnostics must never affect a turn
            pass

    def snapshot(self) -> WebSearchHealthReport:
        """Point-in-time, de-identified report (config posture filled by caller)."""
        with self._lock:
            ordered = sorted(
                self._by_category.items(),
                key=lambda kv: (
                    CATEGORY_ORDER.index(kv[0]) if kv[0] in CATEGORY_ORDER else len(CATEGORY_ORDER),
                    kv[0],
                ),
            )
            return WebSearchHealthReport(
                startedAt=self._started,
                totalCalls=self._successes + self._failures,
                successes=self._successes,
                failures=self._failures,
                lastSuccessAt=self._last_success,
                lastFailureAt=self._last_failure,
                byCategory=[
                    WebSearchCategoryCount(category=c, count=n) for c, n in ordered
                ],
                recent=list(self._recent),
            )
