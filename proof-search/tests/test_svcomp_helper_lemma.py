import sys
import os
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.coq_interface import CoqInterface


TEST_CONTENT = """Definition is_sint32 (x : nat) : Prop :=
  x = x.

Lemma main :
  forall i : nat, is_sint32 i.
Proof.
Admitted.
"""


def print_current_goals(coq_interface):
    """Print current goals from the CoqInterface."""
    print("\nCurrent Goals:")
    try:
        goals_str = coq_interface.get_goal_str()
        if goals_str and goals_str != "No current goals":
            print(goals_str)
        else:
            print("(No goals remaining)")
    except Exception as e:
        print(f"(Error getting goals: {e})")


def print_current_hypotheses(coq_interface):
    """Print current hypotheses from the CoqInterface."""
    print("\nCurrent Hypotheses:")
    try:
        hypotheses_str = coq_interface.get_hypothesis()
        if hypotheses_str and hypotheses_str.strip():
            print(hypotheses_str)
        else:
            print("(No hypotheses)")
    except Exception as e:
        print(f"(Error getting hypotheses: {e})")


def print_current_proof_state(coq_interface):
    """Print complete current proof state."""
    print("\n" + "CURRENT PROOF STATE".center(80, "="))
    print_current_hypotheses(coq_interface)
    print_current_goals(coq_interface)
    print("=" * 80)


def test_helper_lemma_completion_closes_subproof(tmp_path):
    """Test CoqInterface helper-lemma brace completion on an SV-COMP-style goal."""
    coq_file = tmp_path / "svcomp_helper_lemma.v"
    coq_file.write_text(TEST_CONTENT, encoding="utf-8")
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)

    coq_interface = CoqInterface(
        file_path=str(coq_file),
        workspace=str(tmp_path),
        timeout=20,
    )

    try:
        print(f"Loading file: {coq_file}")
        assert coq_interface.load(), coq_interface.get_last_error()

        tactics = [
            "intros i.",
            "assert (Hhelper: forall x : nat, is_sint32 x).",
            "{",
        ]

        for i, tactic in enumerate(tactics, 1):
            print(f"\nStep {i}: Applying tactic: {tactic}")
            assert coq_interface.apply_tactic(tactic), coq_interface.get_last_error()
            print_current_proof_state(coq_interface)

        assert not coq_interface.is_helper_lemma_proof_complete()

        print("\nSolving helper lemma sub-proof")
        helper_tactics = [
            "intros x.",
            "unfold is_sint32.",
            "reflexivity.",
        ]

        for tactic in helper_tactics:
            assert coq_interface.apply_tactic(tactic), coq_interface.get_last_error()

        print_current_proof_state(coq_interface)

        assert coq_interface.is_helper_lemma_proof_complete()
        assert coq_interface.proof.steps[-1].text.strip() == "}"

        print("\nSolving main proof with helper lemma in context")
        assert coq_interface.apply_tactic("apply Hhelper."), coq_interface.get_last_error()

        status = coq_interface.get_proof_completion_status()
        assert status["is_complete"], status
        assert status["qed_already_applied"], status

    finally:
        coq_interface.close()
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        test_helper_lemma_completion_closes_subproof(Path(tmp_dir))
