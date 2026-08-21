"""Regression tests for CoqPyt's Rocq 9 AST and core-library handling."""

from pathlib import Path

from coqpyt.coq.base_file import CoqFile
from coqpyt.coq.context import FileContext


def test_corelib_init_is_treated_as_a_core_library(tmp_path: Path):
    source = tmp_path / "Logic.v"
    source.write_text("(* core library fixture *)\n", encoding="utf-8")
    coq_file = object.__new__(CoqFile)

    coq_file._CoqFile__init_path(str(source), "Corelib.Init.Logic")

    copied_path = Path(coq_file._path)
    try:
        assert copied_path != source
        assert copied_path.read_text(encoding="utf-8") == source.read_text(
            encoding="utf-8"
        )
    finally:
        if copied_path != source:
            copied_path.unlink(missing_ok=True)


def test_rocq9_obligation_tags_with_identifiers(monkeypatch):
    monkeypatch.setattr(
        "coqpyt.coq.context.subprocess.check_output",
        lambda *_args, **_kwargs: b"The Rocq Prover, version 9.0.0\n",
    )

    context = FileContext("fixture.v")

    assert context.obligation_tag_with_id(0)
    assert context.obligation_tag_with_id(2)
    assert not context.obligation_tag_with_id(1)
    assert not context.obligation_tag_with_id(3)
