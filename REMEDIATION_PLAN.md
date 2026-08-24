# ISGEC Proposal System — Remediation Plan

**Date:** 2026-08-21
**Scope:** All 12 findings from `LOGIC_REVIEW.md` + integration tests
**Status:** Approved — not yet implemented

## Decisions (confirmed)

| Decision | Choice |
|---|---|
| Scope | All 12 findings + automated test suite |
| Auth | Session cookie (itsdangerous-signed) + admin/staff roles + CSRF |
| Payment cap | Removed; Razorpay orders always created for full quote value |
| Tests | pytest suite covering the review's minimum acceptance tests |

---

## Phase 1 — Authentication, Authorization, Audit (Finding #1)

**New file: `app/auth.py`**

- `User` model in `models.py`: `id, username, password_hash, role ("admin"|"staff"), is_active, created_at`
- Password hashing: stdlib `hashlib.pbkdf2_hmac` (SHA-256, 600k iterations, per-user salt) — no new heavy dependencies
- Sessions: `itsdangerous.TimestampSigner` signing `{user_id, role}` → `isgec_session` cookie (`HttpOnly`, `SameSite=Lax`, 12h TTL)
- CSRF: double-submit cookie (`isgec_csrf`); dependency rejects mutating requests without matching `X-CSRF-Token` header / form field
- Dependencies: `require_user`, `require_admin`; seed first admin from `ADMIN_USERNAME` / `ADMIN_PASSWORD` env at startup if user table is empty
- Login/logout pages (`/login`, `/logout`) + templates

**Role matrix**

| Endpoint group | staff | admin |
|---|---|---|
| Dashboard, proposals read, RFQ create, chat, customers list | ✅ | ✅ |
| Email approve/reject/retry/check-now, status change, catalog & KB mutations, customer create/import | ❌ | ✅ |
| `/pay/{id}`, `/api/payments/verify`, `/api/payments/{id}/failed`, `/api/track/open/*`, `/api/health`, login | public (signature-guarded / customer-facing) | same |

**New model:** `AuditLog(id, user_id, action, entity_type, entity_id, detail JSON, created_at)` — written on every status change, approval/rejection/retry, import, catalog delete, payment confirm.

---

## Phase 2 — Payment Semantics (Findings #2, #11)

`app/services/payments.py`:

- Delete `RAZORPAY_MAX_AMOUNT` cap logic (and the config var); order amount = full `total_gross` always
- Add `quote_total` column to `Payment` (snapshot for audit)
- `confirm_payment`: after signature check, optionally re-fetch order payments server-side from the Razorpay API to confirm captured amount == order amount before marking proposal `accepted`
- **Retry endpoint** `POST /api/payments/proposal/{proposal_id}/retry`: only allowed when latest payment is `failed` and proposal is `sent`; creates a *new* Razorpay order/Payment row (old row preserved as history)
- `payment.html`: remove capped-note block; add "Retry payment" button that calls retry endpoint and redirects to new `/pay/{id}`

---

## Phase 3 — Idempotent Approval (Finding #3)

`email_pipeline.py::send_quote_for_email` + `routers/email.py::approve_email`:

- `SELECT ... FOR UPDATE` on the `EmailMessage` row; re-check `status == "quoted"` inside the lock (kills the TOCTOU race)
- Before creating an order: query existing `Payment` for this email with status `created`/`paid` → reuse instead of creating duplicates
- Guard: refuse if proposal already `sent`

---

## Phase 4 — Durable Email Ingestion (Findings #4, #5)

`services/email_client.py`:

- `fetch_unread(mark_seen=False)` — never mark seen during fetch
- New `mark_seen(uids: list[int])` — one IMAP session, called by poll cycle **after** each email's DB record is durably persisted
- Crash matrix:
  - Crash before insert → email still unseen, refetched next cycle (message_id dedup prevents double-processing)
  - Crash mid-processing → row exists as `processing` → `_recover_stale_records` marks failed → retryable

`routers/email.py::retry_email`:

- **No deletion.** Reset in place: `status="processing"`, clear error/triage, increment new `retry_count` column
- Refactor `process_email` into `_process_record(record, db)` so retry reuses stored data; missing attachment paths handled gracefully

