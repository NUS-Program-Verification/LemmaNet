"""Isabelle backend behaviour against a scripted prover, without Isabelle.

The backend reaches Isabelle through `isabelle_mcp`, so these tests install a
stand-in for that package's modules and drive the adapter through it.
"""

import asyncio
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from backend.prover_backend import (
    CommandKind, CommandRejectedError, HelperLemmaSpec, InvalidCheckpointError,
    InvalidLifecycleError, LifecycleState, ProverProtocolError, ProverTimeoutError,
    SourceLocation, TheoremIdentity,
)

SOURCE = """theory Demo
  imports Main
begin
theorem demo_theorem:
  fixes P Q :: bool
  shows "P \\<and> Q \\<longrightarrow> Q \\<and> P"
  sorry
end
"""


@dataclass
class FakeSnapshot:
    errors: list = field(default_factory=list)
    error_count: int = 0


@dataclass
class FakeView:
    status: str = "complete"
    message: str = "evaluated"
    files: list = field(default_factory=list)


@dataclass
class FakeGoalState:
    subgoals: list = field(default_factory=list)


class FakeIsabelleToolError(Exception):
    pass


class FakeProver:
    """Scripted Isabelle: maps a proof script to goals or an error."""

    def __init__(self):
        # proof script (tuple of commands) -> subgoals, or an error message
        self.script = {
            (): ["P ∧ Q ⟹ Q ∧ P"],
            ("apply (rule impI)",): ["P ∧ Q ⟹ Q ∧ P"],
            ("apply (rule impI)", "apply (rule conjI)"): [
                "P ∧ Q ⟹ Q", "P ∧ Q ⟹ P",
            ],
            ("apply (rule impI)", "apply auto"): [],
        }
        # Native Isabelle diagnostic commands and the output each produces.
        self.query_output = {
            'find_theorems "sorted (drop _ _)"':
                "found 1 theorem:\n  List.sorted_drop: sorted ?xs ⟹ sorted (drop ?n ?xs)",
            "thm conjI": "⟦?P; ?Q⟧ ⟹ ?P ∧ ?Q",
            "print_statement conjI": "theorem conjI: ⟦?P; ?Q⟧ ⟹ ?P ∧ ?Q",
            'term "drop 1 xs"': '"drop 1 xs" :: "\'a list"',
            'typ "int list"': '"int list"',
            'find_consts "int => int"': "found 2 constants",
        }
        self.clients = []
        self.evaluated = []
        self.queried = []
        self.cancelled = []
        self.fail_next_evaluation_for: tuple[str, ...] | None = None

    def install(self, monkeypatch):
        prover = self

        class FakeClient:
            def __init__(self, logic="HOL", session_dirs=()):
                self.logic, self.session_dirs = logic, list(session_dirs)
                self.started = self.initialized = self.shut_down = False
                self.killed = False
                prover.clients.append(self)

            async def start(self):
                self.started = True

            async def initialize(self):
                self.initialized = True

            async def shutdown(self):
                self.shut_down = True

            def kill(self):
                self.killed = True

        def commands_of(path):
            """Recover the applied script from the working copy on disk."""
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            body = [
                line.strip() for line in lines[6:]
                if line.startswith("  ") and line.strip() not in {"", "sorry"}
            ]
            return tuple(body)

        async def sync_file_locked(client, path):
            return None

        async def evaluate_to(client, path, line):
            key = commands_of(path)
            prover.evaluated.append(key)
            if key == prover.fail_next_evaluation_for:
                prover.fail_next_evaluation_for = None
                raise FakeIsabelleToolError(
                    "The Isabelle server exited before the LSP handshake."
                )
            if key and key[-1] in prover.query_output:
                # A diagnostic command evaluates cleanly on top of the proof.
                return FakeView(files=[FakeSnapshot()])
            if key in prover.script:
                return FakeView(files=[FakeSnapshot()])
            return FakeView(
                message=f"Undefined tactic in {key[-1] if key else ''}",
                files=[FakeSnapshot(errors=[(line, line)], error_count=1)],
            )

        async def cancel_evaluation(client):
            prover.cancelled.append(True)
            return FakeView(status="cancelled")

        async def goal(client, path, line):
            key = commands_of(path)
            if key and key[-1] in prover.query_output:
                key = key[:-1]
            return FakeGoalState(subgoals=list(prover.script.get(key, [])))

        async def command_output(client, path, line):
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            queried = lines[line - 1].strip()
            prover.queried.append(queried)
            text = prover.query_output.get(queried)
            messages = [types.SimpleNamespace(kind="normal", message=text)] if text else []
            return types.SimpleNamespace(messages=messages)

        package = types.ModuleType("isabelle_mcp")
        package.component = types.SimpleNamespace(ensure_component=lambda: None)
        lsp = types.ModuleType("isabelle_mcp.lsp_client")
        lsp.IsabelleLSPClient = FakeClient
        evaluation = types.ModuleType("isabelle_mcp.evaluation")
        evaluation.evaluate_to = evaluate_to
        evaluation.sync_file_locked = sync_file_locked
        evaluation.cancel_evaluation = cancel_evaluation
        tools = types.ModuleType("isabelle_mcp.tools")
        goal_module = types.ModuleType("isabelle_mcp.tools.goal")
        goal_module.goal = goal
        output_module = types.ModuleType("isabelle_mcp.tools.command_output")
        output_module.command_output = command_output
        utils = types.ModuleType("isabelle_mcp.utils")
        utils.MCPLine = int
        utils_core = types.ModuleType("isabelle_mcp.utils.core")
        utils_core.IsabelleToolError = FakeIsabelleToolError
        for name, module in {
            "isabelle_mcp": package,
            "isabelle_mcp.lsp_client": lsp,
            "isabelle_mcp.evaluation": evaluation,
            "isabelle_mcp.tools": tools,
            "isabelle_mcp.tools.goal": goal_module,
            "isabelle_mcp.tools.command_output": output_module,
            "isabelle_mcp.utils": utils,
            "isabelle_mcp.utils.core": utils_core,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)
        return FakeClient


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def prover(monkeypatch):
    fake = FakeProver()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def theorem(tmp_path: Path) -> TheoremIdentity:
    source = tmp_path / "Demo.thy"
    source.write_text(SOURCE, encoding="utf-8")
    return TheoremIdentity(SourceLocation(source, tmp_path), "demo_theorem")


