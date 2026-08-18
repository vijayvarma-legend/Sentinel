"""SVC-02 -- the original document store.

Spec §4.1 requires the original document to be stored; §12 requires that every financial
action be reproducible from the audit trail. Both depend on the stored bytes still being the
bytes that were ingested, months later, after any number of retries and resubmissions.

Two design choices give that for free rather than by discipline:

**Content addressing.** A document's key *is* its SHA-256. Two consequences follow without
any bookkeeping: the same bytes always land on the same key, so a retry cannot create a
second copy; and a document fetched from a key is verifiably the document that produced that
key, so silent corruption is detectable rather than hypothetical.

**Write-once.** :meth:`DocumentStore.put` never overwrites. Because keys are content
addresses, an existing key holds identical bytes by definition, so the correct response to a
collision is to do nothing and report the existing location -- not to rewrite it, and not to
error. That makes ingestion naturally idempotent under retry.
"""

from __future__ import annotations

import mimetypes
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sentinel.core.errors import StorageError
from sentinel.core.ids import DocumentHash

if TYPE_CHECKING:  # pragma: no cover
    from sentinel.core.settings import Settings

__all__ = ["DocumentStore", "S3DocumentStore", "StoredDocument", "key_for"]

URI_SCHEME = "s3"


def key_for(document_hash: DocumentHash, content_type: str) -> str:
    """The object key for a hash: ``sha256/<first two>/<hash><ext>``.

    The two-character shard prefix keeps any single listing prefix from growing without
    bound, which matters for S3 listing performance and for anyone who has to browse the
    bucket. The extension is cosmetic -- it makes a downloaded object open in the right
    application -- and is never trusted for anything.
    """
    extension = mimetypes.guess_extension(content_type) or ""
    return f"sha256/{document_hash[:2]}/{document_hash}{extension}"


class StoredDocument:
    """Where a document came to rest, and whether this call is what put it there."""

    __slots__ = ("already_existed", "document_hash", "uri")

    def __init__(self, uri: str, document_hash: DocumentHash, *, already_existed: bool) -> None:
        self.uri = uri
        self.document_hash = document_hash
        self.already_existed = already_existed
        """True when the bytes were already stored.

        Not a failure -- it is the store absorbing a retry or a genuine resubmission. The
        distinction is preserved because duplicate detection (spec §6) wants to know, and
        because an audit trail should not show two arrivals as two documents.
        """

    def __repr__(self) -> str:
        return f"StoredDocument(uri={self.uri!r}, already_existed={self.already_existed})"


@runtime_checkable
class DocumentStore(Protocol):
    """The interface every consumer depends on. S3 is one implementation of it.

    A Protocol rather than a base class so that tests can substitute an in-memory store
    without inheriting anything, and so the ingestion service never learns what backs it.
    """

    def put(self, data: bytes, *, content_type: str) -> StoredDocument:
        """Store `data`, returning where it lives. Never overwrites."""
        ...

    def get(self, uri: str) -> bytes:
        """Retrieve the bytes at `uri`, verifying they still hash to the key."""
        ...

    def exists(self, document_hash: DocumentHash, *, content_type: str) -> bool: ...


class S3DocumentStore:
    """S3-compatible document storage. MinIO locally, S3 in production (ADR-0006)."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        import boto3

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> S3DocumentStore:
        return cls(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
        )

    # -- lifecycle ----------------------------------------------------------------------

    def ensure_bucket(self) -> None:
        """Create the bucket if it does not exist. Local development convenience.

        Production buckets are provisioned with their retention and encryption policies by
        infrastructure, not by the application -- so this is called from local setup and
        tests, never on a request path.
        """
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            try:
                self._client.create_bucket(Bucket=self._bucket)
            except ClientError as exc:
                raise StorageError(
                    f"could not create bucket {self._bucket!r}", bucket=self._bucket
                ) from exc

    # -- operations ---------------------------------------------------------------------

    def put(self, data: bytes, *, content_type: str) -> StoredDocument:
        """Store `data` at its content address.

        Idempotent by construction: if the key is already present it holds these exact
        bytes, so nothing is written and the existing location is returned.
        """
        from botocore.exceptions import ClientError

        if not data:
            raise StorageError("refusing to store an empty document")

        document_hash = DocumentHash.of(data)
        key = key_for(document_hash, content_type)

        if self._head(key):
            return StoredDocument(self._uri(key), document_hash, already_existed=True)

        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                # Belt and braces against a concurrent writer: content addressing already
                # means any existing object holds identical bytes, but this makes the
                # write-once property explicit to the storage layer itself.
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if _is_precondition_failure(exc):
                # Another writer stored the same bytes between the head and the put. That
                # is the idempotent path, not a failure.
                return StoredDocument(self._uri(key), document_hash, already_existed=True)
            raise StorageError(
                f"could not store document {document_hash[:12]}...",
                bucket=self._bucket,
                key=key,
            ) from exc

        return StoredDocument(self._uri(key), document_hash, already_existed=False)

    def get(self, uri: str) -> bytes:
        """Fetch the bytes at `uri` and verify they still hash to their own key.

        The verification is the point of content addressing. Without it, a truncated or
        corrupted object would be handed to extraction and read as though it were the
        invoice -- and the resulting payment would be perfectly auditable and wrong.
        """
        from botocore.exceptions import ClientError

        key = self._key_from_uri(uri)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            data: bytes = response["Body"].read()
        except ClientError as exc:
            raise StorageError(f"could not read {uri}", bucket=self._bucket, key=key) from exc

        expected = key.rsplit("/", 1)[-1].split(".")[0]
        actual = DocumentHash.of(data)
        if actual != expected:
            raise StorageError(
                f"stored document at {uri} does not match its content address: "
                f"expected {expected[:12]}..., got {actual[:12]}... "
                "The object has been altered or corrupted since it was written.",
                bucket=self._bucket,
                key=key,
            )
        return data

    def exists(self, document_hash: DocumentHash, *, content_type: str) -> bool:
        return self._head(key_for(document_hash, content_type))

    # -- internals ----------------------------------------------------------------------

    def _head(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise StorageError(f"could not check for {key}", bucket=self._bucket, key=key) from exc
        return True

    def _uri(self, key: str) -> str:
        return f"{URI_SCHEME}://{self._bucket}/{key}"

    def _key_from_uri(self, uri: str) -> str:
        prefix = f"{URI_SCHEME}://{self._bucket}/"
        if not uri.startswith(prefix):
            raise StorageError(f"{uri!r} does not belong to bucket {self._bucket!r}", uri=uri)
        return uri[len(prefix) :]


def _is_precondition_failure(exc: Exception) -> bool:
    """Whether a ClientError is S3's "the object already exists" response to IfNoneMatch."""
    response: dict[str, Any] = getattr(exc, "response", {})
    code = response.get("Error", {}).get("Code")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return bool(code == "PreconditionFailed" or status == 412)
