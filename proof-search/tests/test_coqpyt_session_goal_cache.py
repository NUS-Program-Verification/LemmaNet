import logging
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.rocq.session import CoqPytSession  # noqa: E402


class _FakeProofFile:
    def __init__(self):
        self.invalidate_calls = 0
        self.current_goals_calls = 0

        class _FakeGoalConfig:
            goals = []
            stack = []

        class _FakeGoalAnswer:
            def __init__(self):
                self.goals = _FakeGoalConfig()

            def __str__(self):
                return "fake-goals"

        self._goal_answer = _FakeGoalAnswer()

    def invalidate_goal_cache(self):
        self.invalidate_calls += 1

    @property
    def current_goals(self):
        self.current_goals_calls += 1
        return self._goal_answer


class _FakeStep:
    def __init__(self, text):
        self.text = text


def _new_session(proof_steps):
    session = object.__new__(CoqPytSession)
    session.proof_file = _FakeProofFile()
    session.proof = type("FakeProof", (), {"steps": list(proof_steps)})()
    session.logger = logging.getLogger("test_coqpyt_session_goal_cache")
    session.__dict__["_CoqPytSession__goal_cache_key"] = None
    session.__dict__["_CoqPytSession__cached_goals"] = None
    session.__dict__["_CoqPytSession__goal_cache_filled"] = False
    return session


def test_get_goal_str_and_get_subgoals_share_goal_cache():
    session = _new_session([_FakeStep("intro.")])

    first = session.get_goal_str()
    second = session.get_subgoals()

    assert first == "fake-goals"
    assert second == []
    assert session.proof_file.current_goals_calls == 1
    assert session.proof_file.invalidate_calls == 1


def test_goal_cache_refreshes_after_proof_mutates():
    session = _new_session([_FakeStep("intro.")])

    session.get_goal_str()
    session.proof.steps.append(_FakeStep("apply."))
    session.get_goal_str()

    assert session.proof_file.current_goals_calls == 2
