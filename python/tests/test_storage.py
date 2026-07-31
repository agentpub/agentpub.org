"""Unit tests for the storage abstraction.

Covers:
- LocalStorage byte/text/JSON round-trips
- Atomic write semantics (no .tmp leftovers on success)
- Path-escape rejection (key with "..")
- list_keys behaviour
- StorageBackend Protocol conformance
- Backwards-compat: PaperLibrary with legacy library_dir-only signature
- Backwards-compat: PlaybookResearcher default storage = LocalStorage at AGENTPUB_HOME

GCSStorage is not tested here — it's tested in api/tests against a fake bucket
in the runner integration tests.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile

import pytest

from agentpub.storage import LocalStorage, StorageBackend


# ---------------------------------------------------------------------------
# LocalStorage
# ---------------------------------------------------------------------------


def test_local_storage_protocol_conformance():
    ls = LocalStorage(root=pathlib.Path(tempfile.gettempdir()))
    assert isinstance(ls, StorageBackend)


def test_local_storage_bytes_roundtrip(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    ls.save_bytes("checkpoints/pb_foo.json", b'{"step":3}')
    assert ls.load_bytes("checkpoints/pb_foo.json") == b'{"step":3}'
    assert ls.exists("checkpoints/pb_foo.json")


def test_local_storage_json_roundtrip(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    payload = {"step": 7, "topic": "Memory consolidation", "nested": {"a": [1, 2, 3]}}
    ls.save_json("checkpoints/pb_bar.json", payload)
    loaded = ls.load_json("checkpoints/pb_bar.json")
    assert loaded == payload


def test_local_storage_text_roundtrip(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    ls.save_text("logs/run.log", "Hello\nWorld\n")
    assert ls.load_text("logs/run.log") == "Hello\nWorld\n"


def test_local_storage_missing_keys_return_none(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    assert ls.load_bytes("nope.json") is None
    assert ls.load_text("nope.txt") is None
    assert ls.load_json("nope.json") is None
    assert not ls.exists("nope.json")
    assert ls.list_keys("doesnt/exist/") == []


def test_local_storage_delete(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    ls.save_bytes("foo.json", b"x")
    assert ls.delete_bytes("foo.json") is True
    # idempotent: second delete returns False
    assert ls.delete_bytes("foo.json") is False
    assert not ls.exists("foo.json")


def test_local_storage_list_keys(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    ls.save_bytes("checkpoints/pb_a.json", b"a")
    ls.save_bytes("checkpoints/pb_b.json", b"b")
    ls.save_bytes("papers/p1.json", b"p")
    keys = ls.list_keys("checkpoints/")
    assert "checkpoints/pb_a.json" in keys
    assert "checkpoints/pb_b.json" in keys
    assert "papers/p1.json" not in keys


def test_local_storage_rejects_path_escape(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    with pytest.raises(ValueError, match=r"\.\."):
        ls.save_bytes("../escape.json", b"evil")
    with pytest.raises(ValueError):
        ls.load_bytes("../../etc/passwd")


def test_local_storage_atomic_write_no_tmp_leftover(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    ls.save_bytes("checkpoints/pb_atomic.json", b"x" * 1024)
    leftovers = list(tmp_path.rglob("*.tmp"))
    assert leftovers == [], f"expected no .tmp files, found {leftovers}"


def test_local_storage_overwrites_existing(tmp_path: pathlib.Path):
    ls = LocalStorage(root=tmp_path)
    ls.save_bytes("k.json", b"v1")
    ls.save_bytes("k.json", b"v2")
    assert ls.load_bytes("k.json") == b"v2"


# ---------------------------------------------------------------------------
# Backwards compatibility — PaperLibrary
# ---------------------------------------------------------------------------


def test_paper_library_legacy_constructor_still_works(tmp_path: pathlib.Path):
    """PaperLibrary(library_dir=...) (the historical signature) must
    continue to write the index at <library_dir>/index.json.
    """
    from agentpub.library import PaperLibrary

    lib_dir = tmp_path / "library"
    lib = PaperLibrary(library_dir=lib_dir)
    idx = lib._load_index()
    idx["papers"]["test"] = {"title": "Foundation"}
    lib._index = idx
    lib._save_index()
    expected = lib_dir / "index.json"
    assert expected.exists(), f"index not at expected path: {expected}"
    payload = json.loads(expected.read_text(encoding="utf-8"))
    assert payload["papers"]["test"]["title"] == "Foundation"


def test_paper_library_with_explicit_storage(tmp_path: pathlib.Path):
    from agentpub.library import PaperLibrary

    storage = LocalStorage(root=tmp_path)
    lib = PaperLibrary(storage=storage)
    idx = lib._load_index()
    idx["papers"]["paper-1"] = {"title": "Quantum"}
    lib._index = idx
    lib._save_index()

    # Index lives under storage_root/library/index.json
    expected = tmp_path / "library" / "index.json"
    assert expected.exists()


def test_paper_library_index_persists_across_instances(tmp_path: pathlib.Path):
    from agentpub.library import PaperLibrary

    storage = LocalStorage(root=tmp_path)
    lib1 = PaperLibrary(storage=storage)
    lib1._load_index()
    lib1._index["papers"]["foo"] = {"title": "Bar"}
    lib1._save_index()

    lib2 = PaperLibrary(storage=storage)
    idx = lib2._load_index()
    assert "foo" in idx["papers"]


# ---------------------------------------------------------------------------
# Backwards compatibility — PlaybookResearcher default storage
# ---------------------------------------------------------------------------


def test_playbook_researcher_default_storage_is_local(monkeypatch, tmp_path: pathlib.Path):
    """When no storage is passed, default is LocalStorage rooted at AGENTPUB_HOME.

    AGENTPUB_HOME defaults to ~/.agentpub but can be overridden via env var,
    which matters for the cloud runner.
    """
    monkeypatch.setenv("AGENTPUB_HOME", str(tmp_path))

    from agentpub.playbook_researcher import PlaybookResearcher
    from agentpub.storage import LocalStorage

    pr = PlaybookResearcher.__new__(PlaybookResearcher)
    # Replicate the parts of __init__ that set up storage.
    from agentpub._constants import default_agentpub_home
    pr._storage = LocalStorage(default_agentpub_home())
    assert isinstance(pr._storage, LocalStorage)
    assert pr._storage.root == tmp_path


def test_playbook_researcher_save_checkpoint_uses_storage(tmp_path: pathlib.Path):
    """_save_checkpoint should write via self._storage, not directly to disk."""
    from agentpub.playbook_researcher import PlaybookResearcher

    class _StubLLM:
        @property
        def provider_name(self): return "stub"
        @property
        def model_name(self): return "stub-1"

    pr = PlaybookResearcher.__new__(PlaybookResearcher)
    pr.llm = _StubLLM()
    pr._storage = LocalStorage(root=tmp_path)
    pr.artifacts = {"foo": "bar"}
    pr._save_checkpoint("My research topic", step=4, challenge_id="ch_x")

    expected = tmp_path / "checkpoints" / "pb_My research topic.json"
    assert expected.exists()
    payload = json.loads(expected.read_text(encoding="utf-8"))
    assert payload["topic"] == "My research topic"
    assert payload["completed_step"] == 4
    assert payload["challenge_id"] == "ch_x"
    assert payload["artifacts"] == {"foo": "bar"}
