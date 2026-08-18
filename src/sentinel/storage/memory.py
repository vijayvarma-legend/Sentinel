"""An in-memory document store.

Ships in the package rather than in the test suite because it is not only a test double: the
evaluation harness (spec §13) runs a fixed benchmark set repeatedly, and doing that against
object storage would be slow and would leave debris in a bucket.

It implements exactly the same write-once, content-addressed contract as
:class:`~sentinel.storage.store.S3DocumentStore`, including verification on read, so a test
that passes here is testing the real semantics.
"""

from __future__ import annotations

from sentinel.core.errors import StorageError
from sentinel.core.ids import DocumentHash
from sentinel.storage.store import URI_SCHEME, StoredDocument, key_for

__all__ = ["InMemoryDocumentStore"]


class InMemoryDocumentStore:
    """A dict-backed :class:`~sentinel.storage.store.DocumentStore`."""

    def __init__(self, bucket: str = "memory") -> None:
        self._bucket = bucket
        self._objects: dict[str, bytes] = {}

    def put(self, data: bytes, *, content_type: str) -> StoredDocument:
        if not data:
            raise StorageError("refusing to store an empty document")

        document_hash = DocumentHash.of(data)
        key = key_for(document_hash, content_type)

        if key in self._objects:
            return StoredDocument(self._uri(key), document_hash, already_existed=True)

        self._objects[key] = data
        return StoredDocument(self._uri(key), document_hash, already_existed=False)

    def get(self, uri: str) -> bytes:
        key = self._key_from_uri(uri)
        try:
            data = self._objects[key]
        except KeyError as exc:
            raise StorageError(f"no document at {uri}", uri=uri) from exc

        expected = key.rsplit("/", 1)[-1].split(".")[0]
        actual = DocumentHash.of(data)
        if actual != expected:
            raise StorageError(
                f"stored document at {uri} does not match its content address", uri=uri
            )
        return data

    def exists(self, document_hash: DocumentHash, *, content_type: str) -> bool:
        return key_for(document_hash, content_type) in self._objects

    # -- test affordances ---------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._objects)

    def corrupt(self, uri: str, data: bytes) -> None:
        """Overwrite an object's bytes, bypassing write-once.

        Deliberately not part of the ``DocumentStore`` protocol -- no production code can
        reach it. It exists so the corruption-detection path can actually be exercised,
        which is otherwise untestable without damaging a real bucket.
        """
        self._objects[self._key_from_uri(uri)] = data

    def _uri(self, key: str) -> str:
        return f"{URI_SCHEME}://{self._bucket}/{key}"

    def _key_from_uri(self, uri: str) -> str:
        prefix = f"{URI_SCHEME}://{self._bucket}/"
        if not uri.startswith(prefix):
            raise StorageError(f"{uri!r} does not belong to bucket {self._bucket!r}", uri=uri)
        return uri[len(prefix) :]
