"""Identifier guarantees: type distinctness, time ordering, and derived idempotency."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel, ValidationError

from sentinel.core.ids import (
    CorrelationId,
    DocumentHash,
    IdempotencyKey,
    InvoiceId,
    PolicyVersionId,
    VendorId,
    idempotency_key,
    uuid7,
)

ID_TYPES = [CorrelationId, InvoiceId, VendorId, PolicyVersionId]


class TestUuid7:
    def test_is_a_well_formed_version_7_uuid(self) -> None:
        parsed = uuid.UUID(uuid7())
        assert parsed.version == 7
        assert parsed.variant == uuid.RFC_4122

    def test_embeds_the_current_time(self) -> None:
        before = time.time_ns() // 1_000_000
        embedded = int(uuid7().replace("-", "")[:12], 16)
        after = time.time_ns() // 1_000_000

        assert before <= embedded <= after, "the leading 48 bits must be Unix milliseconds"

    def test_lexical_order_matches_creation_order(self) -> None:
        """The property that lets an audit trail sort chronologically without a join."""
        generated = []
        for _ in range(5):
            generated.append(uuid7())
            time.sleep(0.002)

        assert generated == sorted(generated)

    def test_is_unique_under_concurrency(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            produced = list(pool.map(lambda _: uuid7(), range(2000)))

        assert len(set(produced)) == 2000


class TestPrefixedIds:
    @pytest.mark.parametrize("id_type", ID_TYPES)
    def test_new_ids_carry_their_prefix_and_validate(self, id_type: type) -> None:
        minted = id_type.new()
        assert minted.startswith(f"{id_type.PREFIX}_")
        assert id_type.parse(str(minted)) == minted

    def test_prefixes_are_distinct(self) -> None:
        prefixes = [t.PREFIX for t in ID_TYPES]
        assert len(set(prefixes)) == len(prefixes)

    def test_a_vendor_id_is_rejected_where_an_invoice_id_belongs(self) -> None:
        """The bug this design exists to catch: identifiers crossed at a call site."""
        vendor = VendorId.new()
        with pytest.raises(ValueError, match="must start with 'inv_'"):
            InvoiceId.parse(str(vendor))

    @pytest.mark.parametrize("bad", ["", "cor_", "cor_not-a-uuid", "cor_1234", "nope"])
    def test_malformed_values_are_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            CorrelationId.parse(bad)

    def test_repr_names_the_type(self) -> None:
        assert repr(CorrelationId.new()).startswith("CorrelationId('cor_")

    def test_round_trips_through_pydantic(self) -> None:
        class Envelope(BaseModel):
            correlation_id: CorrelationId

        original = Envelope(correlation_id=CorrelationId.new())
        restored = Envelope.model_validate_json(original.model_dump_json())

        assert restored.correlation_id == original.correlation_id
        assert isinstance(restored.correlation_id, CorrelationId)

    def test_pydantic_rejects_a_foreign_prefix(self) -> None:
        class Envelope(BaseModel):
            correlation_id: CorrelationId

        with pytest.raises(ValidationError):
            Envelope(correlation_id=str(InvoiceId.new()))  # type: ignore[arg-type]


class TestDocumentHash:
    def test_identical_bytes_hash_identically(self) -> None:
        """The basis of exact duplicate detection (spec section 6)."""
        assert DocumentHash.of(b"invoice bytes") == DocumentHash.of(b"invoice bytes")

    def test_one_changed_byte_changes_the_hash(self) -> None:
        assert DocumentHash.of(b"invoice bytes") != DocumentHash.of(b"invoice byteS")

    def test_matches_sha256(self) -> None:
        import hashlib

        assert DocumentHash.of(b"abc") == hashlib.sha256(b"abc").hexdigest()

    def test_normalizes_case(self) -> None:
        digest = DocumentHash.of(b"x")
        assert DocumentHash.parse(digest.upper()) == digest

    @pytest.mark.parametrize("bad", ["", "abc", "z" * 64, "a" * 63])
    def test_rejects_non_sha256_values(self, bad: str) -> None:
        with pytest.raises(ValueError):
            DocumentHash.parse(bad)

    def test_repr_is_truncated(self) -> None:
        """Full 64-character hashes make log lines unreadable."""
        assert repr(DocumentHash.of(b"x")) == f"DocumentHash('{DocumentHash.of(b'x')[:12]}...')"


class TestIdempotencyKey:
    def test_same_action_yields_the_same_key(self) -> None:
        """DoD-6 rests on this: a retry must present the key the ERP already recorded."""
        first = idempotency_key("post_invoice", "inv_abc", "9901")
        second = idempotency_key("post_invoice", "inv_abc", "9901")
        assert first == second

    def test_different_actions_yield_different_keys(self) -> None:
        assert idempotency_key("post_invoice", "inv_abc") != idempotency_key(
            "post_invoice", "inv_xyz"
        )
        assert idempotency_key("post_invoice", "inv_abc") != idempotency_key(
            "void_invoice", "inv_abc"
        )

    def test_component_boundaries_cannot_be_smeared(self) -> None:
        """('ab', 'c') and ('a', 'bc') are different actions and must not collide."""
        assert idempotency_key("ab", "c") != idempotency_key("a", "bc")

    def test_is_stable_across_processes(self) -> None:
        """Derived from content only -- no salt, no PYTHONHASHSEED dependence.

        A key that changed between worker processes would let two workers double-post.
        """
        import hashlib

        expected = hashlib.sha256(b"post\x1finv_abc").hexdigest()
        assert idempotency_key("post", "inv_abc") == expected

    def test_rejects_empty_components(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            idempotency_key("post_invoice", "")

    def test_rejects_no_components(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            idempotency_key()

    def test_round_trips_through_pydantic(self) -> None:
        class Action(BaseModel):
            key: IdempotencyKey

        action = Action(key=idempotency_key("post", "inv_1"))
        assert Action.model_validate_json(action.model_dump_json()).key == action.key
