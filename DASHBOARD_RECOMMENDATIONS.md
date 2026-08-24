# ISGEC Proposal System: Dashboard Recommendations

**Review date:** 2026-08-21

## Overall Assessment

The current dashboard has a strong visual foundation and should not be rebuilt from scratch. It already provides a clear navigation structure, KPI cards, proposal charts, recent proposals, and quick actions.

However, it currently behaves more like a prototype analytics page than a production sales-operations dashboard. Several displayed values can be misleading, and the page does not surface the email, payment, and exception workflows that are central to the application.

## What Should Stay

- Existing visual style and branding
- KPI card layout
- Proposal pipeline chart
- Recent proposals section
- Quick workflow actions
- Product and customer summary metrics
- Existing navigation structure

## Priority Improvements

### 1. Make financial metrics unambiguous

The dashboard should separate commercial values by meaning:

- Draft quotation value
- Sent quotation value
- Accepted quotation value
- Paid value
- Outstanding value
- Rejected quotation value

The current `Total Quoted Value` combines proposal statuses, including rejected proposals. This can overstate the active business pipeline.

### 2. Add email workflow metrics

Email automation is a major product workflow but is absent from the dashboard. Add:

- Emails pending approval
- Failed email processing
- Emails requiring review
- Quotes sent today
- Ignored/triage emails
- Last inbox check time

### 3. Add payment metrics

Add a payment summary showing:

- Payment links created
- Payments awaiting completion
- Paid quotations
- Failed payments
- Outstanding payment value

A quotation should not appear simply as successful because a payment link exists. The dashboard should distinguish quoted, accepted, paid, and partially paid states.

### 4. Add a “Needs Attention” panel

Place this near the top of the page. It should show actionable exceptions such as:

- Pending quotation approvals
- Failed email processing
- Failed payment attempts
- RFQs with unmatched products
- Quotations expiring within seven days
- Ignored emails awaiting triage

Each row should link directly to the relevant workflow.

### 5. Fix currency handling

The dashboard currently presents quoted values as INR. If the system supports multiple currencies, totals must be grouped by currency. Otherwise, configure the product explicitly as INR-only and state that consistently.

### 6. Make dashboard failures visible

If the statistics API fails, the page currently leaves KPI values as `-` and only logs the error to the browser console.

Show an inline error state with:

- A clear message
- The affected section
- A retry action
- The last successful refresh time

### 7. Standardize financial calculations

The dashboard uses gross values for the quoted-value KPI, while recent proposals display net values. Choose one consistent display basis or label each value clearly as Net or Gross.

### 8. Add date filters

Management users should be able to view:

- Today
- This week
- This month
- This financial year
- Custom date range

The current “All Time” view is useful for totals but insufficient for operational reporting.

## Recommended Layout

```text
Header: Dashboard Overview | Date range | Refresh

KPI row:
Pending Approval | Accepted Value | Paid Value | Outstanding Value | Failed Items

Needs Attention:
Pending approvals | Failed emails | Failed payments | Expiring quotations

Pipeline:
Draft -> Sent -> Accepted -> Paid

Financial summary:
Quoted vs accepted vs paid vs outstanding

Recent activity:
Latest proposals, email approvals, payments, and failures

Quick actions:
Create proposal | Check inbox | Upload catalog | Open AI assistant
```

## Recommended Labels

Avoid ambiguous labels such as `Total Sales` and `Total Quoted Value`. Prefer:

- `Paid Sales Value`
- `Active Pipeline Value`
- `Accepted but Unpaid`
- `Pending Approval Value`
- `Failed Payment Value`

## Business Rules the Dashboard Should Follow

- Rejected proposals must not count as active pipeline value.
- A payment link must not count as a paid sale.
- A capped or partial payment must not mark a quotation fully paid.
- Failed or ignored emails must remain visible to operators.
- All monetary totals must specify currency and net/gross basis.
- Dashboard totals should use the same filters as the underlying list views.

## Implementation Priority

### Phase 1: Correctness

1. Separate quoted, accepted, paid, and outstanding values.
2. Fix currency and net/gross labeling.
3. Add visible API error and retry states.
4. Ensure dashboard counts match filtered list totals.

### Phase 2: Operations

1. Add pending approvals and failed-email metrics.
2. Add payment status metrics.
3. Add the Needs Attention panel.
4. Add last inbox-check and last-refresh timestamps.

### Phase 3: Management Reporting

1. Add date-range filters.
2. Add conversion rates from sent to accepted and accepted to paid.
3. Add trend comparisons against the previous period.
4. Add exportable summary reports.

## Final Recommendation

Keep the current dashboard design and visual language. Improve its data model and operational content rather than replacing the page. It is suitable as a prototype, but it should receive the Phase 1 correctness changes before being presented as a production management dashboard.
