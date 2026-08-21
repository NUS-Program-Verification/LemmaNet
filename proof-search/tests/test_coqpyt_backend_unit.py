"""Adapter contract tests independent of a CoqPyt/Rocq version pairing."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.rocq.session import CoqPytSession
from backend.rocq.backend import CoqLibraryPath, CoqPytBackend
from backend.prover_backend import (
    Checkpoint, CommandKind, CommandRejectedError, HelperLemmaSpec,
    InvalidCheckpointError, InvalidLifecycleError, LifecycleState,
    ProverProtocolError, SourceLocation, TheoremIdentity,
)


def run(coroutine):
    return asyncio.run(coroutine)


class ScriptedCoqPytSession:
    """Small CoqPytSession double; it does not implement agent policy."""

    def __init__(self, file_path, **_options):
        self.file_path = Path(file_path)
        self.options = _options
        self.last_error = None
        self.commands = []
        self.proof_file = SimpleNamespace(current_goals=None)
        self.proof = SimpleNamespace(steps=[SimpleNamespace(text="Proof.")])
        self.closed = False
        self._refresh()

    def _goal(self, conclusion, hypotheses=()):
        hyps = [SimpleNamespace(names=[name], ty=type_text, definition=None)
                for name, type_text in hypotheses]
        return SimpleNamespace(ty=conclusion, hyps=hyps)

    def _refresh(self):
        states = [
            [self._goal("forall P Q : Prop, P /\\ Q -> Q /\\ P")],
            [self._goal("Q /\\ P", (("P", "Prop"), ("Q", "Prop"), ("H", "P /\\ Q")))],
            [self._goal("Q"), self._goal("P")],
            [self._goal("P")],
            [],
        ]
        config = SimpleNamespace(
            goals=states[len(self.commands)], stack=[], shelf=[], given_up=[]
        )
        self.proof_file.current_goals = SimpleNamespace(goals=config)

    def load(self, theorem_name):
        return theorem_name == "demo_theorem"

    def get_last_error(self):
        return self.last_error

    def get_context_terms(self):
        return {}

    def apply_tactic(self, command):
        command = command.strip()
        expected = ["intros P Q H.", "split.", "exact H.2.", "exact H.1."]
        if command == "Qed." and len(self.commands) == 4:
            text = self.file_path.read_text(encoding="utf-8")
            self.file_path.write_text(text.replace("Admitted.", "Qed."), encoding="utf-8")
            return True
        if len(self.commands) >= len(expected) or command != expected[len(self.commands)]:
            self.last_error = "scripted Rocq rejection"
            return False
        self.commands.append(command)
        self.proof.steps.append(SimpleNamespace(text=command))
        self._refresh()
        return True

    def get_current_step_number(self):
        return 1 + len(self.commands)

    def reset_by_step(self, step):
        self.commands = self.commands[:step - 1]
        self.proof.steps = self.proof.steps[:step]
        self._refresh()
        return True

    def search(self, command):
        return "and_comm : forall A B : Prop, A /\\ B -> B /\\ A"

    def close(self):
        self.closed = True


@pytest.fixture
def theorem(tmp_path):
    source = tmp_path / "DuneManagedGoal.v"
    source.write_text(
        "Lemma demo_theorem : forall P Q : Prop, P /\\ Q -> Q /\\ P.\n"
        "Proof. Admitted.\n", encoding="utf-8"
    )
    return TheoremIdentity(SourceLocation(source, tmp_path), "demo_theorem")


def test_rocq_backend_owns_native_command_syntax():
    backend = CoqPytBackend(session_factory=ScriptedCoqPytSession)
    commands = backend.helper_lemma_commands(HelperLemmaSpec("Hcomm", "P /\\ Q"))

    assert commands.declaration == "assert (Hcomm: P /\\ Q)"
    assert (commands.open_scope, commands.close_scope) == ("{", "}")
    assert backend.classify_command("admit.") is CommandKind.UNSOUND_COMPLETION
    assert backend.automation_command() == "hammer."


def test_adapter_contract(theorem, tmp_path):
    backend = CoqPytBackend(session_factory=ScriptedCoqPytSession)
    original = theorem.source.path.read_text(encoding="utf-8")
    with pytest.raises(InvalidLifecycleError):
        run(backend.state())
    state = run(backend.open(theorem))
    assert state.theorem == theorem and backend.lifecycle is LifecycleState.OPEN
    run(backend.apply("intros P Q H."))
    checkpoint = run(backend.checkpoint())
    before = run(backend.state())
    with pytest.raises(CommandRejectedError) as rejected:
        run(backend.apply("exact I."))
    assert rejected.value.state == before == run(backend.state())
    branched = run(backend.apply("split.")).state
    assert [goal.conclusion for goal in branched.goals] == ["Q", "P"]
    assert run(backend.rollback(checkpoint)).goals[0].conclusion == "Q /\\ P"
    assert "and_comm" in run(backend.query("Check and_comm.")).output
    run(backend.apply("split."))
    run(backend.apply("exact H.2."))
    assert run(backend.apply("exact H.1.")).state.is_complete
    destination = tmp_path / "saved.v"
    certificate = run(backend.save_proof(destination))
    assert certificate.commands[-1] == "Qed."
    assert "Qed." in destination.read_text(encoding="utf-8")
    assert theorem.source.path.read_text(encoding="utf-8") == original
    with pytest.raises(FileExistsError):
        run(backend.save_proof(destination))
    run(backend.save_proof(destination, overwrite=True))
    foreign = Checkpoint(object(), object(), 0)
    with pytest.raises(InvalidCheckpointError):
        run(backend.rollback(foreign))
    run(backend.close())
    run(backend.close())
    assert backend.lifecycle is LifecycleState.CLOSED

def test_coqpyt_session_selects_a_named_unproven_theorem():
    first = SimpleNamespace(text="Lemma first_theorem : True.")
    target = SimpleNamespace(
        text="Theorem demo_theorem : forall P : Prop, P -> P."
    )
    session = object.__new__(CoqPytSession)
    session.proof_file = SimpleNamespace(unproven_proofs=[first, target])

    assert session.get_unproven_proof() is first
    assert session.get_unproven_proof("demo_theorem") is target
    assert session.get_unproven_proof("missing") is None

class RejectedLoadCoqPytSession(ScriptedCoqPytSession):
    def load(self, theorem_name):
        self.last_error = f"theorem not found: {theorem_name}"
        return False


def test_partial_open_failure_closes_and_removes_working_copy(theorem):
    backend = CoqPytBackend(session_factory=RejectedLoadCoqPytSession)

    with pytest.raises(ProverProtocolError):
        run(backend.open(theorem))

    assert backend.lifecycle is LifecycleState.CLOSED
    assert not list(theorem.source.path.parent.glob("lemmanet_backend_*.v"))


def test_dune_managed_source_keeps_basename_and_library_mappings(theorem, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    backend = CoqPytBackend(
        session_factory=ScriptedCoqPytSession,
        library_paths=(CoqLibraryPath(library, "Example"),),
        coqproject_extra_options=("-arg -w",),
    )

    run(backend.open(theorem))
    session = backend._session
    assert session is not None
    working_path = session.file_path
    working_directory = working_path.parent
    assert working_path.name == theorem.source.path.name
    assert working_directory != theorem.source.path.parent
    assert session.options["workspace"] == str(working_directory)
    assert session.options["library_paths"] == [
        {"path": str(library.resolve()), "name": "Example"}
    ]
    assert session.options["auto_setup_coqproject"] is True
    assert session.options["coqproject_extra_options"] == ["-arg -w"]

    run(backend.close())
    assert not working_directory.exists()


def test_close_before_open_is_idempotent():
    backend = CoqPytBackend(session_factory=ScriptedCoqPytSession)
    run(backend.close())
    run(backend.close())
    assert backend.lifecycle is LifecycleState.CLOSED


def test_private_workspaces_share_stable_import_cache_identity(theorem, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    identities = []

    for _ in range(2):
        backend = CoqPytBackend(
            session_factory=ScriptedCoqPytSession,
            library_paths=(CoqLibraryPath(library, "Example"),),
            coqproject_extra_options=("-arg -w",),
        )
        run(backend.open(theorem))
        session = backend._session
        assert session is not None
        identities.append(session.options["cache_workspace"])
        assert str(session.file_path.parent) not in identities[-1]
        run(backend.close())

    assert identities[0] == identities[1]
    assert str(theorem.source.workspace.resolve()) in identities[0]


def test_completed_selected_proof_is_reset_only_in_private_copy(tmp_path):
    source = tmp_path / "Completed.v"
    source.write_text(
        "Lemma first : True.\nProof. exact I. Qed.\n\n"
        "Lemma demo_theorem : forall P Q : Prop, P /\\ Q -> Q /\\ P.\n"
        "Proof. intros P Q H. exact H. Admitted.\n",
        encoding="utf-8",
    )
    original = source.read_text(encoding="utf-8")
    identity = TheoremIdentity(
        SourceLocation(source, tmp_path), "demo_theorem"
    )
    backend = CoqPytBackend(session_factory=ScriptedCoqPytSession)

    run(backend.open(identity))
    session = backend._session
    assert session is not None
    working = session.file_path.read_text(encoding="utf-8")
    assert "Lemma first : True.\nProof. exact I. Qed." in working
    assert "Proof.\nAdmitted." in working
    assert "intros P Q H. exact H." not in working
    assert source.read_text(encoding="utf-8") == original
    run(backend.close())
