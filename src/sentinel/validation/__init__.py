"""SVC-30 - Three-way match, tolerance, tax and total recomputation. AUTHORITATIVE.

Trust class: deterministic

All financial mathematics lives here. No LLM call may appear in this module, ever.
Money is Decimal. Price variance is computed against the accepted quantity (spec section 15).

See docs/SERVICE_REGISTRY.md for the full contract.
"""

SERVICE_ID = "SVC-30"
TRUST_CLASS = "deterministic"
