"""Storage abstraction for SDK state (checkpoints, papers, library index).

Two backends:

- ``LocalStorage`` — the current default. Writes to ``~/.agentpub/`` (or any
  directory). Used by the CLI, GUI, and `AgentPub.exe` exactly as before.

- ``GCSStorage`` — used by the hosted research-as-a-service runner. Writes
  under a per-user/per-job prefix in a private GCS bucket. Lazy-imports
  ``google-cloud-storage`` so SDK users who don't install it aren't affected.

The pipeline (`playbook_researcher.py`, `library.py`) takes a `StorageBackend`
in its constructor and delegates all on-disk operations to it. When no backend
is passed, `LocalStorage()` is constructed with the historical homedir layout.

Key namespace convention (used by callers):

    checkpoints/pb_<safe-topic>.json    # research run checkpoints
    papers/<paper_id>.json              # submitted paper artifacts
    library/index.json                  # paper library index
    library/files/<hash>                # paper library file content
    artifacts/<step>.json               # cloud-runner intermediate artifacts
    logs/runner.log                     # cloud-runner log

Backends are oblivious to the namespace — they just store/retrieve bytes by
key. The structure is a contract between callers, not a backend feature.
"""
from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("agentpub.storage")


@runtime_checkable
class StorageBackend(Protocol):
    """Abstract byte-store with prefix listing.

    All methods are synchronous. Implementations must be thread-safe for the
    same key (concurrent writers to the same key produce one of the writes
    intact — last-writer-wins is acceptable, partial writes are not).
    """

    def save_bytes(self, key: str, data: bytes) -> None: ...
    def load_bytes(self, key: str) -> bytes | None: ...
    def delete_bytes(self, key: str) -> bool: ...
    def exists(self, key: str) -> bool: ...
    def list_keys(self, prefix: str = "") -> list[str]: ...


# ---------------------------------------------------------------------------
# Convenience helpers (mixed into both concrete backends)
# ---------------------------------------------------------------------------


class _JsonHelpersMixin:
    """Default save_json/load_json/save_text/load_text built on save_bytes/load_bytes."""

    def save_json(self, key: str, obj: Any, *, indent: int | None = 2) -> None:
        data = json.dumps(obj, default=str, indent=indent, ensure_ascii=False)
        self.save_bytes(key, data.encode("utf-8"))

    def load_json(self, key: str) -> Any | None:
        raw = self.load_bytes(key)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("load_json(%s) failed to parse: %s", key, e)
            return None

    def save_text(self, key: str, text: str) -> None:
        self.save_bytes(key, text.encode("utf-8"))

    def load_text(self, key: str) -> str | None:
        raw = self.load_bytes(key)
        if raw is None:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            logger.warning("load_text(%s) failed to decode utf-8: %s", key, e)
            return None


# ---------------------------------------------------------------------------
# Local filesystem backend (homedir default — current SDK behaviour)
# ---------------------------------------------------------------------------


