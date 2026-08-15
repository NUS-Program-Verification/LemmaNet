"""Unit tests for reading a theorem's name off its Coq statement."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.coq_utils import extract_theorem_name


def test_theorem_and_lemma():
    assert extract_theorem_name("Theorem wp_goal :\n  forall x, P x.") == "wp_goal"
    assert extract_theorem_name("Lemma orb_true_l : forall b, orb true b = true.") == "orb_true_l"


def test_other_statement_keywords():
    assert extract_theorem_name("Corollary foo_bar : P.") == "foo_bar"
    assert extract_theorem_name("Definition P_Sorted_1_ (M:addr) : Prop := True.") == "P_Sorted_1_"
    assert extract_theorem_name("Proposition p1 : P.") == "p1"
    assert extract_theorem_name("Fact f1 : P.") == "f1"


def test_modifiers_and_attributes():
    assert extract_theorem_name("Program Definition d1 : nat := 0.") == "d1"
    assert extract_theorem_name("#[local] Instance my_inst : Foo := {}.") == "my_inst"
    assert extract_theorem_name("Local Lemma l1 : P.") == "l1"


def test_anonymous_and_non_statements():
    # 'Goal' declares no name — must not pick up the first token of the statement
    assert extract_theorem_name("Goal forall x, x = x.") == ""
    assert extract_theorem_name("intros x.") == ""
    assert extract_theorem_name("") == ""
