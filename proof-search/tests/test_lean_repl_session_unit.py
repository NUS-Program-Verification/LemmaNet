"""Lean REPL stream recovery without starting Lean."""

import json
import threading
import time

import pytest

from backend.lean.repl_session import LeanReplSession
from backend.prover_backend import ProverTimeoutError


class _Input:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, value: str) -> None:
        self.writes.append(value)

    def flush(self) -> None:
        return None


class _Process:
    def __init__(self) -> None:
        self.stdin = _Input()

    def poll(self):
        return None


def test_late_answer_is_drained_before_the_next_request(tmp_path):
    session = LeanReplSession(
        tmp_path / "repl", timeout=0.01, drain_timeout=0.2, use_lake=False
    )
    session._process = _Process()

    with pytest.raises(ProverTimeoutError, match="0.01 seconds"):
        session.request({"cmd": "#check slow", "env": 0})

    def answers() -> None:
        time.sleep(0.02)
        session._answers.put(json.dumps({"env": 1, "messages": []}))
        session._answers.put(json.dumps({"env": 2, "messages": []}))

    producer = threading.Thread(target=answers)
    producer.start()
    answer = session.request({"cmd": "#check True", "env": 0})
    producer.join()
