"""Process and framing layer for the Lean 4 REPL.

The REPL reads one JSON object per request and answers with one JSON object,
each terminated by a blank line. Requests are strictly sequential, so a request
that times out leaves an unread answer in the stream. This session marks itself
desynchronized in that case rather than returning a later answer to the wrong
caller.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from utils.logger import setup_logger
from ..prover_backend import ProverProtocolError, ProverTimeoutError

logger = setup_logger("LeanReplSession")

_STDERR_LINES = 50


class LeanReplDrainTimeoutError(ProverTimeoutError):
    """The answer for an earlier timed-out request never arrived."""

    def __init__(self, message: str, drain_seconds: float) -> None:
        self.drain_seconds = drain_seconds
        super().__init__(message)


class LeanReplSession:
    """One Lean REPL subprocess driven over its JSON line protocol."""

    def __init__(
        self,
        repl_path: Path,
        *,
        project_root: Path | None = None,
        timeout: float = 60.0,
        drain_timeout: float | None = None,
        use_lake: bool = True,
    ) -> None:
        self._repl_path = Path(repl_path)
        self._project_root = Path(project_root) if project_root is not None else None
        self._timeout = timeout
        self._drain_timeout = timeout if drain_timeout is None else drain_timeout
        self._use_lake = use_lake and project_root is not None
        self._process: subprocess.Popen[str] | None = None
        self._answers: queue.Queue[str | BaseException] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        self._readers: list[threading.Thread] = []
        self._desynchronized = False
        self._pending_request: dict[str, Any] | None = None

    @property
    def pending_request(self) -> Mapping[str, Any] | None:
        """Return the request whose late answer must be discarded, if any."""
        if self._pending_request is None:
            return None
        return dict(self._pending_request)

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _command(self) -> list[str]:
        if self._use_lake:
            return ["lake", "env", str(self._repl_path)]
        return [str(self._repl_path)]

    def start(self) -> None:
        """Launch the REPL, resolving Lean's search path through lake."""
        if self._process is not None:
            raise ProverProtocolError("Lean REPL session is already started")
        if not self._repl_path.is_file():
            raise ProverProtocolError(f"Lean REPL executable not found: {self._repl_path}")
        if self._project_root is not None and not self._project_root.is_dir():
            raise ProverProtocolError(
                f"Lean project root not found: {self._project_root}"
            )
        environment = dict(os.environ)
        # The REPL streams UTF-8 goals; a non-UTF-8 locale corrupts them.
        environment.setdefault("LANG", "C.UTF-8")
        try:
            self._process = subprocess.Popen(
                self._command(),
                cwd=str(self._project_root) if self._project_root else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=environment,
            )
        except OSError as error:
            raise ProverProtocolError(f"could not start the Lean REPL: {error}") from error
        self._readers = [
            threading.Thread(target=self._read_answers, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _read_answers(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        buffer: list[str] = []
        try:
            for line in self._process.stdout:
                if line.strip() == "":
                    if buffer:
                        self._answers.put("".join(buffer))
                        buffer = []
                    continue
                buffer.append(line)
        except (ValueError, OSError) as error:  # stream closed during shutdown
            self._answers.put(error)
            return
        if buffer:
            self._answers.put("".join(buffer))
        self._answers.put(EOFError("the Lean REPL closed its output stream"))

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            for line in self._process.stderr:
                self._stderr.append(line.rstrip("\n"))
        except (ValueError, OSError):
            return

    def diagnostics(self) -> str:
        return "\n".join(self._stderr)

    def request(
        self, payload: Mapping[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one request and return the decoded answer."""
        if self._pending_request is not None:
            self._drain_late_answer()
        if self._desynchronized:
            raise ProverProtocolError(
                "the Lean REPL session is desynchronized and must be closed"
            )
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise ProverProtocolError(
                f"the Lean REPL is not running: {self.diagnostics() or 'no output'}"
            )
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as error:
            self._desynchronized = True
            raise ProverProtocolError(
                f"could not send a request to the Lean REPL: {error}"
            ) from error
        try:
            wait_seconds = self._timeout if timeout is None else timeout
            answer = self._answers.get(timeout=wait_seconds)
        except queue.Empty:
            self._pending_request = dict(payload)
            raise ProverTimeoutError(
                f"the Lean REPL did not answer within {wait_seconds} seconds"
            ) from None
        return self._decode_answer(answer)

    def _drain_late_answer(self) -> None:
        started = time.monotonic()
        try:
            answer = self._answers.get(timeout=self._drain_timeout)
        except queue.Empty:
            elapsed = time.monotonic() - started
            raise LeanReplDrainTimeoutError(
                "the Lean REPL late answer did not arrive within "
                f"{self._drain_timeout} seconds",
                elapsed,
            ) from None
        self._decode_answer(answer)
        self._pending_request = None
        elapsed = time.monotonic() - started
        logger.info("MITIGATION L1 drain_rejection drain_seconds=%.3f", elapsed)

    def _decode_answer(
        self, answer: str | BaseException
    ) -> dict[str, Any]:
        """Validate one framed response from the reader queue."""
        if isinstance(answer, BaseException):
            self._desynchronized = True
            raise ProverProtocolError(
                f"the Lean REPL stopped: {answer}: {self.diagnostics()}"
            ) from answer
        try:
            decoded = json.loads(answer)
        except json.JSONDecodeError as error:
            self._desynchronized = True
            raise ProverProtocolError(
                f"the Lean REPL returned malformed JSON: {answer[:500]!r}"
            ) from error
        if not isinstance(decoded, dict):
            self._desynchronized = True
            raise ProverProtocolError(
                f"the Lean REPL returned a non-object answer: {answer[:500]!r}"
            )
        return decoded

    def close(self) -> None:
        """Stop the REPL, escalating to a kill if it does not exit."""
        process = self._process
        self._process = None
        self._pending_request = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for reader in self._readers:
            reader.join(timeout=1)
        self._readers = []
        logger.debug("Closed the Lean REPL session")
