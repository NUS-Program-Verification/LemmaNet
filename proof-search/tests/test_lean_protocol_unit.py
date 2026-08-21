"""Unit tests for Lean REPL parsing and rendering without starting Lean."""

import pytest

from backend.lean.protocol import (
    Position, SorryGoal, declaration_spans, discover_theorem_name, goal_case_tag,
    parse_goal, parse_goals, parse_messages, parse_sorries, render_certificate,
    select_declaration, select_sorry, source_declarations,
)
from backend.prover_backend import FeedbackSeverity, GoalId, ProverProtocolError

SIMPLE_GOAL = "P Q : Prop\nh : P ∧ Q\n⊢ Q ∧ P"
TAGGED_GOAL = "case hq\nP Q : Prop\nh : P ∧ Q\n⊢ Q"

VC_SOURCE = """import Why3.Base
open Classical
namespace demo_vc
axiom get_closure : {α : Type} -> List α -> ℤ -> α
noncomputable def last (s : List ℤ) := s[0]!
theorem goal0 (s : List ℤ) (fact0 : 1 ≤ s.length) : last s = last s
  := sorry
end demo_vc
"""


def test_parse_goal_splits_hypotheses_and_conclusion():
    goal = parse_goal(SIMPLE_GOAL, GoalId("g0"))
    assert goal.conclusion == "Q ∧ P"
    assert [(entry.name, entry.type_text) for entry in goal.hypotheses] == [
        ("P", "Prop"), ("Q", "Prop"), ("h", "P ∧ Q"),
    ]


def test_parse_goal_drops_case_tag_but_exposes_it_separately():
    goal = parse_goal(TAGGED_GOAL, GoalId("g0"))
    assert goal.conclusion == "Q"
    assert [entry.name for entry in goal.hypotheses] == ["P", "Q", "h"]
    assert goal_case_tag(TAGGED_GOAL) == "hq"
    assert goal_case_tag(SIMPLE_GOAL) is None


def test_parse_goal_joins_wrapped_continuation_lines():
    text = (
        "t : Memory.addr -> ℝ\n"
        "long : Cfloat.is_float32 r →\n"
        "  Cfloat.is_float32 r_1 →\n"
        "  Cfloat.is_float32 r_2\n"
        "⊢ S12.eqs12 (l_quatvect m a)\n"
        "    (l_mult b c)"
    )
    goal = parse_goal(text, GoalId("g0"))
    assert goal.hypotheses[1].type_text == (
        "Cfloat.is_float32 r → Cfloat.is_float32 r_1 → Cfloat.is_float32 r_2"
    )
    assert goal.conclusion == "S12.eqs12 (l_quatvect m a) (l_mult b c)"


def test_parse_goal_without_hypotheses():
    goal = parse_goal("⊢ True", GoalId("g0"))
    assert goal.conclusion == "True"
    assert goal.hypotheses == ()


def test_parse_goals_numbers_goals_within_a_revision():
    goals = parse_goals([SIMPLE_GOAL, TAGGED_GOAL], 3)
    assert [goal.id for goal in goals] == ["r3-g0", "r3-g1"]


def test_parse_messages_maps_severity_and_position():
    feedback = parse_messages({
        "messages": [
            {"severity": "error", "data": "type mismatch",
             "pos": {"line": 4, "column": 2}},
            {"severity": "warning", "data": "declaration uses 'sorry'"},
        ]
    })
    assert feedback[0].severity is FeedbackSeverity.ERROR
    assert feedback[0].code == "4:2"
    assert feedback[1].severity is FeedbackSeverity.WARNING
    assert feedback[1].code is None
    assert parse_messages({}) == ()


def test_parse_sorries_reads_positions_and_states():
    sorries = parse_sorries({
        "sorries": [
            {"proofState": 7, "goal": "⊢ True",
             "pos": {"line": 7, "column": 5}, "endPos": {"line": 7, "column": 10}},
            {"proofState": 8, "goal": "⊢ False", "pos": {"line": 9, "column": 0},
             "endPos": None},
        ]
    })
    assert len(sorries) == 1
    assert sorries[0].proof_state == 7
    assert sorries[0].start == Position(7, 5)


def test_declaration_spans_qualify_names_with_the_namespace():
    spans = declaration_spans(VC_SOURCE)
    assert [span.qualified_name for span in spans] == ["demo_vc.goal0"]
    assert spans[0].first_line == 6
    assert spans[0].contains(Position(7, 5))


def test_select_declaration_accepts_short_and_qualified_names():
    assert select_declaration(VC_SOURCE, "goal0").name == "goal0"
    assert select_declaration(VC_SOURCE, "demo_vc.goal0").name == "goal0"
    with pytest.raises(ProverProtocolError, match="no Lean declaration named"):
        select_declaration(VC_SOURCE, "absent")


def test_select_sorry_matches_the_named_declaration():
    sorries = parse_sorries({
        "sorries": [
            {"proofState": 3, "goal": "⊢ last s = last s",
             "pos": {"line": 7, "column": 5}, "endPos": {"line": 7, "column": 10}},
        ]
    })
    span, sorry = select_sorry(VC_SOURCE, "goal0", sorries)
    assert span.qualified_name == "demo_vc.goal0"
    assert sorry.proof_state == 3


def test_select_sorry_reports_a_declaration_that_is_already_proved():
    with pytest.raises(ProverProtocolError, match="no open `sorry`"):
        select_sorry(VC_SOURCE, "goal0", ())


def test_discover_theorem_name_prefers_the_sorried_declaration():
    assert discover_theorem_name(VC_SOURCE) == "goal0"
    proved = VC_SOURCE.replace(":= sorry", ":= rfl")
    assert discover_theorem_name(proved) == "goal0"
    with pytest.raises(ProverProtocolError):
        discover_theorem_name("import Why3.Base\n")


def test_source_declarations_stop_at_the_target_theorem():
    entries = source_declarations(VC_SOURCE, before_line=6)
    assert [entry.name for entry in entries] == ["get_closure", "last"]
    assert entries[0].type_text == ": {α : Type} -> List α -> ℤ -> α"


def test_render_certificate_replaces_the_sorry_with_a_by_block():
    sorry = SorryGoal(3, "⊢ True", Position(7, 5), Position(7, 10))
    rendered = render_certificate(VC_SOURCE, sorry, ["intro h", "exact h"])
    assert "  := by\n    intro h\n    exact h\n" in rendered
    assert "sorry" not in rendered
    assert rendered.startswith("import Why3.Base")
    assert rendered.endswith("end demo_vc\n")


def test_render_certificate_rejects_a_mismatched_position():
    sorry = SorryGoal(3, "⊢ True", Position(1, 0), Position(1, 5))
    with pytest.raises(ProverProtocolError, match="expected `sorry`"):
        render_certificate(VC_SOURCE, sorry, ["rfl"])
    with pytest.raises(ProverProtocolError, match="without tactics"):
        render_certificate(VC_SOURCE, SorryGoal(3, "", Position(7, 5), Position(7, 10)), [])
