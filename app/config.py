import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://solivio:solivio@127.0.0.1:5432/solivio")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "products")
QDRANT_KB_COLLECTION = os.getenv("QDRANT_KB_COLLECTION", "knowledge_base")
QDRANT_PROJECT_COLLECTION = os.getenv("QDRANT_PROJECT_COLLECTION", "project_costs")

# Cost intelligence defaults (fractions, not percent)
DEFAULT_TARGET_MARGIN = float(os.getenv("DEFAULT_TARGET_MARGIN", "0.12"))
MIN_MARGIN_FLOOR = float(os.getenv("MIN_MARGIN_FLOOR", "0.05"))
MAX_MARGIN_CEILING = float(os.getenv("MAX_MARGIN_CEILING", "0.35"))
CONTINGENCY_LOW_PCT = float(os.getenv("CONTINGENCY_LOW_PCT", "5"))
CONTINGENCY_MEDIUM_PCT = float(os.getenv("CONTINGENCY_MEDIUM_PCT", "10"))
CONTINGENCY_HIGH_PCT = float(os.getenv("CONTINGENCY_HIGH_PCT", "15"))
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "INR")
EMBEDDING_DIM = 768

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.getenv("IMAP_USER", "")
IMAP_PASSWORD = os.getenv("IMAP_PASSWORD", "")
MAIL_FOLDER = os.getenv("MAIL_FOLDER", "INBOX")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_POLL_INTERVAL = int(os.getenv("EMAIL_POLL_INTERVAL", "300"))
EMAIL_AUTO_SEND = os.getenv("EMAIL_AUTO_SEND", "false").lower() in ("true", "1", "yes")
SENDER_NAME = os.getenv("SENDER_NAME", "ISGEC Sales Team")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")

# Pricing policy gates (recommendation 7.1). Auto-send and approvals must pass these.
POLICY_MIN_CONFIDENCE = float(os.getenv("POLICY_MIN_CONFIDENCE", "0.40"))
POLICY_MIN_MARGIN_PCT = float(os.getenv("POLICY_MIN_MARGIN_PCT", "8.0"))
POLICY_MAX_DISCOUNT_PCT = float(os.getenv("POLICY_MAX_DISCOUNT_PCT", "10.0"))
POLICY_BLOCK_HIGH_RISK = os.getenv("POLICY_BLOCK_HIGH_RISK", "true").lower() in ("true", "1", "yes")
POLICY_ENFORCE_AUTO_SEND = os.getenv("POLICY_ENFORCE_AUTO_SEND", "true").lower() in ("true", "1", "yes")

# Historical data quality (gap 2): rows older than this many years are excluded from estimates.
HISTORY_STALE_YEARS = int(os.getenv("HISTORY_STALE_YEARS", "12"))

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
# Razorpay test mode rejects orders above Rs 5,00,000. Quotes larger than this
# are capped to the configured amount for demo checkout purposes.
RAZORPAY_MAX_AMOUNT = int(os.getenv("RAZORPAY_MAX_AMOUNT", "500000"))


def razorpay_configured() -> bool:
    return bool(
        RAZORPAY_KEY_ID.startswith("rzp_test_") or RAZORPAY_KEY_ID.startswith("rzp_live_")
    ) and bool(RAZORPAY_KEY_SECRET)
