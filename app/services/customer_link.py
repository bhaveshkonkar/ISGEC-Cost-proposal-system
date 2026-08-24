"""Customer master record linkage (recommendation 7.3).

Maps an inbound email sender to a Customer record so proposals accumulate a
clean per-account history. Matching is by exact email address, then by
company-domain; unmatched senders get a new customer record created once and
reused afterwards.
"""
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Customer, Proposal

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com",
    "live.com", "rediffmail.com", "icloud.com", "aol.com", "proton.me",
}

# Common company-name tokens dropped when deriving a display name from a domain
_STOP_TOKENS = {"com", "co", "in", "org", "net", "ltd", "limited", "inc", "llp",
                "pvt", "private", "group"}


def extract_email_address(from_addr: str) -> str:
    m = EMAIL_RE.search(from_addr or "")
    return m.group(0).lower() if m else ""


def _domain_of(addr: str) -> str:
    return addr.split("@")[1] if "@" in addr else ""


def _display_name_from_domain(domain: str) -> str:
    root = domain.split(".")[0] if domain else ""
    if not root:
        return ""
    words = re.split(r"[\-_0-9]+", root)
    words = [w for w in words if w and w.lower() not in _STOP_TOKENS]
    if not words:
        words = [root]
    return " ".join(w.capitalize() for w in words)


def _contact_emails_match(contact_info, addr: str) -> bool:
    if not isinstance(contact_info, dict):
        return False
    emails = contact_info.get("emails") or []
    if isinstance(emails, str):
        emails = [emails]
    return any(str(e).lower() == addr for e in emails)


async def resolve_customer_for_sender(db: AsyncSession, from_addr: str) -> Customer | None:
    """Find or create the Customer record for an email sender."""
    addr = extract_email_address(from_addr)
    if not addr:
        return None

    result = await db.execute(select(Customer))
    for c in result.scalars().all():
        if _contact_emails_match(c.contact_info, addr):
            return c

    domain = _domain_of(addr)
    customer = None
    if domain and domain not in FREEMAIL_DOMAINS:
        result = await db.execute(select(Customer).where(Customer.source == f"email:{domain}"))
        customer = result.scalars().first()
    if customer is None:
        name = _display_name_from_domain(domain) or addr.split("@")[0]
        contact_info = {"emails": [addr]}
        if domain:
            contact_info["domain"] = domain
        customer = Customer(
            name=name,
            source=f"email:{domain}" if domain else "email:direct",
            contact_info=contact_info,
        )
        db.add(customer)
        await db.flush()
    elif not _contact_emails_match(customer.contact_info, addr):
        info = dict(customer.contact_info or {})
        emails = list(info.get("emails") or [])
        if addr not in [str(e).lower() for e in emails]:
            emails.append(addr)
        info["emails"] = emails
        customer.contact_info = info
        await db.flush()
    return customer


async def link_proposal_to_customer(db: AsyncSession, proposal: Proposal, from_addr: str) -> Customer | None:
    """Attach the proposal to the sender's customer record (idempotent)."""
    if proposal.customer_id:
        existing = await db.get(Customer, proposal.customer_id)
        if existing:
            return existing
    customer = await resolve_customer_for_sender(db, from_addr)
    if customer:
        proposal.customer_id = customer.id
        await db.flush()
    return customer


def serialize_customer_link(customer: Customer | None) -> dict | None:
    if not customer:
        return None
    return {
        "id": customer.id,
        "name": customer.name,
        "sector": customer.sector,
        "source": customer.source,
        "contact_info": customer.contact_info,
    }
