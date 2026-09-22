"""Object storage for original files, scans, and generated reports.

The canonical PostgreSQL model never stores binary content. Every uploaded file
is kept unchanged under a tenant-prefixed key so lineage can always point back
to the original artifact. Three backends share one small interface:

* ``InMemoryObjectStore`` for tests and ephemeral runs;
* ``LocalFileObjectStore`` for development;
* ``S3ObjectStore`` for deployments (boto3 is imported lazily so the dependency
  stays optional).
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ObjectStoreError(RuntimeError):
    """Raised when an object cannot be stored or retrieved safely."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str
    content_type: str
    stored_at: datetime
    backend: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "content_type": self.content_type,
            "stored_at": self.stored_at.isoformat(),
            "backend": self.backend,
        }


def build_object_key(institution_id: str, category: str, object_id: str, file_name: str) -> str:
    """Compose a tenant-prefixed key; every segment is validated, never interpolated blindly."""

    safe_name = safe_file_name(file_name)
    for segment in (institution_id, category, object_id):
        if not _SAFE_SEGMENT.fullmatch(segment):
            raise ValueError(f"object key segment is not safe: {segment!r}")
    return f"{institution_id}/{category}/{object_id}/{safe_name}"


def safe_file_name(file_name: str) -> str:
    base = Path(file_name.replace("\\", "/")).name.strip()
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")
    if not cleaned:
        raise ValueError("file name must contain at least one safe character")
    return cleaned[:160]


def _validate_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/") or "\\" in key:
        raise ValueError("object key must be a relative, traversal-free path")
    parts = key.split("/")
    if len(parts) < 2:
        raise ValueError("object key must be tenant-prefixed")
    return key


class ObjectStore(Protocol):
    backend_name: str

    def put(self, key: str, content: bytes, content_type: str = "application/octet-stream") -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...

    def list_keys(self, prefix: str) -> tuple[str, ...]: ...


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(slots=True)
class InMemoryObjectStore:
    backend_name: str = "memory"
    max_bytes: int = 200_000_000
    _objects: dict[str, tuple[bytes, StoredObject]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def put(self, key: str, content: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        _validate_key(key)
        if not isinstance(content, (bytes, bytearray)):
            raise ValueError("object content must be bytes")
        with self._lock:
            total = sum(len(item[0]) for item in self._objects.values()) + len(content)
            if total > self.max_bytes:
                raise ObjectStoreError("in-memory object store capacity exceeded")
            stored = StoredObject(key, len(content), _digest(content), content_type, datetime.now(timezone.utc), self.backend_name)
            self._objects[key] = (bytes(content), stored)
        return stored

    def get(self, key: str) -> bytes:
        with self._lock:
            try:
                return self._objects[key][0]
            except KeyError as exc:
                raise ObjectStoreError(f"object not found: {key}") from exc

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._objects

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._objects.pop(key, None) is not None

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(key for key in self._objects if key.startswith(prefix)))


class LocalFileObjectStore:
    backend_name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, key: str) -> Path:
        _validate_key(key)
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("object key escapes the storage root")
        return path

    def put(self, key: str, content: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        path = self._path(key)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".part")
            temp.write_bytes(content)
            temp.replace(path)
        return StoredObject(key, len(content), _digest(content), content_type, datetime.now(timezone.utc), self.backend_name)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectStoreError(f"object not found: {key}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False
        path.unlink()
        return True

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        keys: list[str] = []
        for path in self.root.rglob("*"):
            if path.is_file() and not path.name.endswith(".part"):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return tuple(sorted(keys))


class S3ObjectStore:
    """Amazon S3 backend; credentials come from the deployment's IAM role or environment."""

    backend_name = "s3"

    def __init__(self, bucket: str, *, prefix: str = "", region: str | None = None, client: Any | None = None) -> None:
        if not bucket.strip():
            raise ValueError("S3 bucket name must not be blank")
        self.bucket = bucket.strip()
        self.prefix = prefix.strip("/")
        if client is None:
            try:
                import boto3  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - exercised only without boto3
                raise RuntimeError("S3 object storage requires the optional boto3 dependency") from exc
            client = boto3.client("s3", region_name=region) if region else boto3.client("s3")
        self._client = client

    def _full_key(self, key: str) -> str:
        _validate_key(key)
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, content: bytes, content_type: str = "application/octet-stream") -> StoredObject:
        try:
            self._client.put_object(Bucket=self.bucket, Key=self._full_key(key), Body=content, ContentType=content_type, ServerSideEncryption="AES256")
        except Exception as exc:  # noqa: BLE001 - boto3 raises many client classes
            raise ObjectStoreError("S3 put failed") from exc
        return StoredObject(key, len(content), _digest(content), content_type, datetime.now(timezone.utc), self.backend_name)

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._full_key(key))
            body = response["Body"].read()
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError(f"object not found or unreadable: {key}") from exc
        return bytes(body)

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full_key(key))
        except Exception:  # noqa: BLE001
            return False
        return True

    def delete(self, key: str) -> bool:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._full_key(key))
        except Exception:  # noqa: BLE001
            return False
        return True

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        full_prefix = self._full_key(prefix) if "/" in prefix else (f"{self.prefix}/{prefix}" if self.prefix else prefix)
        keys: list[str] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if self.prefix and key.startswith(self.prefix + "/"):
                        key = key[len(self.prefix) + 1:]
                    keys.append(key)
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError("S3 list failed") from exc
        return tuple(sorted(keys))


__all__ = [
    "InMemoryObjectStore",
    "LocalFileObjectStore",
    "ObjectStore",
    "ObjectStoreError",
    "S3ObjectStore",
    "StoredObject",
    "build_object_key",
    "safe_file_name",
]
