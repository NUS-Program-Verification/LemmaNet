"""Contract scenarios required of every prover backend implementation."""

import asyncio
from pathlib import Path

import pytest

from tests.stub_backend import StubProverBackend, InvalidCheckpointError
from backend.prover_backend import (
    CommandKind, CommandRejectedError, HelperLemmaSpec,
    InvalidLifecycleError, LifecycleState, ProverBackendError, SourceLocation, TheoremIdentity,
)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def theorem(tmp_path: Path) -> TheoremIdentity:
    return TheoremIdentity(SourceLocation(tmp_path / "demo.stub", tmp_path), "demo_theorem")


def test_open_named_theorem_exposes_context_and_goal(theorem):
    backend = StubProverBackend()
    state = run(backend.open(theorem))
    assert state.theorem == theorem
    assert state.goals[0].conclusion == "P /\\ Q -> Q /\\ P"
    assert [entry.name for entry in state.context.local_entries] == ["P", "Q"]
    assert backend.lifecycle is LifecycleState.OPEN


def test_open_rejects_unknown_theorem_with_backend_error(theorem):
    backend = StubProverBackend()
    unknown = TheoremIdentity(theorem.source, "unknown_theorem")

    with pytest.raises(ProverBackendError, match="unknown theorem"):
        run(backend.open(unknown))
    assert backend.lifecycle is LifecycleState.CREATED


def test_native_command_policy_is_exposed_by_backend():
    backend = StubProverBackend()
    commands = backend.helper_lemma_commands(HelperLemmaSpec("Hcomm", "P /\\ Q"))

    assert commands.declaration == "assert (Hcomm: P /\\ Q)"
    assert backend.classify_command(commands.open_scope) is CommandKind.STRUCTURAL
    assert backend.classify_command("Admitted.") is CommandKind.UNSOUND_COMPLETION
    assert backend.classify_command("Abort.") is CommandKind.ABORT
    assert backend.automation_command() is None


def test_successful_tactic_and_rejection_is_atomic(theorem):
    backend = StubProverBackend()
    run(backend.open(theorem))
    result = run(backend.apply("intro H."))
    assert result.state.revision == 1
    before = run(backend.state())
    with pytest.raises(CommandRejectedError) as caught:
        run(backend.apply("nonsense."))
    assert caught.value.state == before
    assert run(backend.state()) == before


def test_tactic_can_produce_multiple_subgoals(theorem):
    backend = StubProverBackend()
    run(backend.open(theorem))
    run(backend.apply("intro H."))
    state = run(backend.apply("split.")).state
    assert [goal.conclusion for goal in state.goals] == ["Q", "P"]


def test_checkpoint_and_rollback(theorem):
    backend = StubProverBackend()
    run(backend.open(theorem))
    run(backend.apply("intro H."))
    checkpoint = run(backend.checkpoint())
    run(backend.apply("split."))
    restored = run(backend.rollback(checkpoint))
    assert restored.revision == 1
    assert [goal.conclusion for goal in restored.goals] == ["Q /\\ P"]
    foreign = StubProverBackend()
    run(foreign.open(theorem))
    with pytest.raises(InvalidCheckpointError):
        run(foreign.rollback(checkpoint))


def test_context_query_preserves_state(theorem):
    backend = StubProverBackend()
    before = run(backend.open(theorem))
    result = run(backend.query("Check and_comm."))
    assert result.output.startswith("and_comm :")
    assert run(backend.state()) == before


def complete(backend, theorem):
    run(backend.open(theorem))
    for command in ("intro H.", "split.", "exact H.2.", "exact H.1."):
        run(backend.apply(command))


def test_completion_is_part_of_proof_state(theorem):
    backend = StubProverBackend()
    complete(backend, theorem)
    assert run(backend.state()).is_complete
    assert backend.lifecycle is LifecycleState.COMPLETE
    with pytest.raises(InvalidLifecycleError):
        run(backend.apply("anything."))


def test_certificate_saving_and_overwrite(theorem, tmp_path):
    backend = StubProverBackend()
    complete(backend, theorem)
    destination = tmp_path / "demo.proof"
    certificate = run(backend.save_proof(destination))
    assert certificate.destination == destination
    assert certificate.commands[-1] == "exact H.1."
    assert destination.read_text(encoding="utf-8").startswith("intro H.\n")
    with pytest.raises(FileExistsError):
        run(backend.save_proof(destination))
    assert run(backend.save_proof(destination, overwrite=True)) == certificate


def test_invalid_lifecycle_calls_and_cleanup(theorem, tmp_path):
    backend = StubProverBackend()
    for operation in (
        backend.state, backend.checkpoint,
        lambda: backend.query("Check and_comm."),
        lambda: backend.apply("intro H."),
        lambda: backend.save_proof(tmp_path / "proof"),
    ):
        with pytest.raises(InvalidLifecycleError):
            run(operation())
    run(backend.open(theorem))
    with pytest.raises(InvalidLifecycleError):
        run(backend.open(theorem))
    run(backend.close())
    run(backend.close())
    assert backend.lifecycle is LifecycleState.CLOSED
    with pytest.raises(InvalidLifecycleError):
        run(backend.state())


def test_context_manager_closes_after_failure():
    backend = StubProverBackend()

    async def use_backend():
        with pytest.raises(RuntimeError):
            async with backend:
                raise RuntimeError("stop")

    run(use_backend())
    assert backend.lifecycle is LifecycleState.CLOSED
