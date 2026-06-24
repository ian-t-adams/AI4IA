"""Admin-only user directory: hashed internal userId -> display name + email.

The usage ledger persists only the one-way UUIDv5 ``userId`` hash (see
``auth/userid.py``), so the admin dashboard can only show opaque hashes. This
package captures the ``name``/``email`` already present on the Entra/dev token
into a tiny admin-only store keyed by that same hash, and resolves it back at
admin read time. Names populate GOING FORWARD as each user makes an
authenticated request; the hash is irreversible, so history is never backfilled.

This is a deliberate, owner-approved reintroduction of PII (name/email) into the
admin plane only. Every write and read is best-effort: a store failure must never
break a chat turn or an admin read.
"""
