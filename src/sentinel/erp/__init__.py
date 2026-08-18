"""SVC-90 - Authorized, idempotent ERP transaction execution.

Trust class: control

The only module that may move money, and only after authorization and idempotency checks pass.

See docs/SERVICE_REGISTRY.md for the full contract.
"""

SERVICE_ID = "SVC-90"
TRUST_CLASS = "control"
