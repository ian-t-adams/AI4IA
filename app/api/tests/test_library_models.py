"""Library domain models + pure helpers (Phase 11A): can_access, hashing,
modality classification, and the built-in analyzer registry shape."""
from __future__ import annotations

from ai4ia_api.library.access import can_access, require_owner
from ai4ia_api.library.hashing import content_hash, dedupe_key
from ai4ia_api.library.modality import classify_modality
from ai4ia_api.library.models import (
    BUILTIN_ANALYZER_IDS,
    BUILTIN_ANALYZERS,
    Analyzer,
    AnalyzerKind,
    DocumentStatus,
    Modality,
    UserDocument,
    Visibility,
)


def _doc(**kw) -> UserDocument:
    base = dict(userId="u1", filename="f.pdf")
    base.update(kw)
    return UserDocument(**base)


# --- manifest defaults ---
def test_manifest_defaults_are_inert():
    doc = _doc()
    assert doc.status == DocumentStatus.pending
    assert doc.modality == Modality.other
    assert doc.visibility == Visibility.private
    assert doc.acl == []
    assert doc.sessionLinks == []
    assert doc.rawPath is None and doc.parsedPath is None and doc.chunksPath is None
    assert doc.chunkCount == 0
    assert doc.id  # generated


def test_manifest_touch_updates_timestamp():
    doc = _doc()
    before = doc.updatedAt
    doc.touch()
    assert doc.updatedAt >= before


# --- can_access / require_owner ---
def test_owner_can_access_and_is_owner():
    doc = _doc(userId="alice")
    assert can_access("alice", doc) is True
    assert require_owner("alice", doc) is True


def test_non_owner_denied_in_v1():
    doc = _doc(userId="alice")
    assert can_access("mallory", doc) is False
    assert require_owner("mallory", doc) is False


def test_reserved_sharing_paths():
    # These branches are inert in v1 (defaults never set them) but must behave
    # correctly so enabling sharing later is a pure flip.
    public = _doc(userId="alice", visibility=Visibility.public)
    assert can_access("anyone", public) is True
    assert require_owner("anyone", public) is False  # mutations stay owner-only

    shared = _doc(userId="alice", visibility=Visibility.shared, acl=["bob"])
    assert can_access("bob", shared) is True
    assert can_access("carol", shared) is False


# --- hashing / dedupe ---
def test_content_hash_is_stable_sha256():
    assert content_hash(b"hello") == content_hash(b"hello")
    assert content_hash(b"hello") != content_hash(b"world")
    assert len(content_hash(b"x")) == 64


def test_dedupe_key_separates_analyzers():
    h = content_hash(b"data")
    assert dedupe_key(h, "builtin-document") == dedupe_key(h, "builtin-document")
    assert dedupe_key(h, "builtin-document") != dedupe_key(h, "builtin-image")
    assert dedupe_key(h, None) != dedupe_key(h, "builtin-document")


# --- modality ---
def test_classify_modality_by_mime():
    assert classify_modality("application/pdf", "x") == Modality.document
    assert classify_modality("text/plain", "x") == Modality.text
    assert classify_modality("image/png", "x") == Modality.image
    assert classify_modality("audio/mpeg", "x") == Modality.audio
    assert classify_modality("video/mp4", "x") == Modality.video


def test_classify_modality_falls_back_to_extension():
    # Generic/empty MIME -> use the filename extension.
    assert classify_modality("application/octet-stream", "report.pdf") == Modality.document
    assert classify_modality("", "photo.JPG") == Modality.image
    assert classify_modality(None, "clip.mov") == Modality.video
    assert classify_modality("", "notes.md") == Modality.text


def test_classify_modality_unknown_is_other():
    assert classify_modality("application/x-thing", "mystery.zzz") == Modality.other


# --- built-in analyzers ---
def test_builtins_are_system_owned_and_consistent():
    assert {a.id for a in BUILTIN_ANALYZERS} == set(BUILTIN_ANALYZER_IDS)
    for a in BUILTIN_ANALYZERS:
        assert a.kind == AnalyzerKind.builtin
        assert a.builtin is True
        assert a.userId == "__system__"
        assert a.modalities


def test_custom_analyzer_defaults_to_custom_kind():
    a = Analyzer(userId="u1", name="My Analyzer")
    assert a.kind == AnalyzerKind.custom
    assert a.builtin is False
    assert a.id not in BUILTIN_ANALYZER_IDS
