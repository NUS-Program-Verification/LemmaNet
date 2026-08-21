"""Contract and integration tests for the Isabelle backend against Isabelle.

These deliberately drive the backend the way the Rocq and Lean tests do: one
`asyncio.run` per operation, so every call arrives on a different event loop.
The asynchronous client underneath is loop-bound, and the backend keeps that
off the caller by running sessions on its own loop.
"""

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from backend.prover_backend import (
    CommandRejectedError, InvalidCheckpointError, LifecycleState, SourceLocation,
    TheoremIdentity,
)

pytestmark = pytest.mark.integration

NTP4VC = Path(os.environ.get("NTP4VC_ROOT", "/workspace/NTP4VC"))
NTP4VC_VC = (
    NTP4VC / "data/why3/pearl/huffman_with_two_queues_vcg/isabelle"
    / "huffman_with_two_queues_Top_sorted_tailqtvc.thy"
)

SOURCE = """theory Demo
  imports Main
begin
theorem demo_theorem:
  fixes P Q :: bool
  assumes fact0: "P \\<and> Q"
  shows "Q \\<and> P"
  sorry
end
"""


def run(coroutine):
    """One fresh event loop per call, exactly as the other backends allow."""
    return asyncio.run(coroutine)


# One Isabelle session costs about 2 GB of resident memory, measured on this
# container: available memory fell from 3470 MB to 1511 MB when a prover with
# the HOL heap started.
SESSION_MEMORY_MB = 2200


def available_memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def require_isabelle():
    if shutil.which("isabelle") is None:
        pytest.skip("isabelle is required for Isabelle integration tests")
    pytest.importorskip(
        "isabelle_mcp",
        reason="isabelle-mcp is required; run scripts/provers/setup_isabelle_mcp.sh",
    )


@pytest.fixture
def demo_theorem(tmp_path: Path) -> TheoremIdentity:
    require_isabelle()
    source = tmp_path / "Demo.thy"
    source.write_text(SOURCE, encoding="utf-8")
    return TheoremIdentity(SourceLocation(source, tmp_path), "demo_theorem")


def build(**options):
    from backend.isabelle.backend import IsabelleBackend
    return IsabelleBackend(timeout=300, **options)


async def evaluated_without_errors(path: Path, logic: str, session_dirs) -> bool:
    """Replay a saved certificate through a fresh prover."""
    from isabelle_mcp import component
    from isabelle_mcp.evaluation import evaluate_to
    from isabelle_mcp.lsp_client import IsabelleLSPClient

    component.ensure_component()
    client = IsabelleLSPClient(logic=logic, session_dirs=list(session_dirs))
    await client.start()
    await client.initialize()
    try:
        total = len(path.read_text(encoding="utf-8").splitlines())
        view = await evaluate_to(client, str(path), total)
        return not any(snapshot.error_count for snapshot in view.files)
    finally:
        await client.shutdown()


def test_real_backend_contract(demo_theorem: TheoremIdentity, tmp_path: Path):
    original = demo_theorem.source.path.read_text(encoding="utf-8")
    backend = build(logic="HOL")
    try:
        state = run(backend.open(demo_theorem))
        assert state.theorem == demo_theorem
        assert state.goals[0].conclusion == "Q ∧ P"
        assert backend.lifecycle is LifecycleState.OPEN

        checkpoint = run(backend.checkpoint())

        before = run(backend.state())
        with pytest.raises(CommandRejectedError):
            run(backend.apply("apply nonsense_tactic_xyz"))
        assert run(backend.state()) == before

        branched = run(backend.apply("apply (rule conjI)")).state
        assert [goal.conclusion for goal in branched.goals] == ["Q", "P"]
        assert branched.revision == 1

        restored = run(backend.rollback(checkpoint))
        assert [goal.conclusion for goal in restored.goals] == ["Q ∧ P"]
        assert restored.revision == 0

        # The query surface matches Rocq's Search/Print/Locate/About/Check.
        query_state = run(backend.state())
        searched = run(backend.query('find_theorems "?P \\<and> ?Q"'))
        assert "conj" in searched.output
        printed = run(backend.query("thm conjI"))
        assert "∧" in printed.output
        stated = run(backend.query("print_statement conjI"))
        assert "conjI" in stated.output
        typed = run(backend.query('term "Suc 0"'))
        assert typed.output
        constants = run(backend.query('find_consts "nat \\<Rightarrow> nat"'))
        assert constants.output
        assert run(backend.state()) == query_state

        with pytest.raises(CommandRejectedError):
            run(backend.query("thm no_such_fact_xyz"))
        assert run(backend.state()) == query_state

        complete = run(backend.apply("apply (simp add: fact0)")).state
        assert complete.is_complete
        assert backend.lifecycle is LifecycleState.COMPLETE

        destination = tmp_path / "Proved.thy"
        certificate = run(backend.save_proof(destination))
        assert certificate.commands[-1] == "done"
        written = destination.read_text(encoding="utf-8")
        assert "sorry" not in written
        assert demo_theorem.source.path.read_text(encoding="utf-8") == original
    finally:
        run(backend.close())
    assert backend.lifecycle is LifecycleState.CLOSED
    assert run(evaluated_without_errors(tmp_path / "Proved.thy", "HOL", ()))


