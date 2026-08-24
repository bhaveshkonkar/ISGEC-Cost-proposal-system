from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, Integer, String, Text, Numeric, Date, DateTime, ForeignKey, JSON, func, text
from app.config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "isgec_products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(100), unique=True, nullable=False)
    name = Column(String(500), nullable=False)
    description = Column(Text, default="")
    price_net = Column(Numeric(14, 2), default=0)
    price_gross = Column(Numeric(14, 2), default=0)
    vat_rate = Column(Numeric(5, 2), default=18)
    currency = Column(String(10), default="INR")
    category = Column(String(200), default="")
    specs = Column(JSON, default=dict)
    qdrant_point_id = Column(String(100), default="")
    created_at = Column(DateTime, server_default=func.now())


class Customer(Base):
    __tablename__ = "isgec_customers"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(500), nullable=False)
    sector = Column(String(200), default="")
    source = Column(String(200), default="")
    contact_info = Column(JSON, default=dict)
    created_at = Column(DateTime, server_default=func.now())


class Proposal(Base):
    __tablename__ = "isgec_proposals"
    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_number = Column(String(50), unique=True)
    customer_id = Column(Integer, ForeignKey("isgec_customers.id"), nullable=True)
    rfq_text = Column(Text, nullable=False)
    rfq_source = Column(String(20), default="text")
    status = Column(String(20), default="draft")
    total_net = Column(Numeric(14, 2), default=0)
    total_gross = Column(Numeric(14, 2), default=0)
    currency = Column(String(10), default="INR")
    notes = Column(Text, default="")
    valid_until = Column(Date, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class ProposalItem(Base):
    __tablename__ = "isgec_proposal_items"
    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(Integer, ForeignKey("isgec_proposals.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("isgec_products.id"), nullable=True)
    sku = Column(String(100), default="")
    description = Column(Text, default="")
    quantity = Column(Integer, default=1)
    unit_price_net = Column(Numeric(14, 2), default=0)
    unit_price_gross = Column(Numeric(14, 2), default=0)
    subtotal_net = Column(Numeric(14, 2), default=0)
    subtotal_gross = Column(Numeric(14, 2), default=0)
    notes = Column(Text, default="")
    item_status = Column(String(20), default="quoted")


class ChatMessage(Base):
    __tablename__ = "isgec_chat_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(100), default="default")
    role = Column(String(20), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class EmailMessage(Base):
    __tablename__ = "isgec_emails"
    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String(500), unique=True)
    uid = Column(Integer, default=0)
    from_addr = Column(String(500), default="")
    subject = Column(String(1000), default="")
    body_text = Column(Text, default="")
    attachment_names = Column(JSON, default=list)
    status = Column(String(20), default="new")
    error_message = Column(Text, default="")
    proposal_id = Column(Integer, ForeignKey("isgec_proposals.id"), nullable=True)
    sent_by = Column(String(20), default="")
    received_at = Column(DateTime, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    quote_message_id = Column(String(500), default="")
    email_type = Column(String(30), default="")
    linked_quote_id = Column(Integer, ForeignKey("isgec_emails.id"), nullable=True)
    triage_note = Column(Text, default="")
    reply_match_method = Column(String(30), default="")


class Payment(Base):
    __tablename__ = "isgec_payments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(Integer, ForeignKey("isgec_proposals.id"), nullable=False)
    email_id = Column(Integer, ForeignKey("isgec_emails.id"), nullable=True)
    razorpay_order_id = Column(String(100), unique=True)
    razorpay_payment_id = Column(String(100), default="")
    razorpay_signature = Column(String(500), default="")
    amount = Column(Numeric(14, 2), default=0)
    currency = Column(String(10), default="INR")
    status = Column(String(20), default="created")
    error_message = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())
    paid_at = Column(DateTime, nullable=True)


class ProjectCost(Base):
    __tablename__ = "isgec_project_costs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    project_name = Column(String(500), nullable=False)
    customer_name = Column(String(500), default="")
    sector = Column(String(200), default="")
    equipment_category = Column(String(200), default="")
    description = Column(Text, default="")
    year = Column(Integer, nullable=True)
    quantity = Column(Integer, default=1)
    weight_kg = Column(Numeric(14, 2), nullable=True)
    complexity = Column(String(20), default="medium")
    material_cost = Column(Numeric(14, 2), default=0)
    labour_cost = Column(Numeric(14, 2), default=0)
    engineering_cost = Column(Numeric(14, 2), default=0)
    overhead_cost = Column(Numeric(14, 2), default=0)
    freight_cost = Column(Numeric(14, 2), default=0)
    total_cost = Column(Numeric(14, 2), default=0)
    quoted_value = Column(Numeric(14, 2), default=0)
    final_value = Column(Numeric(14, 2), default=0)
    currency = Column(String(10), default="INR")
    margin_pct = Column(Numeric(6, 2), default=0)
    outcome = Column(String(20), default="won")
    lost_reason = Column(String(500), default="")
    qdrant_point_id = Column(String(100), default="")
    proposal_id = Column(Integer, nullable=True)  # set when the row was created from a live quote
    created_at = Column(DateTime, server_default=func.now())


class ProposalCostEstimate(Base):
    __tablename__ = "isgec_proposal_cost_estimates"
    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(Integer, ForeignKey("isgec_proposals.id"), nullable=True)
    currency = Column(String(10), default="INR")
    input_snapshot = Column(JSON, default=dict)
    breakdown = Column(JSON, default=dict)
    predicted_cost = Column(Numeric(14, 2), default=0)
    similar_projects = Column(JSON, default=list)
    recommended_margin_pct = Column(Numeric(6, 2), default=0)
    margin_floor_pct = Column(Numeric(6, 2), default=0)
    margin_ceiling_pct = Column(Numeric(6, 2), default=0)
    risk_level = Column(String(20), default="medium")
    risk_score = Column(Numeric(5, 1), default=0)
    risk_factors = Column(JSON, default=list)
    contingency_pct = Column(Numeric(6, 2), default=0)
    contingency_amount = Column(Numeric(14, 2), default=0)
    recommended_price_net = Column(Numeric(14, 2), default=0)
    price_min = Column(Numeric(14, 2), default=0)
    price_max = Column(Numeric(14, 2), default=0)
    confidence = Column(Numeric(4, 3), default=0)
    drivers = Column(JSON, default=list)
    llm_narrative = Column(Text, default="")
    model_version = Column(String(50), default="costing-v1")
    created_at = Column(DateTime, server_default=func.now())


class QuoteVersion(Base):
    """Immutable audit record of every quote price state (recommendations 7.2 / 7.4)."""
    __tablename__ = "isgec_quote_versions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    proposal_id = Column(Integer, ForeignKey("isgec_proposals.id"), nullable=False)
    email_id = Column(Integer, ForeignKey("isgec_emails.id"), nullable=True)
    version_number = Column(Integer, default=1)
    estimate_id = Column(Integer, ForeignKey("isgec_proposal_cost_estimates.id"), nullable=True)
    change_type = Column(String(30), default="initial")  # initial | price_override | resend
    predicted_cost = Column(Numeric(14, 2), default=0)
    recommended_price_net = Column(Numeric(14, 2), default=0)
    final_price_net = Column(Numeric(14, 2), default=0)
    deviation_pct = Column(Numeric(8, 2), default=0)  # final vs recommended
    margin_pct = Column(Numeric(6, 2), default=0)
    discount_pct = Column(Numeric(6, 2), default=0)
    risk_score = Column(Numeric(5, 1), default=0)
    risk_level = Column(String(20), default="")
    confidence = Column(Numeric(4, 3), default=0)
    currency = Column(String(10), default="INR")
    policy_result = Column(JSON, default=dict)
    approved_by = Column(String(200), default="")
    justification = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())


LIGHTWEIGHT_MIGRATIONS = [
    "ALTER TABLE isgec_proposal_items ADD COLUMN IF NOT EXISTS item_status VARCHAR(20) DEFAULT 'quoted'",
    "ALTER TABLE isgec_emails ADD COLUMN IF NOT EXISTS quote_message_id VARCHAR(500) DEFAULT ''",
    "ALTER TABLE isgec_emails DROP COLUMN IF EXISTS opened_at",
    "ALTER TABLE isgec_emails DROP COLUMN IF EXISTS call_status",
    "ALTER TABLE isgec_emails DROP COLUMN IF EXISTS call_execution_id",
    "ALTER TABLE isgec_emails DROP COLUMN IF EXISTS call_feedback",
    "ALTER TABLE isgec_emails DROP COLUMN IF EXISTS feedback_sent_at",
    "ALTER TABLE isgec_emails ADD COLUMN IF NOT EXISTS email_type VARCHAR(30) DEFAULT ''",
    "ALTER TABLE isgec_emails ADD COLUMN IF NOT EXISTS linked_quote_id INTEGER",
    "ALTER TABLE isgec_emails ADD COLUMN IF NOT EXISTS triage_note TEXT DEFAULT ''",
    "ALTER TABLE isgec_emails ADD COLUMN IF NOT EXISTS reply_match_method VARCHAR(30) DEFAULT ''",
    "ALTER TABLE isgec_project_costs ADD COLUMN IF NOT EXISTS proposal_id INTEGER",
]


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in LIGHTWEIGHT_MIGRATIONS:
            await conn.execute(text(stmt))


async def get_session():
    async with async_session() as session:
        yield session
