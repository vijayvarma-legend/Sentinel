"""Document store semantics.

The unit tests run against the in-memory store; the integration tests run the *same*
assertions against MinIO, so the two implementations cannot quietly diverge.
"""

from __future__ import annotations

import pytest

from sentinel.core.errors import StorageError
from sentinel.core.ids import DocumentHash
from sentinel.core.settings import Settings
from sentinel.storage.memory import InMemoryDocumentStore
from sentinel.storage.store import DocumentStore, S3DocumentStore, key_for

PDF = "application/pdf"
INVOICE = b"%PDF-1.7 fake invoice bytes for INV-8821"


@pytest.fixture
def store() -> InMemoryDocumentStore:
    return InMemoryDocumentStore()


class TestKeyDerivation:
    def test_key_is_the_content_address(self) -> None:
        digest = DocumentHash.of(INVOICE)
        assert key_for(digest, PDF) == f"sha256/{digest[:2]}/{digest}.pdf"

    def test_identical_bytes_always_produce_the_same_key(self) -> None:
        assert key_for(DocumentHash.of(INVOICE), PDF) == key_for(DocumentHash.of(INVOICE), PDF)

    def test_an_unknown_content_type_simply_has_no_extension(self) -> None:
        digest = DocumentHash.of(INVOICE)
        assert key_for(digest, "application/x-nonsense").endswith(digest)


class TestWriteOnce:
    def test_storing_returns_a_uri(self, store: InMemoryDocumentStore) -> None:
        stored = store.put(INVOICE, content_type=PDF)
        assert stored.uri.startswith("s3://")
        assert not stored.already_existed

    def test_storing_the_same_bytes_twice_stores_one_object(
        self, store: InMemoryDocumentStore
    ) -> None:
        """Ingestion is idempotent under retry, without any bookkeeping to get wrong."""
        first = store.put(INVOICE, content_type=PDF)
        second = store.put(INVOICE, content_type=PDF)

        assert second.uri == first.uri
        assert second.already_existed
        assert len(store) == 1

    def test_different_bytes_land_on_different_keys(self, store: InMemoryDocumentStore) -> None:
        a = store.put(INVOICE, content_type=PDF)
        b = store.put(INVOICE + b" amended", content_type=PDF)

        assert a.uri != b.uri
        assert len(store) == 2

    def test_an_empty_document_is_refused(self, store: InMemoryDocumentStore) -> None:
        with pytest.raises(StorageError, match="empty document"):
            store.put(b"", content_type=PDF)


class TestRetrieval:
    def test_round_trips_the_exact_bytes(self, store: InMemoryDocumentStore) -> None:
        stored = store.put(INVOICE, content_type=PDF)
        assert store.get(stored.uri) == INVOICE

    def test_a_missing_document_raises(self, store: InMemoryDocumentStore) -> None:
        with pytest.raises(StorageError):
            store.get("s3://memory/sha256/ab/" + "a" * 64 + ".pdf")

    def test_a_uri_from_another_bucket_is_refused(self, store: InMemoryDocumentStore) -> None:
        with pytest.raises(StorageError, match="does not belong to bucket"):
            store.get("s3://someone-elses-bucket/sha256/ab/whatever.pdf")

    def test_corruption_is_detected_on_read(self, store: InMemoryDocumentStore) -> None:
        """The reason content addressing earns its keep.

        Without this check a truncated object would be handed to extraction and read as if
        it were the invoice, and the resulting payment would be perfectly auditable and
        completely wrong.
        """
        stored = store.put(INVOICE, content_type=PDF)
        store.corrupt(stored.uri, b"%PDF-1.7 tampered")

        with pytest.raises(StorageError, match="does not match its content address"):
            store.get(stored.uri)


class TestExistence:
    def test_reports_presence_by_hash(self, store: InMemoryDocumentStore) -> None:
        digest = DocumentHash.of(INVOICE)
        assert not store.exists(digest, content_type=PDF)

        store.put(INVOICE, content_type=PDF)
        assert store.exists(digest, content_type=PDF)


class TestProtocolConformance:
    def test_both_implementations_satisfy_the_protocol(self) -> None:
        """Consumers depend on DocumentStore, never on a concrete class."""
        assert isinstance(InMemoryDocumentStore(), DocumentStore)


# ---------------------------------------------------------------------------------------
# The same contract, against real MinIO
# ---------------------------------------------------------------------------------------


@pytest.fixture
def s3_store() -> S3DocumentStore:
    settings = Settings(env="test", _env_file=None)  # type: ignore[call-arg]
    store = S3DocumentStore.from_settings(settings)
    store.ensure_bucket()
    return store


@pytest.mark.integration
class TestAgainstMinio:
    """Requires `make up`. Runs the same guarantees against the real backend."""

    def test_round_trips_the_exact_bytes(self, s3_store: S3DocumentStore) -> None:
        payload = b"%PDF-1.7 integration " + DocumentHash.of(INVOICE).encode()
        stored = s3_store.put(payload, content_type=PDF)
        assert s3_store.get(stored.uri) == payload

    def test_storing_the_same_bytes_twice_is_idempotent(self, s3_store: S3DocumentStore) -> None:
        payload = b"%PDF-1.7 idempotency probe"
        first = s3_store.put(payload, content_type=PDF)
        second = s3_store.put(payload, content_type=PDF)

        assert second.uri == first.uri
        assert second.already_existed

    def test_reports_existence_by_hash(self, s3_store: S3DocumentStore) -> None:
        payload = b"%PDF-1.7 existence probe"
        s3_store.put(payload, content_type=PDF)
        assert s3_store.exists(DocumentHash.of(payload), content_type=PDF)

    def test_an_absent_document_raises(self, s3_store: S3DocumentStore) -> None:
        with pytest.raises(StorageError):
            s3_store.get("s3://sentinel-documents/sha256/ff/" + "f" * 64 + ".pdf")
