"""SVC-20 - Vision extraction of invoice fields. ADVISORY ONLY.

Trust class: llm

Emits structured evidence. Has no database write access and no ERP client in scope.
Every number it reports is re-derived by sentinel.validation before it can affect a decision.

See docs/SERVICE_REGISTRY.md for the full contract.
"""

SERVICE_ID = "SVC-20"
TRUST_CLASS = "llm"