def test_slow_tactic_is_cancelled_and_restored(
    demo_theorem: TheoremIdentity, caplog
):
    backend = build(logic="HOL")
    try:
        before = run(backend.open(demo_theorem))
        backend._timeout = 2
        slow = (
            "apply (tactic ‹fn st => "
            "(OS.Process.sleep (Time.fromSeconds 10); Seq.single st)›)"
        )
        with caplog.at_level("INFO"):
            with pytest.raises(CommandRejectedError, match="tactic timed out"):
                run(backend.apply(slow))
        assert run(backend.state()) == before
        assert backend.lifecycle is LifecycleState.OPEN
        assert "MITIGATION I2 timeout_rejection" in caplog.text
    finally:
        run(backend.close())


def test_two_sessions_are_live_at_once(demo_theorem: TheoremIdentity):
    """Several open backends must coexist, as they do for Rocq and Lean.

    Each session is a full Isabelle process, so this needs the memory for two.
    That is a machine requirement, not a limit of the adapter: the loop and
    evaluation state they share are handled by the backend's own prover loop.
    """
    available = available_memory_mb()
    if available is not None and available < 2 * SESSION_MEMORY_MB:
        pytest.skip(
            f"two Isabelle sessions need about {2 * SESSION_MEMORY_MB} MB; "
            f"{available} MB available"
        )
    first, second = build(logic="HOL"), build(logic="HOL")
    try:
        run(first.open(demo_theorem))
        run(second.open(demo_theorem))
        checkpoint = run(first.checkpoint())

        # Each session advances independently.
        run(first.apply("apply (rule conjI)"))
        assert len(run(first.state()).goals) == 2
        assert len(run(second.state()).goals) == 1

        with pytest.raises(InvalidCheckpointError):
            run(second.rollback(checkpoint))

        run(second.apply("apply (rule conjI)"))
        assert len(run(second.state()).goals) == 2
    finally:
        run(first.close())
        run(second.close())


def test_ntp4vc_obligation_opens_and_preserves_its_source():
    require_isabelle()
    if not NTP4VC_VC.is_file():
        pytest.skip(f"the NTP4VC obligation {NTP4VC_VC} is required")
    original = NTP4VC_VC.read_text(encoding="utf-8")
    backend = build(logic="NTP4Verif", session_dirs=[str(NTP4VC)])
    theorem = TheoremIdentity(SourceLocation(NTP4VC_VC, NTP4VC), "sorted_tail'vc")
    try:
        state = run(backend.open(theorem))
        assert state.goals[0].conclusion == "sorted (drop 1 s)"
        assert backend.lifecycle is LifecycleState.OPEN
        # A rejected tactic must not disturb a benchmark obligation.
        with pytest.raises(CommandRejectedError):
            run(backend.apply("apply (rule no_such_rule)"))
        assert run(backend.state()).goals[0].conclusion == "sorted (drop 1 s)"
    finally:
        run(backend.close())
    assert NTP4VC_VC.read_text(encoding="utf-8") == original
