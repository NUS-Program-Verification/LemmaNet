"""Text handling for Isabelle theory files.

This module is pure. It locates the theorem to prove, builds the working copy
the prover evaluates, and renders the saved certificate, without starting
Isabelle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..prover_backend import ProverProtocolError

_THEORY_HEADER = re.compile(r"(?m)^\s*theory\s+(?P<name>\S+)")
# Isabelle names allow primes and underscores, and the declaration ends at the
# colon that introduces the statement.
_DECLARATION = re.compile(
    r"(?m)^\s*(?:theorem|lemma|corollary|proposition)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_'.]*)\s*:"
)
_UNFINISHED = re.compile(r"^\s*(?:sorry|oops)\s*$")
_RELATIVE_IMPORT = re.compile(r'"(?P<path>\./[^"]+)"')
# `done` closes an apply-script; `by`/`qed` close a structured proof.
_TERMINAL = ("done", "qed", "by ", "by(", "..")


@dataclass(frozen=True, slots=True)
class TheoremRegion:
    """Where one theorem's proof lives in a theory file, one-based.

    ``declaration_line`` is where the ``theorem`` keyword sits and
    ``placeholder_line`` is the ``sorry`` that stands for its proof.
    """

    name: str
    declaration_line: int
    placeholder_line: int


def theory_name(source: str) -> str:
    """Return the name in the file's `theory` header."""
    match = _THEORY_HEADER.search(source)
    if match is None:
        raise ProverProtocolError("no Isabelle `theory` header found")
    return match.group("name").strip('"')


def rename_theory(source: str, new_name: str) -> str:
    """Rename the theory header.

    Isabelle requires a theory's name to match its file name, so a working copy
    under a different file name has to be renamed with it.
    """
    match = _THEORY_HEADER.search(source)
    if match is None:
        raise ProverProtocolError("no Isabelle `theory` header found")
    return source[:match.start()] + f"theory {new_name}" + source[match.end():]


def relative_theory_imports(source: str) -> tuple[Path, ...]:
    """Return quoted `./` theory imports as relative `.thy` paths."""
    paths = []
    for match in _RELATIVE_IMPORT.finditer(source):
        path = Path(match.group("path")[2:])
        if path.suffix != ".thy":
            path = path.with_suffix(".thy")
        paths.append(path)
    return tuple(paths)


def declarations(source: str) -> tuple[tuple[str, int], ...]:
    """Return every theorem-like declaration as (name, one-based line)."""
    return tuple(
        (match.group("name"), source.count("\n", 0, match.start()) + 1)
        for match in _DECLARATION.finditer(source)
    )


def locate_theorem(source: str, name: str) -> TheoremRegion:
    """Find a named theorem and the `sorry` that stands in for its proof."""
    found = declarations(source)
    matching = [line for candidate, line in found if candidate == name]
    if not matching:
        known = ", ".join(candidate for candidate, _ in found) or "none"
        raise ProverProtocolError(
            f"no Isabelle theorem named {name!r}; found: {known}"
        )
    declaration_line = matching[0]
    following = [line for _, line in found if line > declaration_line]
    limit = min(following) if following else len(source.splitlines()) + 1
    lines = source.splitlines()
    for number in range(declaration_line, min(limit, len(lines) + 1)):
        if _UNFINISHED.match(lines[number - 1]):
            return TheoremRegion(name, declaration_line, number)
    raise ProverProtocolError(
        f"theorem {name!r} has no `sorry` placeholder to prove"
    )


def discover_theorem_name(source: str) -> str:
    """Return the first theorem left unproved, else the last declaration."""
    found = declarations(source)
    if not found:
        raise ProverProtocolError("no Isabelle theorem declaration found")
    for name, _ in found:
        try:
            locate_theorem(source, name)
        except ProverProtocolError:
            continue
        return name
    return found[-1][0]


def render_working_copy(
    source: str, region: TheoremRegion, commands: Sequence[str]
) -> tuple[str, int]:
    """Replace the placeholder with the commands applied so far.

    Returns the text and the one-based line to evaluate to. With no commands
    the placeholder is kept so the file stays well formed, but the line
    returned is the end of the statement: `sorry` is a terminal command and
    Isabelle prints no proof state for it, so the initial goal has to be read
    from the statement itself.
    """
    lines = source.splitlines()
    if not 1 <= region.placeholder_line <= len(lines):
        raise ProverProtocolError(
            f"placeholder line {region.placeholder_line} is outside the source"
        )
    head = lines[: region.placeholder_line - 1]
    tail = lines[region.placeholder_line:]
    body = (
        ["  " + command.strip() for command in commands]
        if commands
        else [lines[region.placeholder_line - 1]]
    )
    text = "\n".join(head + body + tail)
    if source.endswith("\n"):
        text += "\n"
    return text, len(head) + len(commands)


def is_terminal_command(command: str) -> bool:
    """Whether a command closes the proof rather than transforming goals."""
    normalized = command.strip()
    return normalized in {"done", "qed", ".."} or normalized.startswith(_TERMINAL)


def render_certificate(
    source: str, region: TheoremRegion, commands: Sequence[str]
) -> str:
    """Render the proved theory, closing the proof if it is still open."""
    if not commands:
        raise ProverProtocolError("cannot render a certificate without commands")
    closed = list(commands)
    if not is_terminal_command(closed[-1]):
        closed.append("done")
    text, _ = render_working_copy(source, region, closed)
    return text
