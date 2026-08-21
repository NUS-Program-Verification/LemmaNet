"""Isabelle backend driven through Isabelle-MCP's PIDE/LSP component.

Isabelle is document-oriented: the proof is text in a theory file and the
prover evaluates it. This adapter therefore works the way the Rocq adapter
does, proving in a private copy and rolling back by re-evaluating a shorter
proof, rather than stepping a REPL.

The `isabelle mcp_server` component ships a prebuilt jar and needs no patched
Isabelle, which is why it is used in place of IsaREPL.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from utils.logger import setup_logger
from .theory import (
    TheoremRegion, discover_theorem_name, is_terminal_command, locate_theorem,
    relative_theory_imports, render_certificate, render_working_copy,
    rename_theory,
)
from ..prover_backend import (
    BackendCapabilities, Checkpoint, CommandKind, CommandRejectedError, CommandResult,
    ContextEntry, FeedbackSeverity, Goal, GoalId, HelperLemmaCommands, HelperLemmaSpec,
    InvalidCheckpointError, InvalidLifecycleError, LifecycleState, ProofState,
    ProverBackend, ProverFeedback, ProverProtocolError, ProverTimeoutError,
    ProvingContext, QueryResult,
    SavedProofCertificate, TheoremIdentity,
)

logger = setup_logger("IsabelleBackend")

# How often to re-check an evaluation that outlasted `evaluate_to`'s own
# polling budget. Bounded overall by the session timeout, not by this value.
_EVALUATION_POLL_SECONDS = 1.0


class _ProverLoop:
    """A dedicated event loop for every Isabelle session in this process.

    `isabelle_mcp` is natively asynchronous and keeps module-level state: its
    client binds a subprocess transport, a reader task, and locks to whichever
    loop created them, and `evaluation.evaluate_to` guards one in-flight
    evaluation per process behind a module-level `asyncio.Lock`.

    Running every session on one private loop keeps that state coherent and,
    more importantly, keeps it out of the caller's way. Callers await these
    operations from their own loop exactly as they do for the Rocq and Lean
    backends, and may use a fresh loop per call if they wish.
    """

    _guard = threading.Lock()
    _loop: asyncio.AbstractEventLoop | None = None
    _thread: threading.Thread | None = None
    _evaluation: asyncio.Lock | None = None

    @classmethod
    def _ensure(cls) -> asyncio.AbstractEventLoop:
        with cls._guard:
            if cls._loop is None:
                cls._loop = asyncio.new_event_loop()
                cls._thread = threading.Thread(
                    target=cls._loop.run_forever,
                    name="lemmanet-isabelle",
                    daemon=True,
                )
                cls._thread.start()
                cls._evaluation = asyncio.Lock()
            return cls._loop

    @classmethod
    async def run(cls, coroutine, *, exclusive: bool = False):
        """Await a coroutine on the prover loop from any caller loop.

        `exclusive` serializes whole evaluations, which the one-in-flight
        evaluation state in `isabelle_mcp` requires when more than one session
        is live in the same process.
        """
        loop = cls._ensure()

        async def body():
            if not exclusive:
                return await coroutine
            assert cls._evaluation is not None
            async with cls._evaluation:
                return await coroutine

        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(body(), loop)
        )


_DEFAULT_LOGIC = "HOL"
_DEFAULT_AUTOMATION = "apply auto"
_WORKING_THEORY = "LemmaNetWorking"
_UNSOUND = re.compile(r"^\s*(?:sorry|oops)\b")
_STRUCTURAL = re.compile(r"^\s*(?:next|\{|\}|\.\.)\s*$")
# `find_theorems` accepts a bare pattern; a leading keyword selects the form.
_QUERY_KEYWORDS = ("name:", "intro", "elim", "dest", "solves", "simp:")


class IsabelleBackend(ProverBackend):
    """Expose Isabelle through the asynchronous backend contract.

    The theorem's source file is never modified. Proving happens in a private
    copy whose theory header is renamed to match its temporary file name, which
    Isabelle requires. Session-qualified imports resolve through the configured
    logic and session directories, so the copy does not have to live beside the
    original.

    Callers drive this exactly as they drive the Rocq and Lean backends: any
    event loop, a fresh loop per call, and several sessions live at once are
    all fine. The asynchronous client underneath is loop-bound and keeps one
    evaluation state per process, so every session runs on one private loop
    (see `_ProverLoop`) and evaluations are serialized there.

    Two consequences are worth knowing, neither of which changes the contract:
    two Isabelle sessions in one process evaluate one after another rather than
    in parallel, and each session is a full Isabelle process needing roughly
    2 GB of memory.
    """

    _capabilities = BackendCapabilities()

    def __init__(
        self,
        *,
        logic: str = _DEFAULT_LOGIC,
        session_dirs: Sequence[str] = (),
        timeout: int = 120,
        automation: str | None = _DEFAULT_AUTOMATION,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._logic = logic
        self._session_dirs = list(session_dirs)
        self._timeout = timeout
        self._automation = automation
        self._client_factory = client_factory
        self._lifecycle = LifecycleState.CREATED
        self._client: Any | None = None
        self._theorem: TheoremIdentity | None = None
        self._source_text: str | None = None
        self._working_text: str | None = None
        self._region: TheoremRegion | None = None
        self._working_path: Path | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._goals: tuple[Goal, ...] = ()
        self._global_entries: tuple[ContextEntry, ...] = ()
        self._backend_token = object()
        self._session_token: object | None = None
        self._revision = 0
        self._commands: list[str] = []
        self._checkpoints: dict[int, tuple[tuple[str, ...], tuple[Goal, ...]]] = {}

    @property
    def lifecycle(self) -> LifecycleState:
        return self._lifecycle

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def _require(self, operation: str, *expected: LifecycleState) -> None:
        if self._lifecycle not in expected:
            raise InvalidLifecycleError(operation, self._lifecycle, expected)

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(
                logic=self._logic, session_dirs=self._session_dirs
            )
        try:
            from isabelle_mcp import component
            from isabelle_mcp.lsp_client import IsabelleLSPClient
        except ImportError as error:
            raise ProverProtocolError(
                "the isabelle-mcp package is required; run "
                "scripts/provers/setup_isabelle_mcp.sh"
            ) from error
        # Registers the prebuilt Scala component; no Isabelle patch or rebuild.
        component.ensure_component()
        return IsabelleLSPClient(
            logic=self._logic, session_dirs=self._session_dirs
        )

    async def open(self, theorem: TheoremIdentity) -> ProofState:
        self._require("open", LifecycleState.CREATED)
        source = theorem.source.path.resolve()
        if not source.is_file():
            raise ProverProtocolError(f"Isabelle source does not exist: {source}")
        text = source.read_text(encoding="utf-8")
        region = locate_theorem(text, theorem.name)

        temporary_directory = tempfile.TemporaryDirectory(prefix="lemmanet_isabelle_")
        working_path = Path(temporary_directory.name) / f"{_WORKING_THEORY}.thy"
        working_text = rename_theory(text, _WORKING_THEORY)
        self._temporary_directory = temporary_directory
        self._working_path = working_path
        self._source_text = text
        self._working_text = working_text
        self._region = region
        self._theorem = theorem
        copied = self._copy_relative_imports(source, working_path.parent)
        if copied:
            logger.info(
                "MITIGATION I1 sibling_imports copied=%d", copied
            )

        client = self._build_client()
        self._client = client
        try:
            await _ProverLoop.run(self._start(client))
            self._session_token = object()
            self._lifecycle = LifecycleState.OPEN
            self._goals, _ = await self._evaluate((), 0)
            state = self._read_state()
            if state.goals == ():
                raise ProverProtocolError(
                    f"theorem {theorem.name!r} reported no goal to prove"
                )
            logger.info(
                "Opened Isabelle theorem %s with logic %s", theorem.name, self._logic
            )
            return state
        except BaseException:
            await self.close()
            raise

    @staticmethod
    def _copy_relative_imports(source: Path, destination: Path) -> int:
        """Copy transitive `./` imports beside the private working theory."""
        root = source.parent.resolve()
        pending = [source.resolve()]
        copied: set[Path] = set()
        started = time.monotonic()
        while pending:
            current = pending.pop()
            text = current.read_text(encoding="utf-8")
            for relative in relative_theory_imports(text):
                imported = (current.parent / relative).resolve()
                try:
                    target_relative = imported.relative_to(root)
                except ValueError as error:
                    raise ProverProtocolError(
                        f"relative Isabelle import escapes source directory: {relative}"
                    ) from error
                if not imported.is_file():
                    raise ProverProtocolError(
                        f"relative Isabelle import does not exist: {imported}"
                    )
                if imported in copied:
                    continue
                target = destination / target_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(imported, target)
                copied.add(imported)
                pending.append(imported)
        if copied:
            logger.info(
                "MITIGATION I1 sibling_import_copy copy_seconds=%.3f",
                time.monotonic() - started,
            )
        return len(copied)

    @staticmethod
    async def _start(client: Any) -> None:
        await client.start()
        await client.initialize()

    @staticmethod
    async def _stop(client: Any) -> None:
        try:
            await asyncio.wait_for(client.shutdown(), 30)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            # A prover that will not shut down cleanly is killed instead.
            kill = getattr(client, "kill", None)
            if kill is not None:
                kill()

    @staticmethod
    def _is_client_failure(error: BaseException) -> bool:
        """Return whether Isabelle-MCP reports a dead or unusable client."""
        try:
            from isabelle_mcp.utils.core import IsabelleToolError
        except ImportError:
            return False
        return isinstance(error, IsabelleToolError)

    async def _restart_and_restore(self) -> float:
        """Replace a dead client and replay the accepted proof prefix."""
        started = time.monotonic()

        async def restart() -> None:
            previous = self._client
            self._client = None
            if previous is not None:
                await self._stop(previous)

            replacement = self._build_client()
            self._client = replacement
            try:
                await self._start(replacement)
                goals, errors = await self._evaluate_on_loop(
                    self._commands, self._revision
                )
                if errors:
                    raise ProverProtocolError(
                        "Isabelle restart could not restore the accepted proof"
                    )
            except BaseException:
                await self._stop(replacement)
                self._client = None
                raise
            self._goals = goals

        await _ProverLoop.run(restart(), exclusive=True)
        return time.monotonic() - started

    def _write_working_copy(self, commands: Sequence[str]) -> int:
        assert self._working_text is not None and self._region is not None
        assert self._working_path is not None
        text, last_line = render_working_copy(
            self._working_text, self._region, commands
        )
        self._working_path.write_text(text, encoding="utf-8")
        return last_line

    async def _evaluate(
        self, commands: Sequence[str], revision: int
    ) -> tuple[tuple[Goal, ...], tuple[ProverFeedback, ...]]:
        """Write the proof so far, evaluate it, and read back goals and errors.

        This does not change the backend's own state, so a caller can evaluate
        a candidate proof and discard the result.
        """
        try:
            return await _ProverLoop.run(
                self._evaluate_on_loop(commands, revision), exclusive=True
            )
        except BaseException as error:
            if not self._is_client_failure(error):
                raise

        try:
            restart_seconds = await self._restart_and_restore()
        except BaseException as restart_error:
            await self.close()
            raise ProverProtocolError(
                "Isabelle evaluation failed and the prover could not be restarted"
            ) from restart_error
        logger.info(
            "MITIGATION I3 evaluation_restart restore_seconds=%.3f",
            restart_seconds,
        )
        try:
            return await _ProverLoop.run(
                self._evaluate_on_loop(commands, revision), exclusive=True
            )
        except BaseException as retry_error:
            if not self._is_client_failure(retry_error):
                raise
            await self.close()
            raise ProverProtocolError(
                "Isabelle evaluation failed after restarting the prover"
            ) from retry_error

    async def _evaluate_on_loop(
        self, commands: Sequence[str], revision: int
    ) -> tuple[tuple[Goal, ...], tuple[ProverFeedback, ...]]:
        from isabelle_mcp.evaluation import evaluate_to, sync_file_locked
        from isabelle_mcp.tools.goal import goal as goal_tool
        from isabelle_mcp.utils import MCPLine

        last_line = self._write_working_copy(commands)
        path = str(self._working_path)
        await asyncio.wait_for(sync_file_locked(self._client, path), self._timeout)
        view = await asyncio.wait_for(
            evaluate_to(self._client, path, last_line), self._timeout
        )
        view = await self._await_evaluation(view, path)
        errors = self._errors(view, last_line)
        state = await asyncio.wait_for(
            goal_tool(self._client, path, MCPLine(last_line)), self._timeout
        )
        goals = tuple(
            Goal(GoalId(f"r{revision}-g{index}"), text)
            for index, text in enumerate(state.subgoals)
        )
        return goals, errors

    async def _await_evaluation(self, view: Any, path: str) -> Any:
        """Poll an evaluation that is still running until PIDE finishes it.

        `evaluate_to` returns when its own polling budget expires even though
        PIDE may still be processing; the view then reports ``in_progress``
        and the module-level evaluation guard stays armed, so the next tool
        call would fail with "Evaluation in progress". First loads of heavy
        import graphs routinely outlast that budget. Bounded by the session
        timeout; on expiry the state cannot be trusted and the caller's
        cleanup path closes the session.
        """
        if getattr(view, "status", None) != "in_progress":
            return view
        from isabelle_mcp.evaluation import evaluation_status

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while getattr(view, "status", None) == "in_progress":
            if loop.time() >= deadline:
                raise ProverTimeoutError(
                    f"Isabelle did not finish evaluating {path} "
                    f"within {self._timeout} seconds"
                )
            await asyncio.sleep(_EVALUATION_POLL_SECONDS)
            view = await evaluation_status(self._client)
        return view

    def _errors(self, view: Any, last_line: int) -> tuple[ProverFeedback, ...]:
        """Collect errors that belong to the proof commands just evaluated.

        An unfinished proof also makes the theory's closing `end` fail, and
        that error is reported even though the command was never asked for.
        Only errors overlapping the commands themselves reject a command.
        """
        assert self._region is not None
        first_line = self._region.placeholder_line
        feedback = []
        for snapshot in getattr(view, "files", ()) or ():
            for start, end in getattr(snapshot, "errors", ()) or ():
                if end < first_line or start > last_line:
                    continue
                feedback.append(
                    ProverFeedback(
                        getattr(view, "message", "Isabelle reported an error"),
                        FeedbackSeverity.ERROR,
                        f"{start}:{end}",
                    )
                )
        return tuple(feedback)

    def _read_state(self) -> ProofState:
        assert self._theorem is not None
        context = ProvingContext(self._global_entries, ())
        return ProofState(self._theorem, self._goals, context, self._revision)

    async def state(self) -> ProofState:
        self._require("state", LifecycleState.OPEN, LifecycleState.COMPLETE)
        return self._read_state()

    async def _cancel_and_restore(self) -> float:
        """Cancel PIDE's current evaluation and restore the accepted prefix."""
        started = time.monotonic()

        async def recover() -> None:
            from isabelle_mcp.evaluation import cancel_evaluation

            await asyncio.wait_for(
                cancel_evaluation(self._client), min(self._timeout, 30)
            )
            goals, errors = await self._evaluate_on_loop(
                self._commands, self._revision
            )
            if errors:
                raise ProverProtocolError(
                    "Isabelle restore reported an error after cancellation"
                )
            self._goals = goals

        command_timeout = self._timeout
        self._timeout = max(command_timeout, 120)
        try:
            await _ProverLoop.run(recover(), exclusive=True)
        finally:
            self._timeout = command_timeout
        return time.monotonic() - started

    async def apply(self, command: str) -> CommandResult:
        self._require("apply", LifecycleState.OPEN)
        before = self._read_state()
        if not command.strip():
            raise CommandRejectedError(command, before, (
                ProverFeedback("empty command", FeedbackSeverity.ERROR),
            ))
        attempt = self._commands + [command.strip()]
        try:
            goals, errors = await self._evaluate(attempt, self._revision + 1)
        except (ProverTimeoutError, asyncio.TimeoutError) as timeout_error:
            try:
                restore_seconds = await self._cancel_and_restore()
            except BaseException as restore_error:
                await self.close()
                raise timeout_error from restore_error
            logger.info(
                "MITIGATION I2 timeout_rejection restore_seconds=%.3f command=%r",
                restore_seconds,
                command,
            )
            raise CommandRejectedError(command, before, (
                ProverFeedback(
                    f"tactic timed out after {self._timeout} seconds",
                    FeedbackSeverity.ERROR,
                ),
            )) from None
        if errors:
            # Restore the accepted prefix so a rejection leaves no trace in the
            # working copy, and keep the goals the caller already saw.
            await self._evaluate(self._commands, self._revision)
            raise CommandRejectedError(command, before, errors)
        self._commands = attempt
        self._revision += 1
        self._goals = goals
        state = self._read_state()
        if state.is_complete:
            self._lifecycle = LifecycleState.COMPLETE
        return CommandResult(command, state)

    def classify_command(self, command: str) -> CommandKind:
        normalized = command.strip()
        if _UNSOUND.match(normalized):
            return CommandKind.UNSOUND_COMPLETION
        if _STRUCTURAL.match(normalized):
            return CommandKind.STRUCTURAL
        return CommandKind.PROOF_STEP

    def automation_command(self) -> str | None:
        return self._automation

    def helper_lemma_commands(self, spec: HelperLemmaSpec) -> HelperLemmaCommands:
        """Render an Isabelle `subgoal` scope for an agent-selected lemma.

        `have` inside an apply-script needs a structured proof block, so the
        helper statement is introduced with `subgoal_tac`, which leaves the
        helper goal open and closes when it is discharged.
        """
        return HelperLemmaCommands(
            f'apply (subgoal_tac "{spec.statement}")',
            "prefer 2",
            "apply assumption",
        )

    async def checkpoint(self) -> Checkpoint:
        self._require("checkpoint", LifecycleState.OPEN, LifecycleState.COMPLETE)
        self._checkpoints[self._revision] = (tuple(self._commands), self._goals)
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
        commands, goals = self._checkpoints[checkpoint._payload]
        self._commands = list(commands)
        self._revision = checkpoint._payload
        await self._evaluate(self._commands, self._revision)
        self._goals = goals
        self._lifecycle = (
            LifecycleState.COMPLETE if not goals else LifecycleState.OPEN
        )
        return self._read_state()

    async def query(self, command: str) -> QueryResult:
        """Run one native Isabelle diagnostic command in the proof context.

        Any Isar diagnostic command is accepted, which is the same breadth the
        Rocq backend gives for `Search`/`Print`/`Locate`/`About`/`Check`:

        ==================================  ====================================
        purpose                             Isabelle command
        ==================================  ====================================
        search for a pattern                ``find_theorems "_ + _ = _"``
        search for a constant               ``find_consts "int => int"``
        print a fact's statement            ``thm conjI``
        summarise a named theorem           ``print_statement conjI``
        type-check and elaborate a term     ``term "drop 1 xs"``
        show a type                         ``typ "int list"``
        ==================================  ====================================

        The command is evaluated at the current proof position and then removed,
        so the proof itself is unchanged.
        """
        self._require("query", LifecycleState.OPEN, LifecycleState.COMPLETE)
        before = self._read_state()
        stripped = command.strip()
        if not stripped:
            raise CommandRejectedError(command, before, (
                ProverFeedback("empty query", FeedbackSeverity.ERROR),
            ))
        try:
            return await _ProverLoop.run(
                self._query_on_loop(command, stripped, before), exclusive=True
            )
        except BaseException as error:
            if not self._is_client_failure(error):
                raise

        try:
            restart_seconds = await self._restart_and_restore()
        except BaseException as restart_error:
            await self.close()
            raise ProverProtocolError(
                "Isabelle query failed and the prover could not be restarted"
            ) from restart_error
        logger.info(
            "MITIGATION I3 query_restart restore_seconds=%.3f",
            restart_seconds,
        )
        try:
            return await _ProverLoop.run(
                self._query_on_loop(command, stripped, before), exclusive=True
            )
        except BaseException as retry_error:
            if not self._is_client_failure(retry_error):
                raise
            await self.close()
            raise ProverProtocolError(
                "Isabelle query failed after restarting the prover"
            ) from retry_error

    async def _query_on_loop(
        self, command: str, stripped: str, before: ProofState
    ) -> QueryResult:
        from isabelle_mcp.evaluation import evaluate_to, sync_file_locked
        from isabelle_mcp.tools.command_output import command_output
        from isabelle_mcp.utils import MCPLine

        last_line = self._write_working_copy(self._commands + [stripped])
        path = str(self._working_path)
        try:
            await asyncio.wait_for(sync_file_locked(self._client, path), self._timeout)
            view = await asyncio.wait_for(
                evaluate_to(self._client, path, last_line), self._timeout
            )
            view = await self._await_evaluation(view, path)
            errors = self._errors(view, last_line)
            if errors:
                raise CommandRejectedError(command, before, errors)
            result = await asyncio.wait_for(
                command_output(self._client, path, MCPLine(last_line)), self._timeout
            )
        finally:
            # Remove the query from the working copy whatever happened. This is
            # the on-loop form: the exclusive lock is already held here.
            await self._evaluate_on_loop(self._commands, self._revision)
        messages = getattr(result, "messages", ()) or ()
        output = "\n".join(
            getattr(message, "message", "") for message in messages
            if getattr(message, "kind", "") != "state"
        )
        return QueryResult(command, output or "no output")

    async def save_proof(
        self, destination: Path, *, overwrite: bool = False
    ) -> SavedProofCertificate:
        self._require("save_proof", LifecycleState.COMPLETE)
        destination = destination.resolve()
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        assert self._source_text is not None and self._region is not None
        assert self._theorem is not None
        # Isabelle requires a theory's name to match its file name, so the
        # certificate is named after where it is saved. Saving under the
        # original file name therefore reproduces the original header.
        certificate = rename_theory(
            render_certificate(
                self._source_text, self._region, tuple(self._commands)
            ),
            destination.stem,
        )
        # Commands were accepted inside the working theory, so fact references
        # the model qualified with its name must follow the rename or the
        # certificate cannot replay outside the session.
        certificate = certificate.replace(
            f"{_WORKING_THEORY}.", f"{destination.stem}."
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
        commands = tuple(self._commands)
        if not is_terminal_command(commands[-1] if commands else ""):
            commands = commands + ("done",)
        return SavedProofCertificate(
            self._theorem, destination, "isabelle-source", commands
        )

    async def close(self) -> None:
        client = self._client
        temporary_directory = self._temporary_directory
        self._client = None
        self._temporary_directory = None
        self._working_path = None
        self._checkpoints.clear()
        self._session_token = None
        self._lifecycle = LifecycleState.CLOSED
        if client is not None:
            await _ProverLoop.run(self._stop(client))
        if temporary_directory is not None:
            temporary_directory.cleanup()
