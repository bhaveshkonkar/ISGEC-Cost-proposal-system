# Email Automation System Overview

This document explains how the email-based quotation system works in this application.

## 1. Purpose

The system reads customer emails from an IMAP mailbox, identifies whether the email is a request for quotation (RFQ), creates a proposal, matches items to the catalog, and then sends a quotation back by email. It also handles customer acceptance/rejection replies and allows manual review from the dashboard.

The implementation is spread across the following files:

- `app/services/email_client.py`
- `app/services/email_pipeline.py`
- `app/routers/email.py`
- `app/models.py`
- `app/templates/quote_email.html`

---

## 2. High-Level Flow

The workflow is:

1. Fetch unread emails from the mailbox.
2. Save email attachments locally.
3. Read and clean email body text.
4. Detect email type:
   - RFQ
   - acceptance
   - rejection
   - irrelevant/other
5. Store the email in the `EmailMessage` table.
6. For RFQ emails:
   - parse product requirements
   - match entries to product catalog
   - generate a proposal and proposal items
   - either auto-send or mark for approval
7. For customer replies:
   - match to the original quote thread
   - update proposal status to accepted/rejected
8. Admin can approve, reject, retry, or override price from the dashboard.

---

## 3. Email Retrieval from the Mailbox

The inbound system begins in `app/services/email_client.py`.

### `fetch_unread(limit=10)`

This function:

- checks whether IMAP credentials are configured
- connects to the configured IMAP mailbox
- fetches unread messages (`seen=False`)
- marks them as seen immediately
- ignores emails from the same mailbox user

For each email, it extracts:

- sender name and address
- subject
- plain text or HTML body
- attachments
- received date
- `In-Reply-To`
- `References`
- unique message ID and IMAP UID

It also saves supported attachments into the upload directory and stores their saved file paths for later text extraction.

Supported attachment extensions include:

- PDF
- DOCX / DOC
- XLSX / XLS
- CSV
- TXT
- PNG / JPG / JPEG

---

## 4. Cleaning the Email Body

The system strips out noisy email content before parsing requirements.

It removes:

- quoted original-message blocks
- signature separators like `--`
- HTML tags
- line breaks that are not useful for parsing

This cleaning helps the requirement extractor focus on the actual customer request instead of forwarded text or signatures.

---

## 5. Email Record Persistence

The database model is defined in `app/models.py`.

The primary table for incoming mail is `EmailMessage`.

It stores:

- a unique `message_id`
- IMAP `uid`
- sender address
- subject
- message body
- attachment metadata
- status
- linked proposal ID
- reply metadata
- triage classification
- timestamps

The system uses this table as the master record for the customer conversation lifecycle.

---

## 6. The Polling Cycle

The email processing loop is defined in `app/services/email_pipeline.py`.

### `run_poll_cycle()`

This function:

- checks whether email integration is configured
- prevents overlapping poll runs with an async lock
- triggers the main processing cycle

### `_run_poll_cycle_locked()`

This loop does the following:

- calls `fetch_unread()`
- processes each email with `process_email()`
- counts processed, skipped, and failed items
- returns a summary dictionary

There is also a stale-record recovery step that marks older `processing` or `new` emails as failed if they remain stuck too long.

---

## 7. `process_email()` Core Logic

This is the central function that decides what each incoming email means.

### Step 1: Deduplicate

Before processing, the system checks whether the same `message_id` already exists in the database.

If yes, it skips duplicate processing.

### Step 2: Insert initial DB row

A new `EmailMessage` row is created with status `processing`.

### Step 3: Collect all text

The system gathers:

- the email body text
- text extracted from attachments

This combined text becomes the RFQ input for the requirement extractor.

### Step 4: Extract requirements

The code calls `extract_requirements(full_text)`.

This extracts a structured result such as:

- `email_type`
- `summary`
- `is_rfq`
- `items`
- notes or issue data

### Step 5: Branch by email type

#### A. Irrelevant email

If the email is neither RFQ nor acceptance/rejection, it is marked as `ignored`.

#### B. Acceptance or rejection

If the email is a customer response to a previously sent quotation, the system tries to match it to the original quote using:

- `In-Reply-To`
- `References`
- proposal number in subject

If a match is found:

- the linked proposal is marked `accepted` or `rejected`
- the email record becomes `replied`

If no match is found:

- the record is ignored as unlinked

#### C. RFQ email

If the message is a request for quotation, the system continues with product matching and proposal creation.

---

## 8. Product Matching and Quote Item Construction

After the system confirms an RFQ, it builds a list of product-line entries from the extracted requirements.

### Duplicate handling

The app merges near-duplicate item descriptions to avoid double-counting the same product when the customer writes similar descriptions multiple times.