---

## Phase 5 — Status Transitions (Finding #6)

New `app/services/workflow.py`:

```python
ALLOWED = {
    "draft":    {"sent", "rejected"},
    "sent":     {"accepted", "rejected"},
    "accepted": set(),          # terminal
    "rejected": {"draft"},      # reopen
}
```

- Enforced in `PUT /proposals/{id}/status` (admin-only now)
- Internal callers (pipeline auto-send, payment confirm, reply triage) use `transition(proposal, new_status, actor)` which validates + writes AuditLog
- Admin override path requires a `reason` form field, logged

---

## Phase 6 — Reply Matching Hardening (Finding #7)

`_match_quoted_thread`:

- Header match (In-Reply-To/References): additionally require sender address equality (case-insensitive) with the quoted email's `from_addr`
- Subject-number match: require sender match AND proposal currently `sent`
- Sender mismatch or multiple candidate quotes → record becomes `needs_review` with triage note (never silently accepts/rejects)
- Acceptance/rejection rejected if proposal not in `sent` state

---

## Phase 7 — Server-Side Commercial Validation (Finding #8)

New `recalculate_proposal_totals(...)` in `services/pricing.py`:

- Items with `product_id`: unit prices forced from catalog (`price_net`, gross derived from `vat_rate`); LLM-supplied prices ignored
- Subtotals recomputed as `qty × unit_price`; totals = sum of subtotals; currency taken from catalog, never from LLM
- Quantity clamped to `[1, 10000]`; negative/NaN prices rejected
- Applied in both paths: `process_rfq` (manual — unknown SKUs get zero price + `on_request`) and email pipeline path

---

## Phase 8 — Medium Fixes (#9, #10, #12)

- **Ignored tab:** `status_map["ignored"] = ["ignored"]` in `routers/email.py`; allow tab in `main.py`; add tab UI in `email.html` showing `triage_note`
- **Customer dedup:** normalize names (lowercase, strip non-alphanumerics + legal suffixes ltd/pvt/llp/inc/co); skip + report duplicates in `import_customers_from_csv`; response includes `skipped` list; same normalization on manual create
- **Reporting consistency:** `list_proposals` count applies the status filter; centralize metrics into `dashboard_metrics(db)` defining quoted / accepted / paid / outstanding explicitly; update dashboard stats endpoint + `index.html` labels

---

## Phase 9 — Test Suite

**Deps added:** `pytest`, `pytest-asyncio`, `aiosqlite`, `respx` (mock Razorpay/Groq HTTP).
`LIGHTWEIGHT_MIGRATIONS` guarded to skip on SQLite dialect (fresh `create_all` covers columns).

| File | Covers (minimum acceptance tests) |
|---|---|
| `test_auth.py` | Unauthenticated → 401; staff can't approve/mutate; CSRF rejection; login works |
| `test_payments.py` | No cap (order == quote total); double-verify idempotent; failed payment retry creates new order, old preserved |
| `test_approval.py` | Concurrent/double approval → exactly 1 email + 1 payment order |
| `test_email_pipeline.py` | Duplicate message_id skipped; retry keeps original row id; ignored email visible w/ reason; wrong-sender reply can't accept/reject; ambiguous reply → needs_review |
| `test_transitions.py` | Invalid transitions rejected; valid logged to audit |
| `test_pricing.py` | Hallucinated LLM totals recalculated from catalog prices |

---

## Execution Order

Phases 1→9 sequentially (each phase compiles + smoke-runs).

DB changes via new `LIGHTWEIGHT_MIGRATIONS` entries: `users`, `audit_logs` tables (create_all), plus `ADD COLUMN IF NOT EXISTS` for `isgec_payments.quote_total`, `isgec_emails.retry_count`.

`.env.example` updated (`ADMIN_USERNAME`, `ADMIN_PASSWORD`; `RAZORPAY_MAX_AMOUNT` removed).

**Out of scope (noted, not blocking):** moving IMAP/file IO off the event loop (blocking-call wrappers only where trivial), credential rotation reminder.
