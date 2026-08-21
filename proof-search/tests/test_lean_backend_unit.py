"""Lean backend behaviour against a scripted REPL, without starting Lean."""

import asyncio
import json
from pathlib import Path

import pytest

from backend.lean.backend import LeanReplBackend
from backend.lean.repl_session import LeanReplDrainTimeoutError
from backend.prover_backend import (
    CommandKind, CommandRejectedError, HelperLemmaSpec, InvalidCheckpointError,
    InvalidLifecycleError, LifecycleState, ProverProtocolError, ProverTimeoutError,
    SourceLocation, TheoremIdentity,
)

SOURCE = """namespace demo
theorem demo_theorem (P Q : Prop) (h : P ∧ Q) : Q ∧ P
  := sorry
end demo
"""

OPEN_ANSWER = {
    "env": 0,
    "messages": [{"severity": "warning", "data": "declaration uses 'sorry'"}],
    "sorries": [{
        "proofState": 0,
        "goal": "P Q : Prop\nh : P ∧ Q\n⊢ Q ∧ P",
        "pos": {"line": 3, "column": 5},
        "endPos": {"line": 3, "column": 10},
    }],
}


class FakeSession:
    """A Lean REPL stand-in that replays scripted answers."""

    def __init__(self, *args, script=None, **kwargs):
        self.script = dict(script or {})
        self.requests = []
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def request(self, payload, *, timeout=None):
        self.requests.append(dict(payload))
        if "cmd" in payload and payload.get("env") is None:
            return self.script.get("open", OPEN_ANSWER)
        if "cmd" in payload:
            command = payload["cmd"]
            if command.startswith("set_option maxHeartbeats ") and " in\n" in command:
                command = command.split(" in\n", 1)[1]
            return self.script.get(command, {"env": 99, "messages": []})
        key = payload["tactic"]
        if key.startswith("set_option maxHeartbeats ") and " in\n" in key:
            key = key.split(" in\n", 1)[1]
        if key not in self.script:
            return {"message": f"Lean error:\nunknown tactic {key}"}
        return self.script[key]

    def close(self):
        self.closed = True

    def diagnostics(self):
        return ""


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def theorem(tmp_path: Path) -> TheoremIdentity:
    source = tmp_path / "demo.lean"
    source.write_text(SOURCE, encoding="utf-8")
    return TheoremIdentity(SourceLocation(source, tmp_path), "demo_theorem")


def build(tmp_path: Path, script=None) -> LeanReplBackend:
    sessions = []

    def factory(*args, **kwargs):
        session = FakeSession(*args, script=script, **kwargs)
        sessions.append(session)
        return session

    repl = tmp_path / "repl"
    repl.write_text("", encoding="utf-8")
    backend = LeanReplBackend(repl_path=repl, session_factory=factory, use_lake=False)
    backend.sessions = sessions
    return backend


def test_open_exposes_goal_context_and_source_declarations(theorem, tmp_path):
    backend = build(tmp_path)
    state = run(backend.open(theorem))
    assert state.theorem == theorem
    assert state.goals[0].conclusion == "Q ∧ P"
    assert [entry.name for entry in state.context.local_entries] == ["P", "Q", "h"]
    assert backend.lifecycle is LifecycleState.OPEN
    # The whole file is elaborated in a fresh environment exactly once.
    assert backend.sessions[0].requests[0]["env"] is None
    run(backend.close())


def test_open_reports_elaboration_errors_and_closes(theorem, tmp_path):
    script = {"open": {"messages": [
        {"severity": "error", "data": "unknown identifier 'Foo'"}
    ]}}
    backend = build(tmp_path, script)
    with pytest.raises(ProverProtocolError, match="unknown identifier"):
        run(backend.open(theorem))
    assert backend.lifecycle is LifecycleState.CLOSED
    assert backend.sessions[0].closed


def test_open_rejects_a_theorem_that_is_absent(theorem, tmp_path):
    backend = build(tmp_path)
    unknown = TheoremIdentity(theorem.source, "absent_theorem")
    with pytest.raises(ProverProtocolError, match="no Lean declaration named"):
        run(backend.open(unknown))
    assert backend.lifecycle is LifecycleState.CLOSED


