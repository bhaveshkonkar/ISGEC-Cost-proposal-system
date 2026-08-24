# ISGEC Proposal System

The ISGEC Proposal System is an internal sales and engineering operations application for turning customer requirements into commercial proposals.

It helps the team:

- Manage products and customers
- Upload product catalogs and knowledge-base documents
- Read RFQs from text, documents, or email
- Match requested equipment against the product catalog
- Generate proposals with net and gross pricing
- Review and send quotation emails
- Create and verify Razorpay payment orders
- Track proposal status from draft through acceptance
- Ask engineering questions through the AI assistant

## Main Workflow

```text
Product Catalog / Customer Data
              |
              v
Customer RFQ (text, document, or email)
              |
              v
Extract requirements with the AI service
              |
              v
Match requested items against the product catalog
              |
              v
Create a proposal with line items and pricing
              |
              v
Review proposal and approve quotation email
              |
              v
Customer receives quotation and optional payment link
              |
              v
Update proposal status and verify payment
```

## Detailed Process

### 1. Load business data

Products can be uploaded from CSV files. Customer records can also be uploaded or created through the application. Product embeddings are stored in Qdrant so descriptions can be matched semantically, not only by exact SKU.

Knowledge-base documents are uploaded separately and can be searched by the AI assistant.

### 2. Receive an RFQ

An RFQ can enter the system in three ways:

- Enter RFQ text manually
- Upload an RFQ document
- Check the configured mailbox for inbound RFQs

The email workflow can classify inbound messages as RFQs, acceptances, rejections, or other messages.

### 3. Extract requirements

The language model extracts requested products, quantities, notes, and other requirements from the RFQ. Document text is parsed before extraction when an attachment is uploaded.

### 4. Match catalog products

Each requested item is matched against the catalog:

- Exact SKU matching is attempted first
- Semantic product search is used when an exact match is not found
- Unmatched products can be marked as available on request or not offered

### 5. Generate the proposal

The system creates a proposal containing:

- Proposal reference number
- Customer RFQ text
- Quoted line items
- Quantity and unit prices
- Net and gross subtotals
- Currency
- Notes and validity date

The proposal can be viewed as a normal document and printed/exported to PDF from the browser.

### 6. Review and send the quotation

Email-generated quotations first appear in the Email Approvals screen when manual approval is required. An operator can:

- Review the requested items and calculated total
- Approve and send the quotation
- Reject the quotation
- Retry failed email processing
- Open the related proposal

Automatic sending can be enabled with `EMAIL_AUTO_SEND=true` when the mailbox is configured.

### 7. Payment

For eligible quotations, the system can create a Razorpay payment order. The payment page supports payment verification and records paid or failed states against the proposal.

### 8. Track business status

Proposal statuses are used to represent progress, including draft, sent, accepted, rejected, and other workflow states. The dashboard displays proposal counts, value summaries, recent proposals, and quick actions.

## Application Areas

| Area | Purpose |
| --- | --- |
| Dashboard | KPIs, proposal pipeline, quoted values, recent proposals, and quick actions |
| Product Catalog | Browse, upload, and delete product records |
| New Proposal | Create a proposal from RFQ text or an uploaded document |
| Proposals | View proposals, customers, statuses, and proposal details |
| Email Approvals | Process inbound RFQs and approve or reject generated quotations |
| Knowledge Base | Upload and search engineering reference documents |
| AI Assistant | Ask questions using the engineering knowledge base |
| Payment Page | Complete and verify Razorpay payments |

## Technology

- FastAPI and Uvicorn
- Jinja2 server-rendered HTML templates
- SQLAlchemy async ORM
- PostgreSQL
- Qdrant vector database
- Ollama embeddings
- Groq-compatible language-model API
- Razorpay payment integration

## Project Structure

```text
app/
  main.py                 FastAPI application and page routes
  config.py               Environment configuration
  models.py               Database models and migrations
  routers/                API endpoints
  services/               Email, document, AI, search, proposal, and payment logic
  templates/              HTML pages and quotation templates
  static/                 CSS and JavaScript assets
uploads/                  Uploaded catalogs and documents
requirements.txt          Python dependencies
.env                      Local environment configuration
```

## Setup

1. Create or activate the virtual environment.

```powershell
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies.

```powershell
pip install -r requirements.txt
```

3. Start the application.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

4. Open the application.

```text
http://127.0.0.1:8001
```

The application requires PostgreSQL for startup. Qdrant is required for semantic product and knowledge-base search. Email and payment features require their corresponding environment variables.

## Important Environment Settings

- `DATABASE_URL`: PostgreSQL connection string
- `QDRANT_URL`: Qdrant server URL
- `GROQ_API_KEY`: Language-model API key
- `OLLAMA_BASE_URL`: Ollama server URL
- `EMAIL_AUTO_SEND`: Enable or disable automatic quotation sending
- `IMAP_USER` and `IMAP_PASSWORD`: Mailbox credentials
- `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`: Razorpay credentials

## Pricing Policy Controls

Automatic sending and manual approvals are gated by pricing policy:

- `POLICY_MIN_CONFIDENCE` (default 0.40): minimum estimate confidence required before a quote may be sent
- `POLICY_MIN_MARGIN_PCT` (default 8): minimum margin on the proposed price
- `POLICY_MAX_DISCOUNT_PCT` (default 10): maximum discount versus the recommended price
- `POLICY_BLOCK_HIGH_RISK` (default true): high-risk estimates always require a justified manual approval, never auto-send
- `POLICY_ENFORCE_AUTO_SEND` (default true): when true, auto-send is blocked unless the policy fully passes
- `HISTORY_STALE_YEARS` (default 12): historical projects older than this are excluded from estimates

Every sent quote records an immutable version entry (estimate reference, predicted cost,
recommended price, final price, deviation percentage, risk, confidence, approver identity,
policy result, justification) visible at `/api/proposals/{id}/quote-history`.

Voice calling and Bolna integration have been removed from the project.

## Useful API Groups

- `/api/proposals/*`: proposals, customers, dashboard statistics, statuses, and quote version history
- `/api/products/*`: product catalog operations
- `/api/emails/*`: mailbox processing and quotation approvals
- `/api/kb/*`: knowledge-base upload and search
- `/api/chat/*`: engineering assistant and chat history
- `/api/payments/*`: payment verification and status
- `/api/health`: application health check