def build():
    from backend.isabelle.backend import IsabelleBackend
    return IsabelleBackend(logic="HOL", timeout=30)


def test_open_exposes_the_initial_goal_and_preserves_the_source(prover, theorem):
    backend = build()
    state = run(backend.open(theorem))
    assert state.theorem == theorem
    assert [goal.conclusion for goal in state.goals] == ["P ∧ Q ⟹ Q ∧ P"]
    assert backend.lifecycle is LifecycleState.OPEN
    assert prover.clients[0].started and prover.clients[0].initialized
    assert theorem.source.path.read_text(encoding="utf-8") == SOURCE
    run(backend.close())


def test_open_rejects_a_theorem_that_is_absent(prover, theorem):
    backend = build()
    unknown = TheoremIdentity(theorem.source, "absent_theorem")
    with pytest.raises(ProverProtocolError, match="no Isabelle theorem named"):
        run(backend.open(unknown))
    # The name is checked in the source text, so no prover was started and
    # nothing needs releasing; the backend can still be opened correctly.
    assert backend.lifecycle is LifecycleState.CREATED
    assert prover.clients == []
    state = run(backend.open(theorem))
    assert [goal.conclusion for goal in state.goals] == ["P ∧ Q ⟹ Q ∧ P"]
    run(backend.close())


def test_open_closes_the_prover_when_startup_fails(prover, theorem, monkeypatch):
    backend = build()

    async def failing_start(self):
        raise RuntimeError("prover did not start")

    monkeypatch.setattr(
        sys.modules["isabelle_mcp.lsp_client"].IsabelleLSPClient,
        "start", failing_start,
    )
    with pytest.raises(RuntimeError, match="did not start"):
        run(backend.open(theorem))
    assert backend.lifecycle is LifecycleState.CLOSED


def test_apply_advances_and_branches(prover, theorem):
    backend = build()
    run(backend.open(theorem))
    first = run(backend.apply("apply (rule impI)")).state
    assert first.revision == 1
    branched = run(backend.apply("apply (rule conjI)")).state
    assert [goal.conclusion for goal in branched.goals] == ["P ∧ Q ⟹ Q", "P ∧ Q ⟹ P"]
    run(backend.close())