def test_apply_advances_and_rejection_leaves_state_unchanged(theorem, tmp_path):
    script = {"constructor": {
        "proofState": 1,
        "goals": ["case left\nP Q : Prop\nh : P ∧ Q\n⊢ Q",
                  "case right\nP Q : Prop\nh : P ∧ Q\n⊢ P"],
    }}
    backend = build(tmp_path, script)
    run(backend.open(theorem))
    result = run(backend.apply("constructor"))
    assert [goal.conclusion for goal in result.state.goals] == ["Q", "P"]
    assert result.state.revision == 1

    before = run(backend.state())
    with pytest.raises(CommandRejectedError) as rejected:
        run(backend.apply("exact h"))
    assert "unknown tactic" in rejected.value.feedback[0].message
    assert rejected.value.state == before
    assert run(backend.state()) == before
    run(backend.close())


def test_apply_rejects_a_tactic_that_leaves_a_sorry(theorem, tmp_path):
    script = {"sorry": {
        "proofState": 5, "goals": [],
        "proofStatus": "Incomplete: contains sorry",
        "sorries": [{"proofState": 4, "goal": "⊢ Q ∧ P"}],
    }}
    backend = build(tmp_path, script)
    before = run(backend.open(theorem))
    with pytest.raises(CommandRejectedError, match="sorry"):
        run(backend.apply("sorry"))
    assert run(backend.state()) == before
    assert backend.lifecycle is LifecycleState.OPEN
    run(backend.close())


def test_apply_maps_error_messages_to_rejection(theorem, tmp_path):
    script = {"omega": {"messages": [
        {"severity": "error", "data": "omega could not prove the goal",
         "pos": {"line": 1, "column": 0}}
    ]}}
    backend = build(tmp_path, script)
    run(backend.open(theorem))
    with pytest.raises(CommandRejectedError) as rejected:
        run(backend.apply("omega"))
    assert rejected.value.feedback[0].code == "1:0"
    run(backend.close())


def test_completion_moves_the_lifecycle_and_blocks_apply(theorem, tmp_path):
    script = {"exact ⟨h.2, h.1⟩": {"proofState": 1, "goals": [],
                                    "proofStatus": "Completed"}}
    backend = build(tmp_path, script)
    run(backend.open(theorem))
    state = run(backend.apply("exact ⟨h.2, h.1⟩")).state
    assert state.is_complete
    assert backend.lifecycle is LifecycleState.COMPLETE
    with pytest.raises(InvalidLifecycleError):
        run(backend.apply("rfl"))
    run(backend.close())


def test_checkpoint_and_rollback_reuse_the_persistent_proof_state(theorem, tmp_path):
    script = {
        "constructor": {"proofState": 1, "goals": ["⊢ Q", "⊢ P"]},
        "exact h.2": {"proofState": 2, "goals": ["⊢ P"]},
    }
    backend = build(tmp_path, script)
    run(backend.open(theorem))
    checkpoint = run(backend.checkpoint())
    run(backend.apply("constructor"))
    run(backend.apply("exact h.2"))
    restored = run(backend.rollback(checkpoint))
    assert restored.revision == 0
    assert [goal.conclusion for goal in restored.goals] == ["Q ∧ P"]

    # Rollback restores the identifier instead of replaying commands.
    run(backend.apply("constructor"))
    assert backend.sessions[0].requests[-1]["proofState"] == 0

    foreign = build(tmp_path, script)
    run(foreign.open(theorem))
    with pytest.raises(InvalidCheckpointError):
        run(foreign.rollback(checkpoint))
    run(backend.close())
    run(foreign.close())


def test_query_runs_environment_and_tactic_forms_without_advancing(theorem, tmp_path):
    script = {
        "#check @And.comm": {"env": 1, "messages": [
            {"severity": "info", "data": "@And.comm : ∀ {a b : Prop}, a ∧ b ↔ b ∧ a"}
        ]},
        "exact?": {"proofState": 42, "goals": [], "messages": [
            {"severity": "info", "data": "Try this: exact id (And.symm h)"}
        ]},
    }
    backend = build(tmp_path, script)
    before = run(backend.open(theorem))
    environment = run(backend.query("#check @And.comm"))
    assert "And.comm" in environment.output
    assert run(backend.state()) == before

    search = run(backend.query("exact?"))
    assert search.output.startswith("Try this:")
    # The proof state produced by the search is discarded.
    assert run(backend.state()) == before
    assert backend.lifecycle is LifecycleState.OPEN
    run(backend.close())


def test_query_failure_is_reported_without_changing_state(theorem, tmp_path):
    script = {"#check nonexistent": {"env": 1, "messages": [
        {"severity": "error", "data": "unknown identifier 'nonexistent'"}
    ]}}
    backend = build(tmp_path, script)
    before = run(backend.open(theorem))
    with pytest.raises(CommandRejectedError, match="unknown identifier"):
        run(backend.query("#check nonexistent"))
    assert run(backend.state()) == before
    assert backend.lifecycle is LifecycleState.OPEN
    run(backend.close())


