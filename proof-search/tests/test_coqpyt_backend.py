"""Contract and integration tests for the Rocq backend."""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.rocq.backend import CoqPytBackend
from backend.prover_backend import (
    CommandRejectedError, LifecycleState,
    SourceLocation, TheoremIdentity,
)

pytestmark = pytest.mark.integration


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def rocq_theorem(tmp_path: Path) -> TheoremIdentity:
    if shutil.which("coq-lsp") is None or shutil.which("coqc") is None:
        pytest.skip("coq-lsp and coqc are required for Rocq integration tests")
    source = tmp_path / "contract.v"
    source.write_text(
        "Require Import Coq.Unicode.Utf8.\n"
        "Require Import List.\n"
        "Require Import ZArith.\n"
        "From Coq Require Import ZArith Lia.\n\n"
        "Lemma first_theorem : True.\nProof. exact I. Qed.\n\n"
        "Lemma demo_theorem : forall P Q : Prop, P /\\ Q -> Q /\\ P.\n"
        "Proof. Admitted.\n",
        encoding="utf-8",
    )
    return TheoremIdentity(SourceLocation(source, tmp_path), "demo_theorem")


def test_rollback_without_proof_marker(tmp_path: Path):
    """NTP4VC-generated obligations state the theorem and immediately write
    `Admitted.` with no `Proof.` command; rollback must still restore the
    checkpoint instead of failing to find the marker."""
    if shutil.which("coq-lsp") is None:
        pytest.skip("coq-lsp is required for Rocq integration tests")
    source = tmp_path / "no_marker.v"
    source.write_text(
        "Theorem no_marker : True /\\ True.\nAdmitted.\n", encoding="utf-8"
    )
    theorem = TheoremIdentity(SourceLocation(source, tmp_path), "no_marker")
    backend = CoqPytBackend(timeout=30)
    try:
        state = run(backend.open(theorem))
        assert len(state.goals) == 1
        checkpoint = run(backend.checkpoint())
        branched = run(backend.apply("split.")).state
        assert [goal.conclusion for goal in branched.goals] == ["True", "True"]
        restored = run(backend.rollback(checkpoint))
        assert len(restored.goals) == 1
        assert restored.revision == 0
        complete = run(backend.apply("split; exact I.")).state
        assert complete.is_complete
    finally:
        run(backend.close())


def test_real_backend_contract(rocq_theorem: TheoremIdentity, tmp_path: Path):
    backend = CoqPytBackend(timeout=30)
    original = rocq_theorem.source.path.read_text(encoding="utf-8")
    try:
        state = run(backend.open(rocq_theorem))
        assert state.theorem == rocq_theorem
        assert "∀" in state.goals[0].conclusion or "forall" in state.goals[0].conclusion
        assert backend.lifecycle is LifecycleState.OPEN

        result = run(backend.apply("intros P Q H."))
        assert result.state.goals[0].conclusion in {"Q /\\ P", "Q ∧ P"}
        checkpoint = run(backend.checkpoint())

        rejected_state = run(backend.state())
        with pytest.raises(CommandRejectedError) as rejected:
            run(backend.apply("exact I."))
        assert rejected.value.state == rejected_state
        assert run(backend.state()) == rejected_state

        split = run(backend.apply("split.")).state
        assert [goal.conclusion for goal in split.goals] == ["Q", "P"]
        restored = run(backend.rollback(checkpoint))
        assert [goal.conclusion for goal in restored.goals] in [["Q /\\ P"], ["Q ∧ P"]]

        query_state = run(backend.state())
        query = run(backend.query("Check and_comm."))
        assert "and_comm" in query.output
        assert run(backend.state()) == query_state

        complete = run(
            backend.apply("destruct H as [HP HQ]; split; assumption.")
        ).state
        assert complete.is_complete
        assert backend.lifecycle is LifecycleState.COMPLETE

        destination = tmp_path / "saved_contract.v"
        certificate = run(backend.save_proof(destination))
        assert certificate.commands[-1] == "Qed."
        assert "Admitted." not in destination.read_text(encoding="utf-8")
        subprocess.run(["coqc", str(destination)], check=True, cwd=tmp_path)
        with pytest.raises(FileExistsError):
            run(backend.save_proof(destination))
        run(backend.save_proof(destination, overwrite=True))
        assert rocq_theorem.source.path.read_text(encoding="utf-8") == original
    finally:
        run(backend.close())
    assert backend.lifecycle is LifecycleState.CLOSED
    run(backend.close())
