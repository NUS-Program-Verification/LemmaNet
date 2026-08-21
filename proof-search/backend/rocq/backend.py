"""Rocq backend implemented with CoqPyt."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .session import CoqPytSession
from ..prover_backend import (
    BackendCapabilities, Checkpoint, CommandKind, CommandRejectedError, CommandResult,
    HelperLemmaCommands, HelperLemmaSpec,
    ContextEntry, FeedbackSeverity, Goal, GoalId, InvalidCheckpointError, InvalidLifecycleError,
    LifecycleState, ProofState, ProverBackend, ProverFeedback,
    ProverProtocolError, ProverTimeoutError, ProvingContext, QueryResult,
    SavedProofCertificate, TheoremIdentity,
)


@dataclass(frozen=True, slots=True)
class CoqLibraryPath:
    """Rocq physical-to-logical library mapping used to build `_CoqProject`."""

    path: Path
    logical_name: str

_THEOREM_DECLARATION = re.compile(
    r"(?m)^\s*(?:Lemma|Theorem|Fact|Remark|Corollary|Proposition)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_']*)\b"
)


def discover_theorem_name(source: Path) -> str:
    """Return the first admitted Rocq theorem, or the final declaration."""
    content = source.read_text(encoding="utf-8")
    matches = list(_THEOREM_DECLARATION.finditer(content))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        if re.search(r"\bAdmitted\s*\.", content[match.end():end]):
            return match.group("name")
    if matches:
        return matches[-1].group("name")
    raise ProverProtocolError(f"no Rocq theorem declaration found in {source}")



def _reset_selected_proof(path: Path, theorem_name: str) -> None:
    """Reset one Rocq proof in the private working copy."""
    content = path.read_text(encoding="utf-8")
    declarations = list(_THEOREM_DECLARATION.finditer(content))
    target_index = next(
        (
            index
            for index, declaration in enumerate(declarations)
            if declaration.group("name") == theorem_name
        ),
        None,
    )
    if target_index is None:
        return
    declaration = declarations[target_index]
    block_end = (
        declarations[target_index + 1].start()
        if target_index + 1 < len(declarations)
        else len(content)
    )
    proof = re.search(r"\bProof\s*\.", content[declaration.end():block_end])
    if proof is None:
        return
    proof_end = declaration.end() + proof.end()
    terminator = re.search(
        r"\b(?:Qed|Defined|Admitted)\s*\.", content[proof_end:block_end]
    )
    if terminator is None:
        replacement_end = block_end
    else:
        replacement_end = proof_end + terminator.end()
    reset = content[:proof_end] + "\nAdmitted." + content[replacement_end:]
    path.write_text(reset, encoding="utf-8")

class CoqPytBackend(ProverBackend):
    """Expose CoqPyt through the asynchronous backend contract.

    CoqPyt edits its input file, so this adapter works in a private temporary
    workspace. The copy keeps the source basename for Rocq module identity.
    The source named by ``TheoremIdentity`` changes only through an
    explicit save to that path.
    """

    _capabilities = BackendCapabilities()

    def __init__(
        self, *, timeout: int = 10,
        session_factory: Callable[..., CoqPytSession] = CoqPytSession,
        library_paths: Iterable[CoqLibraryPath] = (),
        coqproject_extra_options: Iterable[str] = (),
    ) -> None:
        self._timeout = timeout
        self._session_factory = session_factory
        self._library_paths = tuple(library_paths)
        self._coqproject_extra_options = tuple(coqproject_extra_options)
        self._lifecycle = LifecycleState.CREATED
        self._session: CoqPytSession | None = None
        self._theorem: TheoremIdentity | None = None
        self._working_path: Path | None = None
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self._backend_token = object()
        self._session_token: object | None = None
        self._revision = 0
        self._commands: list[str] = []
        self._checkpoints: dict[int, tuple[int, tuple[str, ...]]] = {}
        self._certificate_finalized = False

    @property
    def lifecycle(self) -> LifecycleState:
        return self._lifecycle

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._capabilities

    def _require(self, operation: str, *expected: LifecycleState) -> None:
        if self._lifecycle not in expected:
            raise InvalidLifecycleError(operation, self._lifecycle, expected)

    async def _run(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            finally:
                await self.close()
            raise

    async def open(self, theorem: TheoremIdentity) -> ProofState:
        self._require("open", LifecycleState.CREATED)
        source = theorem.source.path.resolve()
        if not source.is_file():
            raise ProverProtocolError(f"Rocq source does not exist: {source}")
        temporary_directory = tempfile.TemporaryDirectory(prefix="lemmanet_coqpyt_")
        working_path = Path(temporary_directory.name) / source.name
        shutil.copy2(source, working_path)
        _reset_selected_proof(working_path, theorem.name)
        library_paths = [
            {"path": str(mapping.path.resolve()), "name": mapping.logical_name}
            for mapping in self._library_paths
        ]
        cache_workspace = json.dumps(
            {
                "workspace": str((theorem.source.workspace or source.parent).resolve()),
                "libraries": [
                    [item["path"], item["name"]] for item in library_paths
                ],
                "coqproject_options": list(self._coqproject_extra_options),
            },
            sort_keys=True,
        )
        session = self._session_factory(
            str(working_path), workspace=temporary_directory.name,
            cache_workspace=cache_workspace,
            library_paths=library_paths,
            auto_setup_coqproject=bool(
                library_paths or self._coqproject_extra_options
            ),
            coqproject_extra_options=list(self._coqproject_extra_options),
            timeout=self._timeout,
        )
        self._temporary_directory = temporary_directory
        self._working_path = working_path
        self._session = session
        try:
            loaded = await self._run(session.load, theorem.name)
            if not loaded:
                message = session.get_last_error() or f"theorem not found: {theorem.name}"
                if "timeout" in message.lower():
                    raise ProverTimeoutError(message)
                raise ProverProtocolError(message)
            self._theorem = theorem
            self._session_token = object()
            self._lifecycle = LifecycleState.OPEN
            state = self._read_state()
            if state.is_complete:
                self._lifecycle = LifecycleState.COMPLETE
            return state
        except BaseException:
            await self.close()
            raise

    def _goal_entries(self, goal) -> tuple[ContextEntry, ...]:
        entries = []
        for hypothesis in goal.hyps:
            entries.extend(
                ContextEntry(name, hypothesis.ty, hypothesis.definition)
                for name in hypothesis.names
            )
        return tuple(entries)

    def _open_goals(self) -> tuple[Goal, ...]:
        assert self._session is not None and self._session.proof_file is not None
        answer = self._session.proof_file.current_goals
        if answer is None or answer.goals is None:
            return ()
        config = answer.goals
        raw_goals = list(config.goals)
        for left, right in config.stack:
            raw_goals.extend(left)
            raw_goals.extend(right)
        raw_goals.extend(config.shelf)
        raw_goals.extend(config.given_up)
        return tuple(
            Goal(GoalId(f"r{self._revision}-g{index}"), goal.ty,
                 self._goal_entries(goal))
            for index, goal in enumerate(raw_goals)
        )

    def _context(self, goals: tuple[Goal, ...]) -> ProvingContext:
        assert self._session is not None
        global_entries = tuple(
            ContextEntry(name, getattr(term, "text", str(term)))
            for name, term in self._session.get_context_terms().items()
        )
        local_entries = goals[0].hypotheses if goals else ()
        return ProvingContext(global_entries, local_entries)

    def _read_state(self) -> ProofState:
        assert self._theorem is not None
        goals = self._open_goals()
        return ProofState(self._theorem, goals, self._context(goals), self._revision)

    async def state(self) -> ProofState:
        self._require("state", LifecycleState.OPEN, LifecycleState.COMPLETE)
        return await self._run(self._read_state)

    async def apply(self, command: str) -> CommandResult:
        self._require("apply", LifecycleState.OPEN)
        if not command.strip():
            before = await self.state()
            raise CommandRejectedError(command, before, (
                ProverFeedback("empty command", FeedbackSeverity.ERROR),
            ))
        before = await self.state()
        assert self._session is not None
        accepted = await self._run(self._session.apply_tactic, command)
        if not accepted:
            message = self._session.get_last_error() or "command rejected by Rocq"
            if "timeout" in message.lower():
                raise ProverTimeoutError(message)
            raise CommandRejectedError(command, before, (
                ProverFeedback(message, FeedbackSeverity.ERROR),
            ))
        self._revision += 1
        self._commands.append(command.strip())
        state = await self.state()
        if state.is_complete:
            self._lifecycle = LifecycleState.COMPLETE
        return CommandResult(command, state)

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
        return "hammer."

    async def checkpoint(self) -> Checkpoint:
        self._require("checkpoint", LifecycleState.OPEN, LifecycleState.COMPLETE)
        assert self._session is not None
        step = self._session.get_current_step_number()
        self._checkpoints[self._revision] = (step, tuple(self._commands))
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
        step, commands = self._checkpoints[checkpoint._payload]
        assert self._session is not None
        if not await self._run(self._session.reset_by_step, step):
            raise ProverProtocolError(
                self._session.get_last_error() or "Rocq rollback failed"
            )
        self._revision = checkpoint._payload
        self._commands = list(commands)
        self._certificate_finalized = False
        self._lifecycle = LifecycleState.OPEN
        state = await self.state()
        if state.is_complete:
            self._lifecycle = LifecycleState.COMPLETE
        return state

    def helper_lemma_commands(
        self, spec: HelperLemmaSpec
    ) -> HelperLemmaCommands:
        return HelperLemmaCommands(
            f"assert ({spec.name}: {spec.statement})", "{", "}"
        )

    async def query(self, command: str) -> QueryResult:
        self._require("query", LifecycleState.OPEN, LifecycleState.COMPLETE)
        before = await self.state()
        assert self._session is not None
        output = await self._run(self._session.search, command)
        error_prefixes = ("Query error:", "Error executing", "Unsupported query")
        if output.startswith(error_prefixes):
            raise CommandRejectedError(command, before, (
                ProverFeedback(output, FeedbackSeverity.ERROR),
            ))
        return QueryResult(command, output)

    async def save_proof(self, destination: Path, *, overwrite: bool = False
                         ) -> SavedProofCertificate:
        self._require("save_proof", LifecycleState.COMPLETE)
        destination = destination.resolve()
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        assert self._session is not None and self._working_path is not None
        if not self._certificate_finalized:
            if not await self._run(self._session.apply_tactic, "Qed."):
                raise ProverProtocolError(
                    self._session.get_last_error() or "Rocq rejected Qed"
                )
            self._certificate_finalized = True
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            shutil.copyfile(self._working_path, temporary_path)
            temporary_path.replace(destination)
        finally:
            temporary_path.unlink(missing_ok=True)
        assert self._theorem is not None
        return SavedProofCertificate(
            self._theorem, destination, "rocq-source", tuple(self._commands) + ("Qed.",)
        )

    async def close(self) -> None:
        session = self._session
        temporary_directory = self._temporary_directory
        self._session = None
        self._working_path = None
        self._temporary_directory = None
        self._checkpoints.clear()
        self._session_token = None
        self._lifecycle = LifecycleState.CLOSED
        if session is not None:
            await asyncio.to_thread(session.close)
        if temporary_directory is not None:
            temporary_directory.cleanup()