def test_timeout_is_rejected_and_a_wedged_session_restarts_and_replays(
    theorem, tmp_path
):
    sessions = []

    class WedgedSession(FakeSession):
        def request(self, payload, *, timeout=None):
            self.requests.append(dict(payload))
            if "cmd" in payload and payload.get("env") is None:
                return OPEN_ANSWER
            tactic = payload.get("tactic", "")
            if tactic.endswith("constructor"):
                return {"proofState": 1, "goals": ["⊢ Q", "⊢ P"]}
            if tactic.endswith("hang"):
                raise ProverTimeoutError(
                    "the Lean REPL did not answer within 60 seconds"
                )
            raise LeanReplDrainTimeoutError("late answer never arrived", 60.0)

    class RecoveredSession(FakeSession):
        def request(self, payload, *, timeout=None):
            self.requests.append(dict(payload))
            if "cmd" in payload and payload.get("env") is None:
                return OPEN_ANSWER
            tactic = payload.get("tactic", "")
            if tactic.endswith("constructor"):
                return {"proofState": 1, "goals": ["⊢ Q", "⊢ P"]}
            if tactic.endswith("exact h.2"):
                return {"proofState": 2, "goals": ["⊢ P"]}
            return {"message": f"unexpected request: {payload}"}

    def factory(*args, **kwargs):
        cls = WedgedSession if not sessions else RecoveredSession
        session = cls(*args, **kwargs)
        sessions.append(session)
        return session

    repl = tmp_path / "repl"
    repl.write_text("", encoding="utf-8")
    backend = LeanReplBackend(
        repl_path=repl, session_factory=factory, use_lake=False
    )
    run(backend.open(theorem))
    run(backend.apply("constructor"))
    checkpoint = run(backend.checkpoint())

    before = run(backend.state())
    with pytest.raises(CommandRejectedError, match="timed out after 60") as rejected:
        run(backend.apply("hang"))
    assert rejected.value.state == before
    assert run(backend.state()) == before

    recovered = run(backend.apply("exact h.2")).state
    assert recovered.revision == 2
    assert len(sessions) == 2
    assert sessions[0].closed
    replayed = [request.get("tactic", "") for request in sessions[1].requests]
    assert any(command.endswith("constructor") for command in replayed)
    assert any(command.endswith("exact h.2") for command in replayed)
    with pytest.raises(InvalidCheckpointError):
        run(backend.rollback(checkpoint))
    run(backend.close())

def test_tactics_and_queries_are_bounded_by_max_heartbeats(
    theorem, tmp_path
):
    script = {
        "constructor": {"proofState": 1, "goals": ["⊢ Q", "⊢ P"]},
        "#check True": {"env": 1, "messages": []},
        "exact?": {"proofState": 2, "goals": ["⊢ Q"], "messages": []},
    }
    backend = build(tmp_path, script)
    backend._max_heartbeats = 200_000
    backend._query_max_heartbeats = 50_000
    run(backend.open(theorem))

    run(backend.apply("constructor"))
    tactic_request = backend.sessions[0].requests[-1]["tactic"]
    assert tactic_request == "set_option maxHeartbeats 200000 in\nconstructor"

    run(backend.query("#check True"))
    environment_request = backend.sessions[0].requests[-1]["cmd"]
    assert environment_request == "set_option maxHeartbeats 50000 in\n#check True"

    run(backend.query("exact?"))
    tactic_query = backend.sessions[0].requests[-1]["tactic"]
    assert tactic_query == "set_option maxHeartbeats 50000 in\nexact?"
    run(backend.close())


def test_heartbeat_exhaustion_is_logged_as_l3_rejection(
    theorem, tmp_path, caplog
):
    backend = build(tmp_path, {
        "simp": {"message": "Lean error:\nmaximum heartbeats exceeded"}
    })
    run(backend.open(theorem))
    with caplog.at_level("INFO"):
        with pytest.raises(CommandRejectedError, match="maximum heartbeats"):
            run(backend.apply("simp"))
    assert "MITIGATION L3 heartbeat_rejection" in caplog.text
    run(backend.close())


def test_helper_lemma_scope_tracks_the_current_goal_count(theorem, tmp_path):
    script = {"constructor": {"proofState": 1, "goals": ["⊢ Q", "⊢ P"]}}
    backend = build(tmp_path, script)
    run(backend.open(theorem))
    commands = backend.helper_lemma_commands(HelperLemmaSpec("hq", "Q"))
    assert commands.declaration == "have hq : Q"
    assert commands.open_scope == "skip"
    assert commands.close_scope == "guard_goal_nums 1"

    run(backend.apply("constructor"))
    nested = backend.helper_lemma_commands(HelperLemmaSpec("hp", "P"))
    assert nested.close_scope == "guard_goal_nums 2"
    run(backend.close())


