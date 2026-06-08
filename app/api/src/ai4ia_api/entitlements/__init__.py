"""Phase 6B — per-user entitlement enforcement.

Layered on the Phase 6A usage ledger, this turns *observation* into *governance*:
a chat turn can be refused (HTTP 429) when a user is over a configured rate limit
or token/cost budget, or blocked (HTTP 403) when their account is disabled.

Product posture (deliberate): the mechanism ships **effectively unlimited**. The
default entitlement has no limits, so *no user is ever limited unless an admin
explicitly sets a limit on that specific user*. Turning enforcement on therefore
costs the normal user nothing — :meth:`EntitlementService.check` short-circuits to
*allow* with zero ledger IO whenever the effective entitlement carries no limits.

Enforcement is **best-effort / soft**, not a strict quota: the check reads the
ledger *before* a turn is metered, so concurrent in-flight turns can overshoot a
limit slightly. Ledger/store failures **fail open** (availability over strict
caps) — except an explicitly ``disabled`` account, which fails closed from the
last-known cached entitlement. This is the right trade-off for a personal/demo app.
"""