### Product matching

For each requirement item:

- first try exact SKU match
- if no exact match, use embedding-based similarity search against the catalog
- only keep matches above the set threshold

If a catalog product is found:

- SKU is attached
- unit price and VAT are loaded
- subtotal and gross totals are calculated
- product status is set as `quoted`

If not found:

- the item is marked as `on_request` or `not_offered`
- a note is recorded to explain the issue

This is configurable based on how the classifier categorizes unmatched products.

---

## 9. Proposal Creation

Once all line items are classified, a `Proposal` is created.

The proposal includes:

- generated proposal number
- original RFQ text
- source type (`email`)
- total net and gross values
- validity date
- currency

The related `ProposalItem` rows are then created for each item, including:

- product association
- quantity
- unit price
- subtotal
- notes
- `item_status`

The `EmailMessage` record is then linked to the proposal through `proposal_id`.

---

## 10. Auto-Send vs Manual Review

After proposal creation, the system checks whether the proposal is ready to send.

### Auto-send path

If all items are quoted and both of the following are true:

- `EMAIL_AUTO_SEND` is enabled
- email configuration is active

then it immediately calls `send_quote_for_email()`.

### Manual approval path

If:

- some items are `on_request` or `not_offered`, or
- auto-send is disabled,

then the system does not send automatically.

Instead, it sets the email status to `quoted` and adds a reason message such as:

- pending approval
- some items are on request
- some items are not offered
- auto-send disabled

This creates a review queue in the dashboard.

---

## 11. Sending the Quote Email

The send action is handled by `send_quote_for_email()` in `app/services/email_pipeline.py`.

The function:

- loads the linked proposal
- loads its proposal items
- creates the quote context
- optionally creates a payment link if needed
- renders HTML with the quote email template
- sends the message using SMTP
- marks the email status as `replied`
- updates the proposal status to `sent`

### Email building

The actual mail send function is in `app/services/email_client.py`:

- creates a MIME email object
- sets From, To, Subject
- generates a `Message-ID`
- adds `In-Reply-To` and `References` if replying to a previous email
- sends with SMTP using TLS

This ensures the customer receives a threaded, professional quotation email.

---

## 12. Thread Matching for Replies

A key part of the system is matching customer replies back to the correct quote.

The function `_match_quoted_thread()` does this by:

1. checking `In-Reply-To` and `References` headers against stored `quote_message_id`
2. searching the subject for the proposal number pattern
3. locating the last reply record tied to that proposal

This is important because the customer may reply to a quote with acceptance or rejection, and the system must know which quotation they are responding to.

---

## 13. Dashboard Operations

The user-facing API is in `app/routers/email.py`.

### Main actions

- `GET /api/emails`
  - list emails by tab: pending, sent, issues

- `POST /api/emails/check-now`
  - manual trigger for the polling cycle

- `POST /api/emails/{email_id}/approve`
  - approve and send the quote

- `POST /api/emails/{email_id}/reject`
  - reject the email/quote

- `POST /api/emails/{email_id}/approve-with-price`
  - allow editing total price and rescaling line items

- `POST /api/emails/{email_id}/retry`
  - reprocess a failed or partially processed email

These endpoints drive the admin dashboard and create a human review layer before final message sending.

---

## 14. Status Lifecycle Summary

Main email status values include:

- `new`
- `processing`
- `quoted`
- `needs_review`
- `replied`
- `rejected`
- `ignored`
- `failed`

Proposal status values include:

- `draft`
- `sent`
- `accepted`
- `rejected`

The same email may progress through multiple steps:

```
new -> processing -> quoted -> replied
```

or:

```
new -> processing -> ignored
```

or:

```
new -> processing -> quoted -> approved -> replied
```

---

## 15. End-to-End Example

A typical customer process looks like this:

1. Customer sends an email asking for a quotation.
2. Mailbox scanner fetches the message.
3. The app reads the body and attachments.
4. The requirement extractor identifies product items.
5. Products are matched against the catalog.
6. A proposal is created.
7. The app either auto-sends the quotation or sets it to pending approval.
8. The customer receives the quote email.
9. Customer replies with acceptance or rejection.
10. The system matches reply to the original quote.
11. Proposal status updates to accepted/rejected.
12. Dashboard shows the final state.

---

## 16. Overall Design Insight

This system is effectively a lightweight email-driven sales automation workflow:

- IMAP handles inbound messages
- SQL stores the state of each conversation
- NLP extracts requirements
- catalog matching chooses the correct products
- the system proposes pricing and responds by email
- human review is still built in for approval and pricing overrides

It is designed to be practical for handling quotation workflows without entirely replacing human decision-making.