def test_command_classification_and_automation(tmp_path):
    backend = build(tmp_path)
    assert backend.classify_command("sorry") is CommandKind.UNSOUND_COMPLETION
    assert backend.classify_command("exact h.1 <;> sorry") is CommandKind.UNSOUND_COMPLETION
    assert backend.classify_command("skip") is CommandKind.STRUCTURAL
    assert backend.classify_command("guard_goal_nums 2") is CommandKind.STRUCTURAL
    assert backend.classify_command("simp [Nat.add_comm]") is CommandKind.PROOF_STEP
    assert backend.automation_command() == "aesop"


def test_save_proof_writes_a_replayable_source(theorem, tmp_path):
    script = {"exact ⟨h.2, h.1⟩": {"proofState": 1, "goals": []}}
    backend = build(tmp_path, script)
    run(backend.open(theorem))
    run(backend.apply("exact ⟨h.2, h.1⟩"))
    destination = tmp_path / "out" / "demo_proof.lean"
    certificate = run(backend.save_proof(destination))

    assert certificate.format == "lean-source"
    assert certificate.commands == ("exact ⟨h.2, h.1⟩",)
    written = destination.read_text(encoding="utf-8")
    assert ":= by\n    exact ⟨h.2, h.1⟩" in written
    assert "sorry" not in written
    # The source proved against is never modified in place.
    assert theorem.source.path.read_text(encoding="utf-8") == SOURCE

    with pytest.raises(FileExistsError):
        run(backend.save_proof(destination))
    run(backend.save_proof(destination, overwrite=True))
    run(backend.close())


def test_invalid_lifecycle_calls_and_idempotent_close(theorem, tmp_path):
    backend = build(tmp_path)
    for operation in (
        backend.state, backend.checkpoint,
        lambda: backend.query("#check True"),
        lambda: backend.apply("rfl"),
        lambda: backend.save_proof(tmp_path / "proof.lean"),
    ):
        with pytest.raises(InvalidLifecycleError):
            run(operation())
    run(backend.open(theorem))
    with pytest.raises(InvalidLifecycleError):
        run(backend.open(theorem))
    run(backend.close())
    run(backend.close())
    assert backend.lifecycle is LifecycleState.CLOSED
    assert backend.sessions[0].closed
    with pytest.raises(InvalidLifecycleError):
        run(backend.state())


def test_save_proof_requires_completion(theorem, tmp_path):
    backend = build(tmp_path)
    run(backend.open(theorem))
    with pytest.raises(InvalidLifecycleError):
        run(backend.save_proof(tmp_path / "proof.lean"))
    run(backend.close())


def test_missing_repl_executable_is_reported_at_open(theorem, tmp_path):
    backend = LeanReplBackend(repl_path=tmp_path / "absent", use_lake=False)
    with pytest.raises(ProverProtocolError, match="not found"):
        run(backend.open(theorem))
    assert backend.lifecycle is LifecycleState.CLOSED


@pytest.mark.parametrize("command, answer, expected", [
    ("#find _ ∧ _ ↔ _ ∧ _", {"env": 1, "messages": [
        {"severity": "info", "data": "And.comm : a ∧ b ↔ b ∧ a"}]}, "And.comm"),
    ("#print And.comm", {"env": 1, "messages": [
        {"severity": "info", "data": "theorem And.comm : ∀ {a b : Prop}, a ∧ b ↔ b ∧ a"}]},
     "theorem And.comm"),
    ("#check @And.symm", {"env": 1, "messages": [
        {"severity": "info", "data": "@And.symm : ∀ {a b : Prop}, a ∧ b → b ∧ a"}]},
     "And.symm"),
    ("#print axioms And.comm", {"env": 1, "messages": [
        {"severity": "info", "data": "'And.comm' does not depend on any axioms"}]},
     "does not depend"),
    ("apply?", {"proofState": 9, "goals": [], "messages": [
        {"severity": "info", "data": "Try this: exact And.symm h"}]}, "Try this"),
])
def test_query_covers_the_rocq_query_surface(theorem, tmp_path, command, answer, expected):
    """Search, Print, About, Check, and axiom listing all go through query."""
    backend = build(tmp_path, {command: answer})
    before = run(backend.open(theorem))
    result = run(backend.query(command))
    assert expected in result.output
    assert run(backend.state()) == before
    run(backend.close())
