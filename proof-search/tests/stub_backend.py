"""Deterministic backend stub used by backend and agent unit tests."""

from pathlib import Path

from backend.prover_backend import (
    BackendCapabilities, Checkpoint, CommandKind, CommandRejectedError, CommandResult,
    ContextEntry, FeedbackSeverity, Goal, GoalId, InvalidCheckpointError, InvalidLifecycleError,
    HelperLemmaCommands, HelperLemmaSpec, LifecycleState, ProofState, ProverBackend, ProverBackendError,
    ProverFeedback, ProvingContext, QueryResult, SavedProofCertificate,
    TheoremIdentity,
)


class StubProverBackend(ProverBackend):
    """A scripted prover for one theorem with predictable branching."""

    _capabilities = BackendCapabilities()

    def __init__(self) -> None:
        self._lifecycle = LifecycleState.CREATED
        self._backend_token = object()
        self._session_token: object | None = None
        self._state: ProofState | None = None
        self._commands: list[str] = []
        self._snapshots: dict[int, tuple[ProofState, tuple[str, ...]]] = {}

    @property
    def lifecycle(self) -> LifecycleState:
        return self._lifecycle

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def _require(self, operation: str, *expected: LifecycleState) -> None:
        if self._lifecycle not in expected:
            raise InvalidLifecycleError(operation, self._lifecycle, expected)

    async def open(self, theorem: TheoremIdentity) -> ProofState:
        self._require("open", LifecycleState.CREATED)
        if theorem.name != "demo_theorem":
            raise ProverBackendError(f"unknown theorem: {theorem.name}")
        context = ProvingContext(
            global_entries=(ContextEntry("and_comm", "A /\\ B -> B /\\ A"),),
            local_entries=(ContextEntry("P", "Prop"), ContextEntry("Q", "Prop")),
        )
        self._state = ProofState(
            theorem, (Goal(GoalId("g0"), "P /\\ Q -> Q /\\ P"),), context, 0
        )
        self._session_token = object()
        self._lifecycle = LifecycleState.OPEN
        return self._state

    async def state(self) -> ProofState:
        self._require("state", LifecycleState.OPEN, LifecycleState.COMPLETE)
        assert self._state is not None
        return self._state

    async def apply(self, command: str) -> CommandResult:
        self._require("apply", LifecycleState.OPEN)
        before = await self.state()
        transitions = {
            (0, "intro H."): (Goal(GoalId("g1"), "Q /\\ P", (ContextEntry("H", "P /\\ Q"),)),),
            (1, "split."): (Goal(GoalId("g2"), "Q"), Goal(GoalId("g3"), "P")),
            (2, "exact H.2."): (Goal(GoalId("g3"), "P"),),
            (3, "exact H.1."): (),
        }
        goals = transitions.get((before.revision, command))
        if goals is None:
            feedback = (ProverFeedback("scripted tactic does not apply", FeedbackSeverity.ERROR, "STUB001"),)
            raise CommandRejectedError(command, before, feedback)
        self._commands.append(command)
        self._state = ProofState(before.theorem, goals, before.context, before.revision + 1)
        if self._state.is_complete:
            self._lifecycle = LifecycleState.COMPLETE
        return CommandResult(command, self._state)

    def classify_command(self, command: str) -> CommandKind:
        normalized = command.strip().lower().removesuffix(".")
        if normalized == "abort":
            return CommandKind.ABORT
        if normalized in {"admit", "admitted"}:
            return CommandKind.UNSOUND_COMPLETION
        if normalized in {"{", "}"}:
            return CommandKind.STRUCTURAL
        return CommandKind.PROOF_STEP

    def automation_command(self) -> str | None:
        return None

    async def checkpoint(self) -> Checkpoint:
        self._require("checkpoint", LifecycleState.OPEN, LifecycleState.COMPLETE)
        state = await self.state()
        self._snapshots[state.revision] = (state, tuple(self._commands))
        return Checkpoint(self._backend_token, self._session_token, state.revision)

    async def rollback(self, checkpoint: Checkpoint) -> ProofState:
        self._require("rollback", LifecycleState.OPEN, LifecycleState.COMPLETE)
        if (checkpoint._backend_token is not self._backend_token or
                checkpoint._session_token is not self._session_token or
                checkpoint._payload not in self._snapshots):
            raise InvalidCheckpointError("checkpoint is not valid for this session")
        self._state, commands = self._snapshots[checkpoint._payload]
        self._commands = list(commands)
        self._lifecycle = LifecycleState.OPEN if self._state.goals else LifecycleState.COMPLETE
        return self._state

    def helper_lemma_commands(
        self, spec: HelperLemmaSpec
    ) -> HelperLemmaCommands:
        return HelperLemmaCommands(
            f"assert ({spec.name}: {spec.statement})", "{", "}"
        )

    async def query(self, command: str) -> QueryResult:
        self._require("query", LifecycleState.OPEN, LifecycleState.COMPLETE)
        outputs = {"Check and_comm.": "and_comm : A /\\ B -> B /\\ A"}
        if command not in outputs:
            state = await self.state()
            raise CommandRejectedError(command, state, (ProverFeedback("unknown query", FeedbackSeverity.ERROR),))
        return QueryResult(command, outputs[command])

    async def save_proof(self, destination: Path, *, overwrite: bool = False
                         ) -> SavedProofCertificate:
        self._require("save_proof", LifecycleState.COMPLETE)
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text("\n".join(self._commands) + "\n", encoding="utf-8")
        temporary.replace(destination)
        assert self._state is not None
        return SavedProofCertificate(self._state.theorem, destination, "stub-script", tuple(self._commands))

    async def close(self) -> None:
        self._state = None
        self._snapshots.clear()
        self._session_token = None
        self._lifecycle = LifecycleState.CLOSED
