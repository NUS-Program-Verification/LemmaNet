"""The REPL must not re-render actions that step_generator() already printed."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.interactive_session import InteractiveSessionManager


class _StubController:
    """_report touches nothing on the controller, so a bare object suffices."""


def _session():
    return InteractiveSessionManager(_StubController())


def test_successful_tactic_is_not_reprinted(capsys):
    _session()._report(
        {'type': 'tactic', 'tactic': 'lia.', 'success': True, 'proof_complete': False}
    )
    assert capsys.readouterr().out == ""


def test_failed_tactic_reports_only_the_error(capsys):
    _session()._report(
        {'type': 'tactic', 'tactic': 'lia.', 'success': False, 'error': 'no applicable tactic'}
    )
    out = capsys.readouterr().out
    assert "no applicable tactic" in out
    assert "lia." not in out, "the controller already rendered the tactic"


def test_completed_proof_is_announced(capsys):
    _session()._report(
        {'type': 'tactic', 'tactic': 'lia.', 'success': True, 'proof_complete': True}
    )
    assert "Proof complete" in capsys.readouterr().out


def test_successful_rollback_is_not_reprinted(capsys):
    _session()._report({'type': 'rollback', 'success': True, 'distance': 2})
    assert capsys.readouterr().out == ""


def test_failed_rollback_is_reported(capsys):
    _session()._report({'type': 'rollback', 'success': False, 'distance': 0})
    assert "rollback failed" in capsys.readouterr().out.lower()
