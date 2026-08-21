"""Contract and integration tests for the Lean backend against a real REPL."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.lean.backend import LeanReplBackend, default_repl_path
from backend.prover_backend import (
    CommandRejectedError, HelperLemmaSpec, InvalidCheckpointError, LifecycleState,
    SourceLocation, TheoremIdentity,
)

pytestmark = pytest.mark.integration

# The Lean project supplies Mathlib, which the fixture's tactics depend on.
LEAN_PROJECT = Path(
    os.environ.get("LEAN_PROJECT_ROOT", "/workspace/NTP4VC/data/why3")
)

SOURCE = """import Mathlib.Tactic
namespace lemmanet_contract
theorem first_theorem : True := trivial
theorem demo_theorem (P Q : Prop) (h : P ∧ Q) : Q ∧ P
  := sorry
end lemmanet_contract
"""


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def lean_theorem(tmp_path: Path) -> TheoremIdentity:
    if default_repl_path() is None:
        pytest.skip("the Lean REPL is required; run scripts/provers/setup_lean_repl.sh")
    if shutil.which("lake") is None:
        pytest.skip("lake is required for Lean integration tests")
    if not LEAN_PROJECT.is_dir():
        pytest.skip(f"the Lean project {LEAN_PROJECT} is required")
    source = tmp_path / "contract.lean"
    source.write_text(SOURCE, encoding="utf-8")
    return TheoremIdentity(SourceLocation(source, LEAN_PROJECT), "demo_theorem")


def test_real_backend_contract(lean_theorem: TheoremIdentity, tmp_path: Path):
    backend = LeanReplBackend(timeout=120)
    original = lean_theorem.source.path.read_text(encoding="utf-8")
    try:
        state = run(backend.open(lean_theorem))
        assert state.theorem == lean_theorem
        assert state.goals[0].conclusion == "Q ∧ P"
        assert [entry.name for entry in state.goals[0].hypotheses] == ["P", "Q", "h"]
        assert backend.lifecycle is LifecycleState.OPEN

        checkpoint = run(backend.checkpoint())

        before_rejection = run(backend.state())
        with pytest.raises(CommandRejectedError) as rejected:
            run(backend.apply("exact h"))
        assert "type mismatch" in rejected.value.feedback[0].message
        assert rejected.value.state == before_rejection
        assert run(backend.state()) == before_rejection

        with pytest.raises(CommandRejectedError, match="sorry"):
            run(backend.apply("sorry"))
        assert run(backend.state()) == before_rejection

        branched = run(backend.apply("constructor")).state
        assert [goal.conclusion for goal in branched.goals] == ["Q", "P"]
        assert branched.revision == 1

        restored = run(backend.rollback(checkpoint))
        assert [goal.conclusion for goal in restored.goals] == ["Q ∧ P"]
        assert restored.revision == 0

        query_state = run(backend.state())
        environment_query = run(backend.query("#check @And.symm"))
        assert "And.symm" in environment_query.output
        search_query = run(backend.query("exact?"))
        assert "Try this" in search_query.output
        # Neither query form advances the proof.
        assert run(backend.state()) == query_state

        helper = backend.helper_lemma_commands(HelperLemmaSpec("hq", "Q"))
        assert helper.close_scope == "guard_goal_nums 1"
        run(backend.apply(helper.declaration))
        run(backend.apply(helper.open_scope))
        with pytest.raises(CommandRejectedError):
            # The scope stays open while the helper goal is unproved.
            run(backend.apply(helper.close_scope))
        run(backend.apply("exact h.2"))
        run(backend.apply(helper.close_scope))

        complete = run(backend.apply("exact ⟨hq, h.1⟩")).state
        assert complete.is_complete
        assert backend.lifecycle is LifecycleState.COMPLETE

        destination = tmp_path / "saved_contract.lean"
        certificate = run(backend.save_proof(destination))
        assert certificate.format == "lean-source"
        written = destination.read_text(encoding="utf-8")
        assert "sorry" not in written
        assert lean_theorem.source.path.read_text(encoding="utf-8") == original

        replay = subprocess.run(
            ["lake", "env", "lean", str(destination)],
            cwd=LEAN_PROJECT, capture_output=True, text=True, timeout=900,
        )
        assert replay.returncode == 0, replay.stdout + replay.stderr
    finally:
        run(backend.close())
    assert backend.lifecycle is LifecycleState.CLOSED


def test_max_heartbeats_rejects_inside_lean(
    lean_theorem: TheoremIdentity, caplog
):
    backend = LeanReplBackend(timeout=120, max_heartbeats=1)
    try:
        before = run(backend.open(lean_theorem))
        with caplog.at_level("INFO"):
            with pytest.raises(CommandRejectedError, match="heartbeat"):
                run(backend.query("#reduce (List.range 10000).length"))
        assert run(backend.state()) == before
        assert "MITIGATION L3 heartbeat_rejection" in caplog.text
    finally:
        run(backend.close())


def test_foreign_checkpoint_is_rejected(lean_theorem: TheoremIdentity):
    backend = LeanReplBackend(timeout=120)
    other = LeanReplBackend(timeout=120)
    try:
        run(backend.open(lean_theorem))
        checkpoint = run(backend.checkpoint())
        run(other.open(lean_theorem))
        with pytest.raises(InvalidCheckpointError):
            run(other.rollback(checkpoint))
    finally:
        run(backend.close())
        run(other.close())


def test_close_is_safe_after_a_failed_open(tmp_path: Path):
    if default_repl_path() is None:
        pytest.skip("the Lean REPL is required; run scripts/provers/setup_lean_repl.sh")
    source = tmp_path / "broken.lean"
    source.write_text("theorem broken : Nonexistent := sorry\n", encoding="utf-8")
    backend = LeanReplBackend(timeout=120, use_lake=False)
    theorem = TheoremIdentity(SourceLocation(source, None), "broken")
    with pytest.raises(Exception):
        run(backend.open(theorem))
    assert backend.lifecycle is LifecycleState.CLOSED
    run(backend.close())
