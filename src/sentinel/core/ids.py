"""Identifiers: correlation IDs, document hashes, and idempotency keys.

Spec §12 requires correlation IDs spanning ingestion, LangGraph state, database operations,
and ERP execution, and §18 requires that every financial action be reproducible from the
audit trail. Both properties live or die on identifiers, so they get a module.

Three design choices worth stating:

**Typed, not stringly-typed.** ``InvoiceId`` and ``VendorId`` are distinct types even though
both are strings underneath. Passing a vendor id where an invoice id belongs is a real bug
class in AP systems, and one the type checker can catch for free.

**Time-ordered.** IDs embed a UUIDv7 (RFC 9562): a 48-bit millisecond timestamp followed by
random bits. Two consequences that matter here -- database index locality stays good as the
invoice table grows, and lexical sort order equals creation order, so an audit trail reads
chronologically without a join.

**Idempotency keys are derived, never generated.** A random key would make every retry look
like a new payment. See :func:`idempotency_key`.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, ClassVar, Final, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

__all__ = [
    "CorrelationId",
    "DocumentHash",
    "IdempotencyKey",
    "InvoiceId",
    "PolicyVersionId",
    "VendorId",
    "idempotency_key",
    "uuid7",
]

_UUID7_VARIANT: Final = 0b10


def uuid7() -> str:
    """A time-ordered UUIDv7 as a canonical hyphenated string (RFC 9562).

    Layout: 48 bits of Unix milliseconds, 4 version bits, 12 random, 2 variant bits,
    62 random. Sorting these lexically sorts them chronologically.
    """
    timestamp_ms = time.time_ns() // 1_000_000
    entropy = int.from_bytes(os.urandom(10), "big")  # 80 bits; 74 are used

    rand_a = (entropy >> 62) & 0xFFF  # 12 bits, sits below the version nibble
    rand_b = entropy & 0x3FFF_FFFF_FFFF_FFFF  # 62 bits, sits below the variant

    value = (timestamp_ms & 0xFFFF_FFFF_FFFF) << 80  # bits 127..80
    value |= 0x7 << 76  # bits  79..76: version 7
    value |= rand_a << 64  # bits  75..64
    value |= _UUID7_VARIANT << 62  # bits  63..62: RFC 9562 variant
    value |= rand_b  # bits  61..0

    hexed = f"{value:032x}"
    return f"{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:]}"


class _PrefixedId(str):
    """Base for human-legible, type-distinct identifiers.

    Rendered as ``<prefix>_<uuid7>``. The prefix costs a few bytes and repays them every
    time an identifier shows up in a log line, a support ticket, or an ERP reference field
    and someone has to work out what it points at.
    """

    __slots__ = ()

    PREFIX: ClassVar[str] = ""
    _LENGTH: ClassVar[int] = 36  # canonical UUID string length

    @classmethod
    def new(cls) -> Self:
        """Mint a fresh identifier."""
        return cls(f"{cls.PREFIX}_{uuid7()}")

    @classmethod
    def parse(cls, value: str) -> Self:
        """Validate an existing identifier, raising ``ValueError`` if it is malformed."""
        expected = f"{cls.PREFIX}_"
        if not value.startswith(expected):
            raise ValueError(
                f"{cls.__name__} must start with {expected!r}, got {value!r}. "
                "A mismatched prefix usually means an identifier was passed to the wrong "
                "parameter -- check the call site rather than relaxing this check."
            )
        body = value[len(expected) :]
        if len(body) != cls._LENGTH or body.count("-") != 4:
            raise ValueError(f"{cls.__name__} has a malformed body: {value!r}")
        return cls(value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str.__repr__(self)})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls.parse,
            core_schema.str_schema(),
            serialization=core_schema.to_string_ser_schema(),
        )


class CorrelationId(_PrefixedId):
    """Threads one invoice's journey through every stage, store, and external call.

    Minted once at ingestion and never regenerated. Spec §12: it must appear on ingestion
    records, LangGraph state, database rows, ERP calls, and every audit event.
    """

    __slots__ = ()
    PREFIX: ClassVar[str] = "cor"


class InvoiceId(_PrefixedId):
    """Sentinel's own identifier for an invoice.

    Distinct from the supplier's ``invoice_number``, which is not unique across vendors and
    is exactly the field duplicate detection has to reason about.
    """

    __slots__ = ()
    PREFIX: ClassVar[str] = "inv"


class VendorId(_PrefixedId):
    __slots__ = ()
    PREFIX: ClassVar[str] = "ven"


class PolicyVersionId(_PrefixedId):
    """Identifies one immutable version of the policy set.

    Spec §9 requires that every decision record which policy version produced it, so a
    historical decision can be replayed exactly.
    """

    __slots__ = ()
    PREFIX: ClassVar[str] = "pol"


class DocumentHash(str):
    """SHA-256 of the original document bytes, lowercase hex.

    The basis of exact duplicate detection (spec §6) and of the guarantee that a stored
    document is the one that was ingested.
    """

    __slots__ = ()
    _HEX_LENGTH: ClassVar[int] = 64

    @classmethod
    def of(cls, data: bytes) -> Self:
        """Hash raw document bytes."""
        return cls(hashlib.sha256(data).hexdigest())

    @classmethod
    def parse(cls, value: str) -> Self:
        normalized = value.lower()
        if len(normalized) != cls._HEX_LENGTH:
            raise ValueError(
                f"document hash must be {cls._HEX_LENGTH} hex characters "
                f"(SHA-256), got {len(normalized)}"
            )
        try:
            bytes.fromhex(normalized)
        except ValueError as exc:
            raise ValueError(f"document hash is not valid hex: {value!r}") from exc
        return cls(normalized)

    def __repr__(self) -> str:
        return f"DocumentHash('{self[:12]}...')"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls.parse,
            core_schema.str_schema(),
            serialization=core_schema.to_string_ser_schema(),
        )


class IdempotencyKey(str):
    """A stable fingerprint of one financial action. Build it with :func:`idempotency_key`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return f"IdempotencyKey('{self[:16]}...')"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(min_length=1),
            serialization=core_schema.to_string_ser_schema(),
        )


def idempotency_key(*components: str) -> IdempotencyKey:
    """Derive a stable key from the identity of the action being performed.

    Spec §11 requires that ERP retries cannot create duplicate financial transactions, and
    DoD-6 makes it an acceptance criterion. That guarantee rests entirely on the key being a
    *function of the action* rather than of the attempt::

        >>> a = idempotency_key("post_invoice", "inv_018f...", "9901")
        >>> b = idempotency_key("post_invoice", "inv_018f...", "9901")
        >>> a == b
        True

    Retrying the same action therefore produces the same key, and the ERP layer recognises
    it as already done. A ``uuid4()`` here would look correct in every test and silently
    double-pay in production, because each retry would present a key nobody had seen.

    Components are joined with a separator that cannot appear in an identifier, so that
    ``("ab", "c")`` and ``("a", "bc")`` cannot collide.
    """
    if not components:
        raise ValueError("an idempotency key needs at least one identifying component")
    if any(not part for part in components):
        raise ValueError(
            f"idempotency key components must be non-empty, got {components!r}. "
            "An empty component silently merges distinct actions into one key."
        )
    joined = "\x1f".join(components)
    return IdempotencyKey(hashlib.sha256(joined.encode("utf-8")).hexdigest())
