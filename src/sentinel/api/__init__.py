"""SVC-05 - HTTP transport and the composition root.

Trust class: composition

The only module permitted to depend on every other. It wires objects together and
translates HTTP to domain calls; it never calculates, evaluates policy, or authorizes.
See ADR-0008.

See docs/SERVICE_REGISTRY.md for the full contract.
"""

SERVICE_ID = "SVC-05"
TRUST_CLASS = "composition"
