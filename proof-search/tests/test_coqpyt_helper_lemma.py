import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from coqpyt.coq.proof_file import ProofFile


TEST_CONTENT = """Lemma main :
  forall x : nat, x = x.
Proof.
Admitted.
"""


def print_current_goals(proof_file):
    """Print current goals from CoqPyt."""
    print("\nCurrent Goals:")
    try:
        goals = proof_file.current_goals
        if goals:
            print(goals)
        else:
            print("(No goals remaining)")
    except Exception as e:
        print(f"(Error getting goals: {e})")


def test_nested_helper_lemma_braces_with_coqpyt(tmp_path):
    """Test nested assert helper lemmas with explicit brace steps in CoqPyt."""
    test_file_path = tmp_path / "helper_lemma_test.v"
    test_file_path.write_text(TEST_CONTENT, encoding="utf-8")

    tactics = [
        " intros x.",
        " assert (H: forall y : nat, y = y).",
        " {",
        " intros y.",
        " assert (Hinner: forall z : nat, z = z).",
        " {",
        " intros z.",
        " reflexivity.",
        " }",
        " apply Hinner.",
        " }",
        " apply H.",
        " Qed.",
    ]

    print(f"Loading file: {test_file_path}")

    with ProofFile(str(test_file_path), workspace=str(tmp_path), timeout=60) as proof_file:
        proof_file.run()

        assert proof_file.unproven_proofs, "Expected one admitted proof"
        unproven = proof_file.unproven_proofs[0]

        assert unproven.steps
        assert unproven.steps[-1].text.strip() == "Admitted."

        proof_file.pop_step(unproven)

        for i, tactic in enumerate(tactics, 1):
            print(f"\nStep {i}: Applying tactic: {tactic.strip()}")
            proof_file.append_step(unproven, tactic)
            print_current_goals(proof_file)

        step_texts = [step.text.strip() for step in unproven.steps]
        assert step_texts.count("{") == 2
        assert step_texts.count("}") == 2
        assert step_texts[-1] == "Qed."


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        test_nested_helper_lemma_braces_with_coqpyt(Path(tmp_dir))
