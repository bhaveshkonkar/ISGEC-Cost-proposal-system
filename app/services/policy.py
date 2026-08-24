"""Pricing policy enforcement (recommendation 7.1).

Every quote that is about to leave the system - auto-send or manual approval -
must pass evaluate_quote_policy(). A failing policy blocks sending unless an
authorized approver explicitly overrides with a justification.
"""
from app.config import (
    POLICY_MIN_CONFIDENCE, POLICY_MIN_MARGIN_PCT, POLICY_MAX_DISCOUNT_PCT,
    POLICY_BLOCK_HIGH_RISK, POLICY_ENFORCE_AUTO_SEND,
)


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _estimate_fields(estimate) -> dict:
    """Accept either the run_cost_estimate() result dict or a ProposalCostEstimate row."""
    if estimate is None:
        return {}
    if isinstance(estimate, dict):
        margin = estimate.get("margin") or {}
        risk = estimate.get("risk") or {}
        return {
            "confidence": estimate.get("confidence"),
            "margin_pct": margin.get("recommended_margin_pct", margin.get("margin_pct"))
            if isinstance(margin, dict) else None,
            "risk_level": str(risk.get("risk_level", "")) if isinstance(risk, dict) else "",
            "risk_score": _f(risk.get("risk_score")) if isinstance(risk, dict) else 0.0,
        }
    return {
        "confidence": getattr(estimate, "confidence", None),
        "margin_pct": getattr(estimate, "recommended_margin_pct", None),
        "risk_level": getattr(estimate, "risk_level", "") or "",
        "risk_score": _f(getattr(estimate, "risk_score", 0)),
    }


def evaluate_quote_policy(
    estimate,
    proposed_price_net: float = 0.0,
    predicted_cost: float = 0.0,
    discount_pct: float = 0.0,
) -> dict:
    """Return {allowed, requires_review, violations, checks}.

    - allowed: quote may be sent without human override
    - requires_review: policy passed but weak signals mean review is advised
      (high risk); auto-send must stop, manual approval may proceed
    - violations: hard failures; auto-send blocked, manual approval needs
      justification + force
    """
    checks = []
    violations = []
    warnings = []

    fields = _estimate_fields(estimate)
    confidence = fields.get("confidence")
    margin_pct = fields.get("margin_pct")
    risk_level = fields.get("risk_level", "")
    risk_score = fields.get("risk_score", 0.0)

    # Check 1: minimum estimate confidence
    if confidence is None:
        violations.append("No cost estimate found for this proposal - run an estimate before quoting")
        checks.append({"check": "min_confidence", "passed": False, "value": None,
                       "threshold": POLICY_MIN_CONFIDENCE})
    else:
        conf = _f(confidence)
        ok = conf >= POLICY_MIN_CONFIDENCE
        checks.append({"check": "min_confidence", "passed": ok, "value": round(conf, 3),
                       "threshold": POLICY_MIN_CONFIDENCE})
        if not ok:
            violations.append(
                f"Estimate confidence {conf:.0%} below required {POLICY_MIN_CONFIDENCE:.0%}"
            )

    # Check 2: minimum margin on the proposed price
    if proposed_price_net > 0 and predicted_cost > 0:
        implied_margin = (proposed_price_net - predicted_cost) / proposed_price_net * 100
        ok = implied_margin >= POLICY_MIN_MARGIN_PCT
        checks.append({"check": "min_margin", "passed": ok,
                       "value": round(implied_margin, 2), "threshold": POLICY_MIN_MARGIN_PCT})
        if not ok:
            violations.append(
                f"Implied margin {implied_margin:.1f}% below policy floor {POLICY_MIN_MARGIN_PCT:.1f}%"
            )
    elif margin_pct is not None:
        ok = _f(margin_pct) >= POLICY_MIN_MARGIN_PCT
        checks.append({"check": "min_margin", "passed": ok,
                       "value": round(_f(margin_pct), 2), "threshold": POLICY_MIN_MARGIN_PCT})
        if not ok:
            violations.append(
                f"Recommended margin {_f(margin_pct):.1f}% below policy floor {POLICY_MIN_MARGIN_PCT:.1f}%"
            )

    # Check 3: maximum discount
    disc = _f(discount_pct)
    ok = disc <= POLICY_MAX_DISCOUNT_PCT
    checks.append({"check": "max_discount", "passed": ok, "value": disc,
                   "threshold": POLICY_MAX_DISCOUNT_PCT})
    if not ok:
        violations.append(
            f"Discount {disc:.1f}% exceeds maximum allowed {POLICY_MAX_DISCOUNT_PCT:.1f}%"
        )

    # Check 4: high-risk quotes always need a human decision (never auto-sent)
    requires_review = False
    if POLICY_BLOCK_HIGH_RISK and risk_level == "high":
        requires_review = True
        checks.append({"check": "risk_review", "passed": False, "value": risk_level,
                       "threshold": "medium"})
        if not violations:
            warnings.append(
                f"High-risk project (score {risk_score:.0f}) - manual approval required"
            )
    else:
        checks.append({"check": "risk_review", "passed": True, "value": risk_level or "n/a",
                       "threshold": "medium"})

    return {
        "allowed": not violations and not requires_review,
        "requires_review": requires_review,
        "violations": violations,
        "warnings": warnings,
        "checks": checks,
    }


def auto_send_allowed(policy_result: dict) -> bool:
    """Auto-send only when policy fully passes and enforcement is enabled."""
    if not POLICY_ENFORCE_AUTO_SEND:
        return True
    return bool(policy_result.get("allowed"))
