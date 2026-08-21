"""Typed asynchronous contract shared by interactive prover backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import NewType

GoalId = NewType("GoalId", str)


@dataclass(frozen=True, slots=True)
class SourceLocation:
    path: Path
    workspace: Path | None = None


@dataclass(frozen=True, slots=True)
class TheoremIdentity:
    source: SourceLocation
    name: str


@dataclass(frozen=True, slots=True)
class ContextEntry:
    name: str
    type_text: str
    value_text: str | None = None


@dataclass(frozen=True, slots=True)
class ProvingContext:
    """Global declarations and theorem-level assumptions at session open."""
    global_entries: tuple[ContextEntry, ...] = ()
    local_entries: tuple[ContextEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class Goal:
    id: GoalId
    conclusion: str
    hypotheses: tuple[ContextEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class ProofState:
    theorem: TheoremIdentity
    goals: tuple[Goal, ...]
    context: ProvingContext
    revision: int

    @property
    def is_complete(self) -> bool:
        return not self.goals


class FeedbackSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class CommandKind(Enum):
    PROOF_STEP = "proof_step"
    ABORT = "abort"
    UNSOUND_COMPLETION = "unsound_completion"
    STRUCTURAL = "structural"


@dataclass(frozen=True, slots=True)
class ProverFeedback:
    message: str
    severity: FeedbackSeverity = FeedbackSeverity.INFO
    code: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    state: ProofState
    feedback: tuple[ProverFeedback, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryResult:
    command: str
    output: str
    feedback: tuple[ProverFeedback, ...] = ()


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Opaque backend-owned handle; callers must not inspect its fields."""
    _backend_token: object = field(repr=False)
    _session_token: object = field(repr=False)
    _payload: object = field(repr=False)


@dataclass(frozen=True, slots=True)
class HelperLemmaSpec:
    """Agent-selected helper lemma without prover syntax."""

    name: str
    statement: str


@dataclass(frozen=True, slots=True)
class HelperLemmaCommands:
    """Backend-rendered native commands for one helper-lemma scope."""

    declaration: str
    open_scope: str
    close_scope: str


@dataclass(frozen=True, slots=True)
class SavedProofCertificate:
    theorem: TheoremIdentity
    destination: Path
    format: str
    commands: tuple[str, ...]


class LifecycleState(Enum):
    CREATED = "created"
    OPEN = "open"
    COMPLETE = "complete"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    checkpoints: bool = True
    queries: bool = True
    certificate_saving: bool = True


class ProverBackendError(Exception):
    pass


class InvalidLifecycleError(ProverBackendError):
    def __init__(self, operation: str, actual: LifecycleState,
                 expected: tuple[LifecycleState, ...]) -> None:
        self.operation, self.actual, self.expected = operation, actual, expected
        names = ", ".join(state.value for state in expected)
        super().__init__(f"{operation} requires {names}; current state is {actual.value}")


class CommandRejectedError(ProverBackendError):
    """A command rejected without changing state."""
    def __init__(self, command: str, state: ProofState,
                 feedback: tuple[ProverFeedback, ...]) -> None:
        self.command, self.state, self.feedback = command, state, feedback
        message = feedback[0].message if feedback else "command rejected"
        super().__init__(f"prover rejected {command!r}: {message}")


class InvalidCheckpointError(ProverBackendError):
    """A checkpoint is foreign, stale, or no longer retained."""


class ProverTimeoutError(ProverBackendError):
    pass


class ProverProtocolError(ProverBackendError):
    pass


class UnsupportedOperationError(ProverBackendError):
    pass


class ProverBackend(ABC):
    """Single-session asynchronous prover contract.

    The backend owns prover interaction. The agent owns proof trees, tactic
    selection, retries, persistent-error policy, and historical learning.
    ``apply`` accepts one native command. Rejection is atomic and raises
    ``CommandRejectedError``. Timeout raises ``ProverTimeoutError``; cancellation
    propagates ``asyncio.CancelledError``. Both preserve pre-call state or close
    the session if restoration is impossible. Checkpoints belong to their
    creating backend and current session and remain valid only while retained.
    """
    @property
    @abstractmethod
    def lifecycle(self) -> LifecycleState: ...

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities: ...

    @abstractmethod
    async def open(self, theorem: TheoremIdentity) -> ProofState: ...

    @abstractmethod
    async def state(self) -> ProofState: ...

    @abstractmethod
    async def apply(self, command: str) -> CommandResult: ...

    @abstractmethod
    def classify_command(self, command: str) -> CommandKind:
        """Classify a native command for prover-independent agent policy."""

    @abstractmethod
    def automation_command(self) -> str | None:
        """Return the native fallback automation command, if supported."""

    @abstractmethod
    async def checkpoint(self) -> Checkpoint: ...

    @abstractmethod
    async def rollback(self, checkpoint: Checkpoint) -> ProofState: ...

    @abstractmethod
    async def query(self, command: str) -> QueryResult: ...

    @abstractmethod
    def helper_lemma_commands(
        self, spec: HelperLemmaSpec
    ) -> HelperLemmaCommands:
        """Render syntax only; the agent owns helper-lemma policy."""

    @abstractmethod
    async def save_proof(self, destination: Path, *, overwrite: bool = False
                         ) -> SavedProofCertificate:
        """Save from COMPLETE; reject existing paths unless overwrite is true."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources; idempotent from every lifecycle state."""

    async def __aenter__(self) -> ProverBackend:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()