def test_apply_restarts_a_dead_prover_and_replays_the_prefix(
    prover, theorem, caplog
):
    backend = build()
    run(backend.open(theorem))
    run(backend.apply("apply (rule impI)"))
    prover.fail_next_evaluation_for = (
        "apply (rule impI)", "apply (rule conjI)"
    )

    with caplog.at_level("INFO"):
        branched = run(backend.apply("apply (rule conjI)")).state

    assert [goal.conclusion for goal in branched.goals] == [
        "P ∧ Q ⟹ Q", "P ∧ Q ⟹ P",
    ]
    assert len(prover.clients) == 2
    assert prover.clients[0].shut_down
    assert "MITIGATION I3 evaluation_restart" in caplog.text
    run(backend.close())


def test_rejection_is_atomic_and_restores_the_working_copy(prover, theorem):
    backend = build()
    run(backend.open(theorem))
    run(backend.apply("apply (rule impI)"))
    before = run(backend.state())

    with pytest.raises(CommandRejectedError) as rejected:
        run(backend.apply("apply nonsense"))
    assert rejected.value.state == before
    assert run(backend.state()) == before
    # The failed command is evaluated, then the accepted prefix is restored.
    assert prover.evaluated[-2] == ("apply (rule impI)", "apply nonsense")
    assert prover.evaluated[-1] == ("apply (rule impI)",)
    run(backend.close())


def test_apply_timeout_is_cancelled_restored_and_rejected(
    prover, theorem, monkeypatch, caplog
):
    backend = build()
    run(backend.open(theorem))
    run(backend.apply("apply (rule impI)"))
    before = run(backend.state())
    evaluate = backend._evaluate

    async def timeout_once(commands, revision):
        if commands and commands[-1] == "apply slow":
            raise ProverTimeoutError("evaluation timed out")
        return await evaluate(commands, revision)

    monkeypatch.setattr(backend, "_evaluate", timeout_once)
    with caplog.at_level("INFO"):
        with pytest.raises(CommandRejectedError, match="tactic timed out"):
            run(backend.apply("apply slow"))

    assert prover.cancelled == [True]
    assert prover.evaluated[-1] == ("apply (rule impI)",)
    assert run(backend.state()) == before
    assert backend.lifecycle is LifecycleState.OPEN
    assert "MITIGATION I2 timeout_rejection" in caplog.text
    run(backend.close())


def test_an_error_past_the_proof_is_not_a_rejection(prover, theorem):
    """An unfinished proof also fails the theory's `end`; that is not the tactic's fault."""
    backend = build()
    run(backend.open(theorem))

    evaluation = sys.modules["isabelle_mcp.evaluation"]
    accepted = evaluation.evaluate_to

    async def failing_tail(client, path, line):
        view = await accepted(client, path, line)
        # `end` sits one line past the proof and fails while the proof is open.
        view.files.append(FakeSnapshot(errors=[(line + 1, line + 1)], error_count=1))
        return view

    evaluation.evaluate_to = failing_tail
    try:
        state = run(backend.apply("apply (rule impI)")).state
        assert state.revision == 1
    finally:
        evaluation.evaluate_to = accepted
    run(backend.close())


def test_completion_moves_the_lifecycle_and_blocks_apply(prover, theorem):
    backend = build()
    run(backend.open(theorem))
    run(backend.apply("apply (rule impI)"))
    complete = run(backend.apply("apply auto")).state
    assert complete.is_complete
    assert backend.lifecycle is LifecycleState.COMPLETE
    with pytest.raises(InvalidLifecycleError):
        run(backend.apply("done"))
    run(backend.close())


def test_checkpoint_and_rollback_reevaluate_the_shorter_proof(prover, theorem):
    backend = build()
    run(backend.open(theorem))
    run(backend.apply("apply (rule impI)"))
    checkpoint = run(backend.checkpoint())
    run(backend.apply("apply (rule conjI)"))

    restored = run(backend.rollback(checkpoint))
    assert restored.revision == 1
    assert [goal.conclusion for goal in restored.goals] == ["P ∧ Q ⟹ Q ∧ P"]
    assert prover.evaluated[-1] == ("apply (rule impI)",)

    foreign = build()
    run(foreign.open(theorem))
    with pytest.raises(InvalidCheckpointError):
        run(foreign.rollback(checkpoint))
    run(backend.close())
    run(foreign.close())


