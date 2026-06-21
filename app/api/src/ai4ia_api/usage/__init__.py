"""Usage metering + cost ledger.

Observational governance: every chat completion is metered (token usage + an
estimated cost) and written to a per-user ledger so cost and traffic can be
tracked and reported. This is *observe-and-report* only; entitlement
enforcement (budgets/rate limits returning 429) is a separate later increment.

Honesty is a design goal: usage that the upstream did not report is recorded as
*unknown* (never silently summed as zero), cost is only estimated when both the
token usage and a price are known, and the price rates used are snapshotted onto
each record so historical totals never drift when ``pricing.json`` changes.
"""
