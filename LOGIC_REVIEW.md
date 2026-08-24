# ISGEC Proposal System: Logic and Business-Risk Review

**Review date:** 2026-08-21

## Executive Summary

The core workflow is functional, but several business-control gaps could be exposed during a company review or production use. The highest-risk areas are missing access control, payment amount validation, duplicate quote sending, email-processing durability, unrestricted status changes, and trusting AI-generated commercial values.

## Priority Findings

### Critical

#### 1. Missing authentication and authorization

The business APIs do not currently enforce user authentication or role-based authorization. This includes catalog changes, customer creation, proposal status changes, email approval/rejection, email polling, and payment verification.

**Business impact:** An unauthorized caller could alter products, inspect customer/email information, approve or reject quotations, or trigger outbound actions.

**Recommended action:** Add authentication, role checks, CSRF protection for browser mutations, and restrict administrative endpoints.

#### 2. Partial payment can be recorded as full acceptance

Razorpay orders are capped at `RAZORPAY_MAX_AMOUNT`, but successful payment always changes the full proposal status to `accepted`. A quote above the cap can therefore be marked accepted after paying less than the quotation total.

**Business impact:** The system may record an underpayment as a fully paid commercial order.

**Recommended action:** Remove the cap for production payments, or mark capped payments as partial and block fulfillment until the outstanding amount is settled.

### High

#### 3. Quote approval is not idempotent

Repeated approval requests can create multiple payment orders and send duplicate quotation emails.

**Recommended action:** Store and reuse one payment order per proposal/email, lock the email record during approval, and add an idempotency key or send-attempt record.

#### 4. Emails are marked read before processing completes

IMAP fetch marks messages as seen before database persistence, extraction, embedding, and proposal generation finish.

**Business impact:** A process crash or provider failure can permanently hide an email from later polling.

**Recommended action:** Persist a durable processing record first, use a checkpoint or UID table, and mark messages read only after successful handling.

#### 5. Retry deletes the original email record

The retry endpoint deletes the existing email row before processing it again.

**Business impact:** Audit history is lost, and deletion can conflict with payment records linked to the email.

**Recommended action:** Retry in place and retain processing attempts, errors, and timestamps.

#### 6. Proposal status transitions are unrestricted

The status API allows `draft`, `sent`, `accepted`, and `rejected` without checking the current status or user role.

**Business impact:** A sent or accepted proposal can be changed back to draft or rejected without an audit trail.

**Recommended action:** Define allowed state transitions and require privileged override actions with audit logging.

#### 7. Customer replies may be linked to the wrong quotation

When reply headers do not identify a quote, subject matching selects the latest replied email for the proposal number without verifying the sender.

**Business impact:** A message containing a quotation number could potentially affect the wrong customer quotation.

**Recommended action:** Match `In-Reply-To` or `References`, sender address, proposal number, and customer identity. Require manual review when multiple matches exist.

#### 8. AI-generated prices and totals are trusted directly

The proposal workflow persists model-generated quantities, prices, subtotals, totals, and currency without sufficient server-side validation.

**Business impact:** Hallucinated or mathematically incorrect commercial values could be sent to customers.

**Recommended action:** Treat AI output as a draft only. Validate fields, recalculate all totals server-side, and use approved catalog prices for quoted items.

### Medium

#### 9. Ignored emails are invisible in the dashboard

Non-RFQ messages are stored with status `ignored`, but the email dashboard tabs do not display ignored records.

**Business impact:** An email can be successfully checked and stored but appear to users as if it disappeared.

**Recommended action:** Add an “Ignored/Triage” tab or show ignored messages in the issues view with the triage reason.

#### 10. Customer imports are not deduplicated

Every customer CSV upload creates new customer records.

**Business impact:** Re-uploading a file creates duplicate customers and ambiguous assignments.

**Recommended action:** Normalize company names, support an external customer key, detect duplicates, and report skipped rows.

#### 11. Failed payments cannot be retried

Failed payments are blocked by the payment page, while the proposal UI can still present a payment action.

**Recommended action:** Create a new Razorpay order for every retry and preserve the old failed payment as history.

#### 12. Dashboard and list totals can disagree

Proposal list pagination counts all proposals even when filtered by status. Sales totals are calculated separately from paid payment rows.

**Business impact:** Users may see misleading counts or totals after rejected, superseded, or duplicate proposals.

**Recommended action:** Centralize reporting queries and explicitly define quoted, accepted, paid, outstanding, and rejected metrics.

## Operational Concerns

- External service failures are sometimes swallowed or reduced to console logs.
- There are no visible automated tests for duplicate email delivery, concurrent approval, payment retries, capped payments, status transitions, or customer-reply matching.
- Email polling performs blocking IMAP and file operations inside the async application process.
- Environment credentials must remain outside source control and should be rotated if previously exposed.

## Recommended Remediation Order

1. Add authentication, authorization, and audit logging.
2. Correct payment amount and acceptance semantics.
3. Make quote approval and payment-order creation idempotent.
4. Make email ingestion durable and retry-safe.
5. Add proposal status-transition rules.
6. Validate and recalculate all AI-generated commercial values.
7. Add dashboard visibility for ignored/triage emails.
8. Add integration tests for the full email-to-quote-to-payment workflow.

## Minimum Acceptance Tests

- A user without permission cannot approve, reject, delete, or import records.
- A quote above the payment limit cannot be marked fully paid with a partial amount.
- Double-clicking approval sends one email and creates one payment order.
- A failed processing attempt is visible and retryable without deleting the original record.
- A duplicate IMAP message is skipped only when its message ID already exists.
- An ignored email is visible with its triage reason.
- AI-generated totals are recalculated from validated line items.
- Invalid proposal status transitions are rejected.
- A reply from an unrelated sender cannot accept or reject a quotation.
