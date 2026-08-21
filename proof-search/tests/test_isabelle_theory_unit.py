"""Unit tests for Isabelle theory text handling, without starting Isabelle."""

import pytest

from backend.isabelle.theory import (
    declarations, discover_theorem_name, is_terminal_command, locate_theorem,
    render_certificate, render_working_copy, rename_theory, theory_name,
)
from backend.prover_backend import ProverProtocolError

VC = """theory huffman_Top_sorted_tailqtvc
  imports "NTP4Verif.NTP4Verif" "Why3STD.int_Sum"
begin
definition last :: "int list \\<Rightarrow> int"
  where "last s = s ! nat (int (length s) - (1 :: int))" for s
theorem sorted_tail'vc:
  fixes s :: "int list"
  assumes fact0: "sorted s"
  shows "sorted (drop (1 :: nat) s)"
  sorry
end
"""


def test_theory_name_and_rename():
    assert theory_name(VC) == "huffman_Top_sorted_tailqtvc"
    renamed = rename_theory(VC, "LemmaNetWorking")
    assert theory_name(renamed) == "LemmaNetWorking"
    # Only the header changes.
    assert renamed.splitlines()[1:] == VC.splitlines()[1:]
    with pytest.raises(ProverProtocolError, match="theory` header"):
        theory_name("lemma foo: \"True\"\n")


def test_declarations_reports_names_and_lines():
    assert declarations(VC) == (("sorted_tail'vc", 6),)


def test_locate_theorem_finds_the_placeholder():
    region = locate_theorem(VC, "sorted_tail'vc")
    assert region.declaration_line == 6
    assert region.placeholder_line == 10


def test_locate_theorem_rejects_unknown_and_proved_theorems():
    with pytest.raises(ProverProtocolError, match="no Isabelle theorem named"):
        locate_theorem(VC, "absent")
    proved = VC.replace("  sorry", "  by auto")
    with pytest.raises(ProverProtocolError, match="no `sorry` placeholder"):
        locate_theorem(proved, "sorted_tail'vc")


def test_discover_theorem_name_prefers_the_unproved_theorem():
    assert discover_theorem_name(VC) == "sorted_tail'vc"
    with pytest.raises(ProverProtocolError, match="no Isabelle theorem"):
        discover_theorem_name("theory T imports Main begin end\n")


def test_render_working_copy_without_commands_targets_the_statement():
    text, line = render_working_copy(VC, locate_theorem(VC, "sorted_tail'vc"), ())
    # The placeholder is kept so the theory stays well formed ...
    assert "  sorry" in text
    # ... but `sorry` prints no proof state, so evaluation stops at the statement.
    assert line == 9
    assert text == VC


def test_render_working_copy_replaces_the_placeholder_with_commands():
    region = locate_theorem(VC, "sorted_tail'vc")
    text, line = render_working_copy(VC, region, ["apply (rule impI)", "apply auto"])
    assert "sorry" not in text
    assert line == 11
    lines = text.splitlines()
    assert lines[9] == "  apply (rule impI)"
    assert lines[10] == "  apply auto"
    assert lines[11] == "end"
    assert text.endswith("\n")


def test_render_working_copy_rejects_a_placeholder_outside_the_source():
    region = locate_theorem(VC, "sorted_tail'vc")
    truncated = "theory T\nimports Main\nbegin\n"
    with pytest.raises(ProverProtocolError, match="outside the source"):
        render_working_copy(truncated, region, ["apply auto"])


def test_is_terminal_command():
    assert is_terminal_command("done")
    assert is_terminal_command("  qed  ")
    assert is_terminal_command("by auto")
    assert is_terminal_command("by(simp)")
    assert not is_terminal_command("apply auto")
    assert not is_terminal_command("apply (rule impI)")


def test_render_certificate_closes_an_open_apply_script():
    region = locate_theorem(VC, "sorted_tail'vc")
    rendered = render_certificate(VC, region, ["apply auto"])
    assert "sorry" not in rendered
    assert rendered.splitlines()[9:11] == ["  apply auto", "  done"]


def test_render_certificate_keeps_an_already_terminal_proof():
    region = locate_theorem(VC, "sorted_tail'vc")
    rendered = render_certificate(VC, region, ["by auto"])
    assert rendered.splitlines()[9] == "  by auto"
    assert "done" not in rendered
    with pytest.raises(ProverProtocolError, match="without commands"):
        render_certificate(VC, region, [])
