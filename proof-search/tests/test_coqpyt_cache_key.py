"""Unit tests for stable CoqPyt imported-library cache keys."""

from coqpyt.coq.proof_file import _AuxFile


def test_cache_directory_is_versioned_and_legacy_cache_is_removed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / ".cache" / "coqpyt_cache"
    legacy.mkdir(parents=True)
    (legacy / "obsolete-key").write_bytes(b"obsolete")

    current = _AuxFile.get_coqpyt_disk_cache_loc()

    assert current is not None
    assert "1.1.0" in current
    assert not legacy.exists()


def test_cache_key_uses_stable_identity_not_private_workspace(
    tmp_path, monkeypatch
):
    library = tmp_path / "Library.v"
    library.write_text("Lemma cached : True. Proof. exact I. Qed.\n", encoding="utf-8")
    hashes = []

    def get_cached(cls, library_hash):
        hashes.append(library_hash)
        return {}

    monkeypatch.setattr(
        _AuxFile, "get_from_disk_cache", classmethod(get_cached)
    )

    for private_workspace in ("/tmp/private-one", "/tmp/private-two"):
        assert (
            _AuxFile.get_library(
                "Demo.Library",
                str(library),
                timeout=1,
                coq_lsp_options=None,
                workspace=private_workspace,
                cache_workspace="stable-project-identity",
                use_disk_cache=True,
            )
            == {}
        )

    assert hashes[0] == hashes[1]


def test_cache_key_changes_with_project_identity(tmp_path, monkeypatch):
    library = tmp_path / "Library.v"
    library.write_text("Definition cached := True.\n", encoding="utf-8")
    hashes = []

    def get_cached(cls, library_hash):
        hashes.append(library_hash)
        return {}

    monkeypatch.setattr(
        _AuxFile, "get_from_disk_cache", classmethod(get_cached)
    )

    for identity in ("project-a", "project-b"):
        _AuxFile.get_library(
            "Demo.Library",
            str(library),
            timeout=1,
            coq_lsp_options=None,
            workspace="/tmp/private",
            cache_workspace=identity,
            use_disk_cache=True,
        )

    assert hashes[0] != hashes[1]
