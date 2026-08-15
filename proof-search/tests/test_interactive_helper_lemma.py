"""End-to-end tests for helper lemmas driven from the interactive REPL.

Exercises the ProofController lifecycle (open / close / abandon) through the
InteractiveSessionManager commands, against a real Coq session.
"""

import sys
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent import visualizer
from agent.context_manager import ContextManager
from agent.interactive_session import InteractiveSessionManager
from agent.proof_controller import ProofController
from backend.coq_interface import CoqInterface


TEST_CONTENT = """Definition is_sint32 (x : nat) : Prop :=
  x = x.

Lemma main :
  forall i : nat, is_sint32 i.
Proof.
Admitted.
"""

HELPER_STATEMENT = "forall x : nat, is_sint32 x"
HELPER_TACTICS = ["intros x.", "unfold is_sint32.", "reflexivity."]


@pytest.fixture
def session(tmp_path, monkeypatch):
    """An initialized interactive session on a fresh proof, with a clean history."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    coq_file = tmp_path / "interactive_helper_lemma.v"
    coq_file.write_text(TEST_CONTENT, encoding="utf-8")

    coq_interface = CoqInterface(
        file_path=str(coq_file),
        workspace=str(tmp_path),
        timeout=20,
    )
    assert coq_interface.load(), coq_interface.get_last_error()

    context_manager = ContextManager(
        coq_interface=coq_interface,
        history_file=str(tmp_path / "tactic_history.json"),
        enable_context_search=False,
    )
    controller = ProofController(
        coq_interface=coq_interface,
        context_manager=context_manager,
        enable_recording=False,
        history_file=str(tmp_path / "tactic_history.json"),
    )
    manager = InteractiveSessionManager(controller)
    # No theorem name, exactly as `main.py` calls it without --theorem
    assert controller._init_proof_session()

    try:
        yield manager
    finally:
        coq_interface.close()


def _tactics(controller):
    return [t["tactic"] for t in controller._tactics_with_states]


def test_theorem_name_is_read_from_the_file(session):
    # No name was passed to _init_proof_session, so it must come from the
    # statement itself rather than falling back to 'unnamed'.
    assert session.controller.current_theorem_name == "main"


def test_supplied_theorem_name_wins(session):
    assert session.controller._init_proof_session("explicit_name")
    assert session.controller.current_theorem_name == "explicit_name"


def test_lemma_command_opens_subproof(session):
    controller = session.controller
    session._do_lemma(f"Hhelper: {HELPER_STATEMENT}")

    assert _tactics(controller) == [f"assert (Hhelper: {HELPER_STATEMENT})", "{"]
    assert len(controller.helper_lemma_stack) == 1

    open_lemmas = controller.helper_lemma_context()
    assert len(open_lemmas) == 1
    assert open_lemmas[0]["name"] == "Hhelper"
    assert open_lemmas[0]["statement"] == HELPER_STATEMENT
    assert session._prompt() == "lemmanet[Hhelper]> "


def test_unnamed_lemma_gets_generated_name(session):
    controller = session.controller
    session._do_lemma(HELPER_STATEMENT)

    open_lemmas = controller.helper_lemma_context()
    assert len(open_lemmas) == 1
    assert open_lemmas[0]["name"], "a name should have been generated"
    assert open_lemmas[0]["statement"] == HELPER_STATEMENT


def test_subproof_closes_and_is_recorded(session):
    controller = session.controller
    session._do_lemma(f"Hhelper: {HELPER_STATEMENT}")

    for tactic in HELPER_TACTICS:
        session._do_user_tactic(tactic)

    # Closing brace applied, back in the parent proof
    assert controller.helper_lemma_stack == []
    assert controller.helper_lemma_context() == []
    assert _tactics(controller)[-1] == "}"
    assert session._prompt() == "lemmanet> "

    # Recorded as a reusable unit
    entries = controller.tactic_history.helper_lemma_entries
    assert len(entries) == 1
    assert entries[0].name == "Hhelper"
    assert entries[0].proof_tactics == HELPER_TACTICS + ["}"]

    # The lemma is usable in the parent proof
    session._do_user_tactic("exact Hhelper.")
    assert controller.is_successful


def test_recorded_lemma_is_replayed_from_cache(session):
    controller = session.controller

    session._do_lemma(f"Hhelper: {HELPER_STATEMENT}")
    for tactic in HELPER_TACTICS:
        session._do_user_tactic(tactic)
    assert len(controller.tactic_history.helper_lemma_entries) == 1

    # Roll the whole thing back, then propose the identical lemma again
    session._do_rollback(len(controller._tactics_with_states))
    assert controller._tactics_with_states == []

    result = controller.open_helper_lemma(
        f"assert (Hhelper: {HELPER_STATEMENT})", source="user"
    )
    assert result["success"]
    assert result["replayed"], "cached proof should have been replayed"
    assert controller.helper_lemma_stack == []
    assert _tactics(controller)[-1] == "}"


def test_drop_removes_assert_and_brace(session):
    controller = session.controller
    session._do_user_tactic("intros i.")
    before = _tactics(controller)

    session._do_lemma(f"Hhelper: {HELPER_STATEMENT}")
    session._do_user_tactic("intros x.")
    assert len(controller.helper_lemma_context()) == 1

    session._do_drop()

    assert _tactics(controller) == before
    assert controller.helper_lemma_stack == []
    assert controller.helper_lemma_context() == []
    # Coq is back on the parent goal, which is still provable
    session._do_user_tactic("unfold is_sint32.")
    session._do_user_tactic("reflexivity.")
    assert controller.is_successful


def test_drop_outside_subproof_is_a_no_op(session):
    controller = session.controller
    session._do_user_tactic("intros i.")
    before = _tactics(controller)

    session._do_drop()

    assert _tactics(controller) == before


def test_rollback_onto_brace_also_removes_assert(session):
    controller = session.controller
    session._do_user_tactic("intros i.")
    session._do_lemma(f"Hhelper: {HELPER_STATEMENT}")
    session._do_user_tactic("intros x.")

    # 2 steps would land on '{', which must take the assert with it
    session._do_rollback(2)

    assert _tactics(controller) == ["intros i."]
    assert controller.helper_lemma_stack == []


def test_admit_closes_subproof_without_recording(session):
    controller = session.controller
    session._do_lemma(f"Hhelper: {HELPER_STATEMENT}")

    session._do_admit()

    assert controller.helper_lemma_stack == []
    assert _tactics(controller)[-1] == "}"
    assert controller.tactic_history.helper_lemma_entries == []


def test_failed_lemma_is_reported_and_leaves_no_trace(session, capsys):
    controller = session.controller
    session._do_user_tactic("intros i.")

    session._do_lemma("Hbogus: this_identifier_does_not_exist")

    assert _tactics(controller) == ["intros i."]
    assert controller.helper_lemma_stack == []
    out = capsys.readouterr().out
    assert "✗ Helper Lemma" in out and "assert (Hbogus:" in out


def test_depth_limit_is_reported(session, capsys):
    controller = session.controller
    for i in range(controller.MAX_HELPER_LEMMA_DEPTH):
        session._do_lemma(f"Hnest{i}: {i} = {i}")
    assert len(controller.helper_lemma_stack) == controller.MAX_HELPER_LEMMA_DEPTH

    capsys.readouterr()
    session._do_lemma("Htoodeep: 9 = 9")

    # Rejected before touching the proof
    assert len(controller.helper_lemma_stack) == controller.MAX_HELPER_LEMMA_DEPTH
    assert "assert (Htoodeep:" not in [t for t in _tactics(controller)]
    out = capsys.readouterr().out
    assert "✗ Helper Lemma" in out and "depth" in out


def test_rollback_failure_renders_like_a_failed_tactic():
    line = visualizer.render_action(
        'rollback', {'reason': 'undo the last lemma', 'message': 'no successful tactics'}, False
    )
    assert "✗ Rollback" in line
    assert "undo the last lemma" in line
    assert "no successful tactics" in line


def test_lemma_respects_ablation_toggle(session):
    controller = session.controller
    controller.context_manager.enable_helper_lemma = False

    assert not controller.helper_lemma_enabled()
    session._do_lemma(f"Hhelper: {HELPER_STATEMENT}")

    assert _tactics(controller) == []
    assert controller.helper_lemma_context() == []


def test_nested_lemmas_open_and_close_in_order(session):
    controller = session.controller
    session._do_lemma(f"Houter: {HELPER_STATEMENT}")
    session._do_lemma("Hinner: 0 = 0")
    assert [hl["name"] for hl in controller.helper_lemma_context()] == ["Houter", "Hinner"]

    session._do_user_tactic("reflexivity.")

    # Inner closed, outer still open
    assert [hl["name"] for hl in controller.helper_lemma_context()] == ["Houter"]
    assert session._prompt() == "lemmanet[Houter]> "

    for tactic in HELPER_TACTICS:
        session._do_user_tactic(tactic)

    assert controller.helper_lemma_context() == []
    assert controller.helper_lemma_stack == []
    assert _tactics(controller)[-1] == "}"

    # Each lemma is recorded against its own proof: the inner one gets only the
    # tactics inside it, the outer one spans the nested lemma as well.
    by_name = {e.name: e for e in controller.tactic_history.helper_lemma_entries}
    assert set(by_name) == {"Houter", "Hinner"}
    assert by_name["Hinner"].proof_tactics == ["reflexivity.", "}"]
    assert by_name["Houter"].proof_tactics == [
        "assert (Hinner: 0 = 0)", "{", "reflexivity.", "}",
    ] + HELPER_TACTICS + ["}"]
