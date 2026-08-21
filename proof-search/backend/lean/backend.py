"""Lean 4 backend implemented with the leanprover-community REPL.

Lean proof states are immutable and persistent: applying a tactic returns a new
state and leaves the previous one usable. Checkpoints therefore store a state
identifier and rollback is exact rather than a replay.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from utils.logger import setup_logger
from .protocol import (
    DeclarationSpan, SorryGoal, error_feedback, parse_goals, parse_messages,
    parse_sorries, render_certificate, select_sorry, source_declarations,
)
from .repl_session import LeanReplDrainTimeoutError, LeanReplSession
from ..prover_backend import (
    BackendCapabilities, Checkpoint, CommandKind, CommandRejectedError, CommandResult,
    ContextEntry, FeedbackSeverity, Goal, HelperLemmaCommands, HelperLemmaSpec,
    InvalidCheckpointError, InvalidLifecycleError, LifecycleState, ProofState,
    ProverBackend, ProverFeedback, ProverProtocolError, ProvingContext, QueryResult,
    ProverTimeoutError, SavedProofCertificate, TheoremIdentity,
)

logger = setup_logger("LeanReplBackend")

_DEFAULT_AUTOMATION = "aesop"
_DEFAULT_MAX_HEARTBEATS = 200_000
_DEFAULT_QUERY_MAX_HEARTBEATS = 50_000
# `guard_goal_nums` and `skip` are the scope bookkeeping this backend emits
# itself; they carry no proof content worth replaying as history.
_STRUCTURAL = re.compile(r"^(?:skip|guard_goal_nums\s+\d+|·|\{|\}|next\b|case\b)")
_UNSOUND = re.compile(r"(?:^|\W)(?:sorry|admit)(?:\W|$)")


def default_repl_path() -> Path | None:
    """Return the configured Lean REPL executable, if one is set."""
    configured = os.environ.get("LEAN_REPL_PATH")
    if configured:
        return Path(configured).expanduser()
    fallback = Path.home() / ".lemmanet" / "provers" / "repl" / ".lake" / "build" / "bin" / "repl"
    return fallback if fallback.is_file() else None


def default_project_root() -> Path | None:
    configured = os.environ.get("LEAN_PROJECT_ROOT")
    return Path(configured).expanduser() if configured else None


class LeanReplBackend(ProverBackend):
    """Expose the Lean 4 REPL through the asynchronous backend contract.

    The REPL never writes to the source file, so this backend proves against
    the file in place and only an explicit ``save_proof`` produces output.

    Lifecycle differences from Rocq are deliberate and documented in
    ``handoff.md``: Lean has no native abort command, and context queries are
    either environment commands (``#check``) or tactic-level searches whose
    resulting proof state is discarded.
    """

    _capabilities = BackendCapabilities()

    def __init__(
        self,
        *,
        repl_path: Path | None = None,
        project_root: Path | None = None,
        timeout: int = 60,
        open_timeout: int | None = None,
        drain_timeout: float | None = None,
        max_heartbeats: int = _DEFAULT_MAX_HEARTBEATS,
        query_max_heartbeats: int | None = None,
        automation: str | None = _DEFAULT_AUTOMATION,
        use_lake: bool = True,
        session_factory: Callable[..., LeanReplSession] = LeanReplSession,
    ) -> None:
        resolved = repl_path or default_repl_path()
        if resolved is None:
            raise ProverProtocolError(
                "no Lean REPL executable configured; set LEAN_REPL_PATH or run "
                "scripts/provers/setup_lean_repl.sh"
            )
        self._repl_path = Path(resolved).expanduser()
        self._project_root = (
            Path(project_root).expanduser() if project_root is not None
            else default_project_root()
        )
        self._timeout = timeout
        # Elaborating a file's imports costs far more than one tactic.
        self._drain_timeout = timeout if drain_timeout is None else drain_timeout
        if max_heartbeats <= 0:
            raise ValueError("Lean max_heartbeats must be positive")
        self._max_heartbeats = max_heartbeats
        query_limit = (
            _DEFAULT_QUERY_MAX_HEARTBEATS
            if query_max_heartbeats is None
            else query_max_heartbeats
        )
        self._query_max_heartbeats = min(max_heartbeats, query_limit)
        self._open_timeout = open_timeout if open_timeout is not None else max(timeout, 900)
        self._automation = automation
        self._use_lake = use_lake
        self._session_factory = session_factory
        self._lifecycle = LifecycleState.CREATED
        self._session: LeanReplSession | None = None
        self._theorem: TheoremIdentity | None = None
        self._source_text: str | None = None
        self._span: DeclarationSpan | None = None
        self._sorry: SorryGoal | None = None
        self._environment: int | None = None
        self._proof_state: int | None = None
        self._goals: tuple[Goal, ...] = ()
        self._global_entries: tuple[ContextEntry, ...] = ()
        self._backend_token = object()
        self._session_token: object | None = None
        self._revision = 0
        self._commands: list[str] = []
        self._checkpoints: dict[int, tuple[int, tuple[str, ...], tuple[Goal, ...]]] = {}
        self._active_project_root: Path | None = None

    @property
    def lifecycle(self) -> LifecycleState:
        return self._lifecycle

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def _require(self, operation: str, *expected: LifecycleState) -> None:
        if self._lifecycle not in expected:
            raise InvalidLifecycleError(operation, self._lifecycle, expected)

    async def _run(self, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                await self.close()
            raise

    def _new_session(self) -> LeanReplSession:
        return self._session_factory(
            self._repl_path,
            project_root=self._active_project_root,
            timeout=self._timeout,
            drain_timeout=self._drain_timeout,
            use_lake=self._use_lake,
        )

    def _bounded_command(self, command: str, *, query: bool = False) -> str:
        heartbeats = (
            self._query_max_heartbeats if query else self._max_heartbeats
        )
        return f"set_option maxHeartbeats {heartbeats} in\n{command}"

    async def _request(
        self, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        assert self._session is not None
        try:
            return await self._run(self._session.request, payload, timeout=timeout)
        except LeanReplDrainTimeoutError as error:
            await self._restart_and_replay(error.drain_seconds)
            assert self._session is not None
            retry = dict(payload)
            if "proofState" in retry:
                retry["proofState"] = self._proof_state
            if retry.get("env") is not None:
                retry["env"] = self._environment
            return await self._run(self._session.request, retry, timeout=timeout)
        except ProverProtocolError:
            await self.close()
            raise

    async def _restart_and_replay(self, drain_seconds: float) -> None:
        """Replace a wedged process and rebuild its immutable state prefix."""
        assert self._source_text is not None and self._theorem is not None
        old_session = self._session
        started = time.monotonic()
        if old_session is not None:
            await asyncio.to_thread(old_session.close)
        session = self._new_session()
        self._session = session
        try:
            await self._run(session.start)
            answer = await self._run(
                session.request,
                {"cmd": self._source_text, "env": None},
                timeout=self._open_timeout,
            )
            feedback = parse_messages(answer)
            errors = error_feedback(feedback)
            if errors or "env" not in answer:
                detail = errors[0].message if errors else "no environment"
                raise ProverProtocolError(
                    f"Lean restart could not reopen the source: {detail}"
                )
            _, sorry = select_sorry(
                self._source_text, self._theorem.name, parse_sorries(answer)
            )
            proof_state = sorry.proof_state
            goals = parse_goals((sorry.goal,), 0)
            replay_started = time.monotonic()
            for revision, command in enumerate(self._commands, 1):
                replay = await self._run(
                    session.request,
                    {
                        "tactic": self._bounded_command(command),
                        "proofState": proof_state,
                    },
                    timeout=self._timeout,
                )
                replay_feedback = error_feedback(parse_messages(replay))
                if "message" in replay or replay_feedback or "proofState" not in replay:
                    detail = str(replay.get("message") or replay_feedback or replay)
                    raise ProverProtocolError(
                        f"Lean restart could not replay {command!r}: {detail}"
                    )
                proof_state = int(replay["proofState"])
                goals = parse_goals(replay.get("goals") or (), revision)
            replay_seconds = time.monotonic() - replay_started
            self._environment = int(answer["env"])
            self._proof_state = proof_state
            self._goals = goals
            self._session_token = object()
            self._checkpoints.clear()
            logger.info(
                "MITIGATION L2 restart_replay drain_seconds=%.3f "
                "restart_seconds=%.3f replay_seconds=%.3f commands=%d",
                drain_seconds,
                time.monotonic() - started,
                replay_seconds,
                len(self._commands),
            )
        except BaseException:
            await asyncio.to_thread(session.close)
            self._session = None
            self._session_token = None
            self._checkpoints.clear()
            self._lifecycle = LifecycleState.CLOSED
            raise

    async def open(self, theorem: TheoremIdentity) -> ProofState:
        self._require("open", LifecycleState.CREATED)
        source = theorem.source.path.resolve()
        if not source.is_file():
            raise ProverProtocolError(f"Lean source does not exist: {source}")
        project_root = (
            theorem.source.workspace.resolve()
            if theorem.source.workspace is not None
            else self._project_root
        )
        self._active_project_root = project_root
        session = self._new_session()
        self._session = session
        try:
            await self._run(session.start)
            text = source.read_text(encoding="utf-8")
            answer = await self._request(
                {"cmd": text, "env": None}, timeout=self._open_timeout
            )
            feedback = parse_messages(answer)
            errors = error_feedback(feedback)
            if errors:
                raise ProverProtocolError(
                    f"Lean could not elaborate {source}: "
                    + "; ".join(item.message for item in errors[:3])
                )
            if "env" not in answer:
                raise ProverProtocolError(
                    f"the Lean REPL returned no environment for {source}"
                )
            span, sorry = select_sorry(text, theorem.name, parse_sorries(answer))
            self._theorem = theorem
            self._source_text = text
            self._span = span
            self._sorry = sorry
            self._environment = int(answer["env"])
            self._proof_state = sorry.proof_state
            self._goals = parse_goals((sorry.goal,), 0)
            self._global_entries = source_declarations(text, before_line=span.first_line)
            self._session_token = object()
            self._lifecycle = LifecycleState.OPEN
            logger.info(
                "Opened Lean theorem %s at proof state %s",
                span.qualified_name, sorry.proof_state,
            )
            return self._read_state()
        except BaseException:
            await self.close()
            raise

    def _read_state(self) -> ProofState:
        assert self._theorem is not None
        local_entries = self._goals[0].hypotheses if self._goals else ()
        context = ProvingContext(self._global_entries, local_entries)
        return ProofState(self._theorem, self._goals, context, self._revision)

    async def state(self) -> ProofState:
        self._require("state", LifecycleState.OPEN, LifecycleState.COMPLETE)
        return self._read_state()

    def _timeout_rejection(
        self, command: str, before: ProofState
    ) -> CommandRejectedError:
        logger.info(
            "MITIGATION L1 timeout_rejection timeout_seconds=%s command=%r",
            self._timeout,
            command,
        )
        return CommandRejectedError(command, before, (
            ProverFeedback(
                f"command timed out after {self._timeout} seconds",
                FeedbackSeverity.ERROR,
            ),
        ))

    async def apply(self, command: str) -> CommandResult:
        self._require("apply", LifecycleState.OPEN)
        before = self._read_state()
        if not command.strip():
            raise CommandRejectedError(command, before, (
                ProverFeedback("empty command", FeedbackSeverity.ERROR),
            ))
        try:
            answer = await self._request(
                {
                    "tactic": self._bounded_command(command.strip()),
                    "proofState": self._proof_state,
                }
            )
        except ProverTimeoutError:
            if self._lifecycle is not LifecycleState.OPEN:
                raise
            raise self._timeout_rejection(command, before) from None
        rejection = self._rejection(command, before, answer)
        if rejection is not None:
            raise rejection
        self._proof_state = int(answer["proofState"])
        self._revision += 1
        self._commands.append(command.strip())
        self._goals = parse_goals(answer.get("goals") or (), self._revision)
        state = self._read_state()
        if state.is_complete:
            self._lifecycle = LifecycleState.COMPLETE
        return CommandResult(command, state, parse_messages(answer))

    def _rejection(
        self, command: str, before: ProofState, answer: Mapping[str, Any]
    ) -> CommandRejectedError | None:
        """Classify a tactic answer, leaving the proof state untouched."""
        if "message" in answer:
            message = str(answer["message"])
            self._log_heartbeat_rejection(command, message)
            return CommandRejectedError(command, before, (
                ProverFeedback(message, FeedbackSeverity.ERROR),
            ))
        feedback = parse_messages(answer)
        errors = error_feedback(feedback)
        if errors:
            self._log_heartbeat_rejection(
                command, "\n".join(item.message for item in errors)
            )
            return CommandRejectedError(command, before, errors)
        if answer.get("sorries"):
            # Lean reports no open goals for a sorried state; accepting it would
            # record an unsound proof as complete.
            return CommandRejectedError(command, before, (
                ProverFeedback(
                    "the tactic left a `sorry` placeholder, which cannot close a proof",
                    FeedbackSeverity.ERROR,
                ),
            ))
        if "proofState" not in answer:
            return CommandRejectedError(command, before, (
                ProverFeedback(
                    f"the Lean REPL returned no proof state for {command!r}",
                    FeedbackSeverity.ERROR,
                ),
            ))
        return None

    @staticmethod
    def _is_heartbeat_error(message: str) -> bool:
        lowered = message.casefold()
        return "heartbeat" in lowered and (
            "maximum" in lowered or "exceeded" in lowered
        )

    def _log_heartbeat_rejection(self, command: str, message: str) -> None:
        if self._is_heartbeat_error(message):
            logger.info(
                "MITIGATION L3 heartbeat_rejection max_heartbeats=%d command=%r",
                self._max_heartbeats,
                command,
            )

    def classify_command(self, command: str) -> CommandKind:
        normalized = command.strip()
        if _UNSOUND.search(normalized):
            return CommandKind.UNSOUND_COMPLETION
        if _STRUCTURAL.match(normalized):
            return CommandKind.STRUCTURAL
        return CommandKind.PROOF_STEP

    def automation_command(self) -> str | None:
        return self._automation

    def helper_lemma_commands(self, spec: HelperLemmaSpec) -> HelperLemmaCommands:
        """Render a Lean `have` scope.

        `have name : statement` opens a goal for the statement ahead of the
        goals that were already open, so the scope is closed exactly when the
        goal count returns to what it was. `guard_goal_nums` is the native
        check for that, and it fails while the helper goal is still open.
        """
        return HelperLemmaCommands(
            f"have {spec.name} : {spec.statement}",
            "skip",
            f"guard_goal_nums {len(self._goals)}",
        )

    async def checkpoint(self) -> Checkpoint:
        self._require("checkpoint", LifecycleState.OPEN, LifecycleState.COMPLETE)
        if self._proof_state is None:
            raise ProverProtocolError("no Lean proof state to check point")
        self._checkpoints[self._revision] = (
            self._proof_state, tuple(self._commands), self._goals
        )
        return Checkpoint(self._backend_token, self._session_token, self._revision)

    async def rollback(self, checkpoint: Checkpoint) -> ProofState:
        self._require("rollback", LifecycleState.OPEN, LifecycleState.COMPLETE)
        valid = (
            checkpoint._backend_token is self._backend_token
            and checkpoint._session_token is self._session_token
            and checkpoint._payload in self._checkpoints
        )
        if not valid:
            raise InvalidCheckpointError("checkpoint is not valid for this session")
        proof_state, commands, goals = self._checkpoints[checkpoint._payload]
        self._proof_state = proof_state
        self._commands = list(commands)
        self._goals = goals
        self._revision = checkpoint._payload
        self._lifecycle = (
            LifecycleState.COMPLETE if not goals else LifecycleState.OPEN
        )
        return self._read_state()

    async def query(self, command: str) -> QueryResult:
        """Run a Lean context query without advancing the proof.

        Commands starting with `#` run against the file's environment; anything
        else runs as a tactic against the current proof state, whose result is
        discarded. Lean proof states are immutable, so the proof is unaffected
        either way. This gives the same breadth the Rocq backend has for
        `Search`/`Print`/`Locate`/`About`/`Check`:

        ==================================  ====================================
        purpose                             Lean command
        ==================================  ====================================
        search by name or pattern           ``#find _ + _ = _``
        search for a term closing the goal  ``exact?`` / ``apply?``
        print a declaration's definition    ``#print And.comm``
        show a declaration's type           ``#check @And.comm``
        type-check a term                   ``#check (fun n => n + 1)``
        list the axioms a proof depends on  ``#print axioms And.comm``
        ==================================  ====================================
        """
        self._require("query", LifecycleState.OPEN, LifecycleState.COMPLETE)
        before = self._read_state()
        stripped = command.strip()
        if not stripped:
            raise CommandRejectedError(command, before, (
                ProverFeedback("empty query", FeedbackSeverity.ERROR),
            ))
        try:
            if stripped.startswith("#"):
                answer = await self._request(
                    {
                        "cmd": self._bounded_command(stripped, query=True),
                        "env": self._environment,
                    }
                )
            else:
                answer = await self._request(
                    {
                        "tactic": self._bounded_command(stripped, query=True),
                        "proofState": self._proof_state,
                    }
                )
        except ProverTimeoutError:
            if self._lifecycle not in (LifecycleState.OPEN, LifecycleState.COMPLETE):
                raise
            raise self._timeout_rejection(command, before) from None
        if "message" in answer:
            message = str(answer["message"])
            self._log_heartbeat_rejection(command, message)
            raise CommandRejectedError(command, before, (
                ProverFeedback(message, FeedbackSeverity.ERROR),
            ))
        feedback = parse_messages(answer)
        errors = error_feedback(feedback)
        if errors:
            self._log_heartbeat_rejection(
                command, "\n".join(item.message for item in errors)
            )
            raise CommandRejectedError(command, before, errors)
        output = "\n".join(item.message for item in feedback)
        if not output and answer.get("goals") is not None:
            output = "\n\n".join(answer["goals"])
        return QueryResult(command, output, feedback)

    async def save_proof(
        self, destination: Path, *, overwrite: bool = False
    ) -> SavedProofCertificate:
        self._require("save_proof", LifecycleState.COMPLETE)
        destination = destination.resolve()
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        assert self._source_text is not None and self._sorry is not None
        assert self._theorem is not None
        certificate = render_certificate(
            self._source_text, self._sorry, tuple(self._commands)
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=destination.parent, delete=False, encoding="utf-8"
        ) as temporary:
            temporary.write(certificate)
            temporary_path = Path(temporary.name)
        try:
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        return SavedProofCertificate(
            self._theorem, destination, "lean-source", tuple(self._commands)
        )

    async def close(self) -> None:
        session = self._session
        self._session = None
        self._checkpoints.clear()
        self._session_token = None
        self._proof_state = None
        self._lifecycle = LifecycleState.CLOSED
        if session is not None:
            await asyncio.to_thread(session.close)