@pytest.mark.parametrize("command, expected", [
    ('find_theorems "sorted (drop _ _)"', "List.sorted_drop"),
    ("thm conjI", "?P ∧ ?Q"),
    ("print_statement conjI", "theorem conjI"),
    ('term "drop 1 xs"', "drop 1 xs"),
    ('typ "int list"', "int list"),
    ('find_consts "int => int"', "2 constants"),
])
def test_query_runs_any_diagnostic_command(prover, theorem, command, expected):
    """The query surface matches Rocq's Search/Print/Locate/About/Check."""
    backend = build()
    before = run(backend.open(theorem))
    result = run(backend.query(command))
    assert expected in result.output
    assert prover.queried[-1] == command
    # The query is removed again, so the proof is untouched.
    assert run(backend.state()) == before
    assert prover.evaluated[-1] == ()
    run(backend.close())


def test_query_reports_an_invalid_command_and_restores_the_proof(prover, theorem):
    backend = build()
    run(backend.open(theorem))
    run(backend.apply("apply (rule impI)"))
    before = run(backend.state())
    with pytest.raises(CommandRejectedError):
        run(backend.query("thm no_such_fact"))
    assert run(backend.state()) == before
    assert prover.evaluated[-1] == ("apply (rule impI)",)
    run(backend.close())


def test_query_restarts_a_dead_prover_and_restores_the_proof(
    prover, theorem, caplog
):
    backend = build()
    run(backend.open(theorem))
    run(backend.apply("apply (rule impI)"))
    before = run(backend.state())
    prover.fail_next_evaluation_for = ("apply (rule impI)",)

    with caplog.at_level("INFO"):
        result = run(backend.query("thm conjI"))

    assert "?P ∧ ?Q" in result.output
    assert run(backend.state()) == before
    assert len(prover.clients) == 2
    assert prover.clients[0].shut_down
    assert prover.clients[1].started
    assert prover.clients[1].initialized
    assert "MITIGATION I3 query_restart" in caplog.text
    run(backend.close())


def test_command_classification_and_automation(prover, theorem):
    backend = build()
    assert backend.classify_command("sorry") is CommandKind.UNSOUND_COMPLETION
    assert backend.classify_command("oops") is CommandKind.UNSOUND_COMPLETION
    assert backend.classify_command("next") is CommandKind.STRUCTURAL
    assert backend.classify_command("apply auto") is CommandKind.PROOF_STEP
    assert backend.automation_command() == "apply auto"

    commands = backend.helper_lemma_commands(HelperLemmaSpec("h", "P ⟹ Q"))
    assert commands.declaration == 'apply (subgoal_tac "P ⟹ Q")'


def test_save_proof_closes_the_script_and_preserves_the_source(prover, theorem, tmp_path):
    backend = build()
    run(backend.open(theorem))
    run(backend.apply("apply (rule impI)"))
    run(backend.apply("apply auto"))

    destination = tmp_path / "out" / "Proved.thy"
    certificate = run(backend.save_proof(destination))
    assert certificate.format == "isabelle-source"
    assert certificate.commands == ("apply (rule impI)", "apply auto", "done")
    written = destination.read_text(encoding="utf-8")
    assert "sorry" not in written
    assert "  done" in written
    # Isabelle requires the theory name to match the file name, so the
    # certificate is named after its destination, never the working copy.
    assert written.startswith("theory Proved")
    assert "LemmaNetWorking" not in written
    assert theorem.source.path.read_text(encoding="utf-8") == SOURCE

    with pytest.raises(FileExistsError):
        run(backend.save_proof(destination))
    run(backend.save_proof(destination, overwrite=True))

    # Saving under the original file name reproduces the original header.
    original_name = tmp_path / "out" / "Demo.thy"
    run(backend.save_proof(original_name))
    assert original_name.read_text(encoding="utf-8").startswith("theory Demo")
    run(backend.close())


def test_invalid_lifecycle_calls_and_idempotent_close(prover, theorem, tmp_path):
    backend = build()
    for operation in (
        backend.state, backend.checkpoint,
        lambda: backend.query("sorted"),
        lambda: backend.apply("apply auto"),
        lambda: backend.save_proof(tmp_path / "proof.thy"),
    ):
        with pytest.raises(InvalidLifecycleError):
            run(operation())
    run(backend.open(theorem))
    with pytest.raises(InvalidLifecycleError):
        run(backend.open(theorem))
    run(backend.close())
    run(backend.close())
    assert backend.lifecycle is LifecycleState.CLOSED
    assert prover.clients[0].shut_down
    with pytest.raises(InvalidLifecycleError):
        run(backend.state())