class LocalStorage(_JsonHelpersMixin):
    """Stores blobs under a local directory.

    Layout: ``<root>/<key>``. Parent directories are created on demand.
    Atomic writes via temp-file + rename (so a crashed write never leaves a
    half-written file).
    """

    def __init__(self, root: pathlib.Path | str | None = None):
        if root is None:
            root = pathlib.Path.home() / ".agentpub"
        self._root = pathlib.Path(root)

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def _path(self, key: str) -> pathlib.Path:
        # Reject paths that try to escape the root via "..".
        if ".." in pathlib.PurePosixPath(key).parts:
            raise ValueError(f"key may not contain '..': {key!r}")
        return self._root / key

    def save_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic via tmp + rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def load_bytes(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError as e:
            logger.warning("LocalStorage.load_bytes(%s) failed: %s", key, e)
            return None

    def delete_bytes(self, key: str) -> bool:
        path = self._path(key)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.warning("LocalStorage.delete_bytes(%s) failed: %s", key, e)
            return False

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self._root
        if not base.exists():
            return []
        if base.is_file():
            return [prefix]
        results: list[str] = []
        for p in sorted(base.rglob("*")):
            if p.is_file():
                rel = p.relative_to(self._root).as_posix()
                results.append(rel)
        return results


# ---------------------------------------------------------------------------
# GCS backend (used by the Cloud Run Job runner)
# ---------------------------------------------------------------------------


class GCSStorage(_JsonHelpersMixin):
    """Stores blobs under a GCS bucket + prefix.

    Lazy-imports ``google-cloud-storage`` so SDK users without the package
    don't pay the import cost. Uses the runner SA's ADC for auth (no
    explicit credentials passed).

    Object key in the bucket: ``<prefix>/<key>``. Caller-supplied keys MUST
    NOT contain ``..`` segments (defence against accidental escape across
    a per-user prefix).
    """

    def __init__(self, bucket: str, prefix: str = ""):
        if not bucket:
            raise ValueError("bucket is required")
        # Lazy import — only fail when the backend is actually constructed.
        try:
            from google.cloud import storage as _gcs_storage  # type: ignore[import-untyped]
        except ImportError as e:
            raise RuntimeError(
                "GCSStorage requires google-cloud-storage. "
                "Install with: pip install google-cloud-storage"
            ) from e
        self._client_module = _gcs_storage
        self._client = _gcs_storage.Client()
        self._bucket_name = bucket
        # Normalise prefix: no leading slash, exactly one trailing slash if non-empty.
        prefix = prefix.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"
        self._prefix = prefix
        self._bucket = self._client.bucket(bucket)

    @property
    def bucket(self) -> str:
        return self._bucket_name

    @property
    def prefix(self) -> str:
        return self._prefix

    def _object_name(self, key: str) -> str:
        if ".." in pathlib.PurePosixPath(key).parts:
            raise ValueError(f"key may not contain '..': {key!r}")
        return self._prefix + key.lstrip("/")

    def save_bytes(self, key: str, data: bytes) -> None:
        blob = self._bucket.blob(self._object_name(key))
        # Sensible content-type defaults — JSON for .json keys, text for .log/.txt.
        ct = "application/octet-stream"
        if key.endswith(".json"):
            ct = "application/json; charset=utf-8"
        elif key.endswith((".log", ".txt", ".md")):
            ct = "text/plain; charset=utf-8"
        blob.upload_from_string(data, content_type=ct)

    def load_bytes(self, key: str) -> bytes | None:
        blob = self._bucket.blob(self._object_name(key))
        # Lazy-import the not-found exception.
        try:
            from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]
        except ImportError:
            NotFound = Exception  # type: ignore[assignment]
        try:
            return blob.download_as_bytes()
        except NotFound:
            return None
        except Exception as e:
            logger.warning("GCSStorage.load_bytes(%s) failed: %s", key, e)
            return None

    def delete_bytes(self, key: str) -> bool:
        blob = self._bucket.blob(self._object_name(key))
        try:
            from google.cloud.exceptions import NotFound  # type: ignore[import-untyped]
        except ImportError:
            NotFound = Exception  # type: ignore[assignment]
        try:
            blob.delete()
            return True
        except NotFound:
            return False
        except Exception as e:
            logger.warning("GCSStorage.delete_bytes(%s) failed: %s", key, e)
            return False

    def exists(self, key: str) -> bool:
        blob = self._bucket.blob(self._object_name(key))
        try:
            return blob.exists()
        except Exception as e:
            logger.warning("GCSStorage.exists(%s) failed: %s", key, e)
            return False

    def list_keys(self, prefix: str = "") -> list[str]:
        full_prefix = self._object_name(prefix) if prefix else self._prefix
        try:
            blobs = self._client.list_blobs(self._bucket_name, prefix=full_prefix)
            results: list[str] = []
            for b in blobs:
                # Strip the constructor prefix so callers see relative keys.
                if b.name.startswith(self._prefix):
                    results.append(b.name[len(self._prefix):])
                else:
                    results.append(b.name)
            return sorted(results)
        except Exception as e:
            logger.warning("GCSStorage.list_keys(%s) failed: %s", prefix, e)
            return []


__all__ = ["StorageBackend", "LocalStorage", "GCSStorage"]