def test_open_copies_relative_sibling_imports_transitively(
    prover, tmp_path
):
    source = tmp_path / "Demo.thy"
    source.write_text(
        SOURCE.replace("imports Main", 'imports "./Helper"'),
        encoding="utf-8",
    )
    helper = tmp_path / "Helper.thy"
    helper.write_text(
        'theory Helper\n  imports "./Nested"\nbegin\nend\n',
        encoding="utf-8",
    )
    nested = tmp_path / "Nested.thy"
    nested.write_text(
        "theory Nested\n  imports Main\nbegin\nend\n",
        encoding="utf-8",
    )
    identity = TheoremIdentity(
        SourceLocation(source, tmp_path), "demo_theorem"
    )
    backend = build()
    run(backend.open(identity))
    working_directory = backend._working_path.parent
    assert (working_directory / "Helper.thy").read_text(encoding="utf-8") == (
        helper.read_text(encoding="utf-8")
    )
    assert (working_directory / "Nested.thy").read_text(encoding="utf-8") == (
        nested.read_text(encoding="utf-8")
    )
    assert source.read_text(encoding="utf-8").startswith("theory Demo")
    run(backend.close())


def test_close_releases_the_working_directory(prover, theorem):
    backend = build()
    run(backend.open(theorem))
    working = backend._working_path
    assert working is not None and working.is_file()
    run(backend.close())
    assert not working.exists()


def test_open_waits_for_a_slow_evaluation_to_finish(prover, theorem, monkeypatch):
    """A view still `in_progress` after `evaluate_to` must be polled, not used.

    On a real prover a heavy import graph outlasts `evaluate_to`'s own polling
    budget; it then returns status "in_progress" with the module-level
    evaluation guard still armed, and the next tool call fails with
    "Evaluation in progress". Observed on NTP4VC `div_Div_div_sb_qrqtvc`.
    """
    evaluation = sys.modules["isabelle_mcp.evaluation"]
    accepted = evaluation.evaluate_to
    polls = []

    async def slow_evaluate(client, path, line):
        slow_evaluate.final = await accepted(client, path, line)
        return FakeView(status="in_progress")

    async def evaluation_status(client):
        polls.append(True)
        if len(polls) < 2:
            return FakeView(status="in_progress")
        return slow_evaluate.final

    evaluation.evaluate_to = slow_evaluate
    evaluation.evaluation_status = evaluation_status
    monkeypatch.setattr("backend.isabelle.backend._EVALUATION_POLL_SECONDS", 0.01)
    backend = build()
    try:
        state = run(backend.open(theorem))
        assert [goal.conclusion for goal in state.goals] == ["P ∧ Q ⟹ Q ∧ P"]
        assert len(polls) >= 2
    finally:
        evaluation.evaluate_to = accepted
        run(backend.close())


def test_a_never_finishing_evaluation_times_out(prover, theorem, monkeypatch):
    from backend.isabelle.backend import IsabelleBackend
    from backend.prover_backend import ProverTimeoutError

    evaluation = sys.modules["isabelle_mcp.evaluation"]
    accepted = evaluation.evaluate_to

    async def never_done(client, path, line):
        return FakeView(status="in_progress")

    async def still_running(client):
        return FakeView(status="in_progress")

    evaluation.evaluate_to = never_done
    evaluation.evaluation_status = still_running
    monkeypatch.setattr("backend.isabelle.backend._EVALUATION_POLL_SECONDS", 0.01)
    backend = IsabelleBackend(logic="HOL", timeout=1)
    try:
        with pytest.raises(ProverTimeoutError):
            run(backend.open(theorem))
        assert backend.lifecycle is LifecycleState.CLOSED
    finally:
        evaluation.evaluate_to = accepted


def test_saved_certificate_requalifies_working_theory_facts(prover, theorem, tmp_path):
    """Facts qualified with the working theory's name must follow the rename.

    Commands are accepted inside the `LemmaNetWorking` working copy, so the
    model may cite session facts as `LemmaNetWorking.<fact>`. The certificate
    is renamed after its destination; without requalifying those references it
    cannot replay. Observed on NTP4VC `imp_SymStateSet_of_list_to_listqtvc`.
    """
    qualified = "apply (simp add: LemmaNetWorking.mk'spec)"
    prover.script[(qualified,)] = []
    backend = build()
    run(backend.open(theorem))
    run(backend.apply(qualified))
    destination = tmp_path / "Renamed.thy"
    run(backend.save_proof(destination))
    text = destination.read_text(encoding="utf-8")
    assert "LemmaNetWorking." not in text
    assert "apply (simp add: Renamed.mk'spec)" in text
    run(backend.close())
