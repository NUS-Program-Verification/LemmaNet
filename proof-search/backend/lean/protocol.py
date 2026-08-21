"""Parsing and rendering for the Lean 4 REPL protocol.

This module is pure: it converts REPL payloads and Lean source text into the
typed contract values without starting a prover.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from ..prover_backend import (
    ContextEntry, FeedbackSeverity, Goal, GoalId, ProverFeedback,
    ProverProtocolError,
)

# Lean identifiers exclude the punctuation that separates a hypothesis block
# from its type, so this stays deliberately narrow.
_IDENTIFIER = r"[^\s:(),⊢]+"
_CASE_LINE = re.compile(r"^case\s+(?P<tag>\S+)\s*$")
_HYPOTHESIS_LINE = re.compile(
    rf"^(?P<names>{_IDENTIFIER}(?:[ \t]+{_IDENTIFIER})*)[ \t]:[ \t](?P<type>.*)$"
)
_TURNSTILE = "⊢"

_DECLARATION = re.compile(
    r"(?m)^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:private\s+|protected\s+|noncomputable\s+|partial\s+|unsafe\s+)*"
    r"(?:theorem|lemma)\s+(?P<name>«[^»]+»|[^\s:({\[]+)"
)
_NAMESPACE = re.compile(r"(?m)^\s*namespace\s+(?P<name>\S+)\s*$")
_SORRY_TOKEN = re.compile(r"\bsorry\b")


@dataclass(frozen=True, slots=True)
class Position:
    """One-based line and zero-based column, as reported by the REPL."""

    line: int
    column: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Position":
        return cls(int(payload["line"]), int(payload["column"]))


@dataclass(frozen=True, slots=True)
class SorryGoal:
    """One `sorry` placeholder reported when a Lean file is elaborated."""

    proof_state: int
    goal: str
    start: Position
    end: Position


@dataclass(frozen=True, slots=True)
class DeclarationSpan:
    """Line range of a single Lean declaration, both bounds inclusive."""

    name: str
    qualified_name: str
    first_line: int
    last_line: int

    def contains(self, position: Position) -> bool:
        return self.first_line <= position.line <= self.last_line


def parse_goal(text: str, goal_id: GoalId) -> Goal:
    """Split one pretty-printed Lean goal into hypotheses and a conclusion.

    Lean wraps long hypotheses and conclusions onto indented continuation
    lines, so only unindented lines can start a new entry.
    """
    hypotheses: list[list[str]] = []
    names: list[tuple[str, ...]] = []
    conclusion: list[str] = []
    in_conclusion = False
    for line in text.splitlines():
        if line.startswith(_TURNSTILE):
            in_conclusion = True
            conclusion.append(line[len(_TURNSTILE):].strip())
            continue
        if in_conclusion:
            conclusion.append(line.strip())
            continue
        if _CASE_LINE.match(line):
            continue
        if line[:1].isspace() and hypotheses:
            hypotheses[-1].append(line.strip())
            continue
        match = _HYPOTHESIS_LINE.match(line)
        if match is None:
            continue
        names.append(tuple(match.group("names").split()))
        hypotheses.append([match.group("type")])
    entries = tuple(
        ContextEntry(name, " ".join(part for part in parts if part))
        for group, parts in zip(names, hypotheses)
        for name in group
    )
    return Goal(goal_id, " ".join(part for part in conclusion if part), entries)


def parse_goals(goals: Sequence[str], revision: int) -> tuple[Goal, ...]:
    """Convert the REPL's goal list into typed goals for one revision."""
    return tuple(
        parse_goal(text, GoalId(f"r{revision}-g{index}"))
        for index, text in enumerate(goals)
    )


def goal_case_tag(text: str) -> str | None:
    """Return the `case` tag of a pretty-printed goal, when Lean prints one."""
    for line in text.splitlines():
        match = _CASE_LINE.match(line)
        if match is not None:
            return match.group("tag")
        if line.startswith(_TURNSTILE):
            break
    return None


def parse_messages(payload: Mapping[str, Any]) -> tuple[ProverFeedback, ...]:
    """Convert REPL diagnostics into typed feedback."""
    severities = {
        "error": FeedbackSeverity.ERROR,
        "warning": FeedbackSeverity.WARNING,
        "information": FeedbackSeverity.INFO,
        "info": FeedbackSeverity.INFO,
    }
    feedback = []
    for message in payload.get("messages") or ():
        severity = severities.get(
            str(message.get("severity", "info")).lower(), FeedbackSeverity.INFO
        )
        position = message.get("pos") or {}
        code = (
            f"{position['line']}:{position['column']}"
            if "line" in position and "column" in position
            else None
        )
        feedback.append(ProverFeedback(str(message.get("data", "")), severity, code))
    return tuple(feedback)


def error_feedback(feedback: Iterable[ProverFeedback]) -> tuple[ProverFeedback, ...]:
    return tuple(item for item in feedback if item.severity is FeedbackSeverity.ERROR)


def parse_sorries(payload: Mapping[str, Any]) -> tuple[SorryGoal, ...]:
    """Read the `sorries` block of an elaborated Lean command."""
    sorries = []
    for entry in payload.get("sorries") or ():
        if "pos" not in entry or "endPos" not in entry or entry["endPos"] is None:
            continue
        sorries.append(
            SorryGoal(
                int(entry["proofState"]),
                str(entry.get("goal", "")),
                Position.from_payload(entry["pos"]),
                Position.from_payload(entry["endPos"]),
            )
        )
    return tuple(sorries)


def declaration_spans(source: str) -> tuple[DeclarationSpan, ...]:
    """Locate every `theorem`/`lemma` declaration and its enclosing namespace.

    A declaration runs until the next declaration or the end of the file, which
    is what the REPL's `sorry` positions have to be matched against.
    """
    lines = source.splitlines()
    line_of_offset = []
    offset = 0
    for number, line in enumerate(lines, start=1):
        line_of_offset.append((offset, number))
        offset += len(line) + 1

    def line_number(position: int) -> int:
        result = 1
        for start, number in line_of_offset:
            if start > position:
                break
            result = number
        return result

    namespaces = [
        (match.start(), match.group("name")) for match in _NAMESPACE.finditer(source)
    ]
    matches = list(_DECLARATION.finditer(source))
    spans = []
    for index, match in enumerate(matches):
        name = match.group("name").strip("«»")
        prefix = ".".join(
            namespace for start, namespace in namespaces if start < match.start()
        )
        end = (
            matches[index + 1].start() - 1
            if index + 1 < len(matches)
            else len(source) - 1
        )
        spans.append(
            DeclarationSpan(
                name,
                f"{prefix}.{name}" if prefix else name,
                line_number(match.start()),
                line_number(max(end, match.start())),
            )
        )
    return tuple(spans)


def select_declaration(source: str, theorem_name: str) -> DeclarationSpan:
    """Find one declaration by its short or fully qualified name."""
    spans = declaration_spans(source)
    for span in spans:
        if theorem_name in (span.name, span.qualified_name):
            return span
    known = ", ".join(span.qualified_name for span in spans) or "none"
    raise ProverProtocolError(
        f"no Lean declaration named {theorem_name!r}; found: {known}"
    )


def select_sorry(
    source: str, theorem_name: str, sorries: Sequence[SorryGoal]
) -> tuple[DeclarationSpan, SorryGoal]:
    """Pick the `sorry` that stands for the proof of one named declaration."""
    span = select_declaration(source, theorem_name)
    for sorry in sorries:
        if span.contains(sorry.start):
            return span, sorry
    raise ProverProtocolError(
        f"declaration {span.qualified_name!r} has no open `sorry` to prove"
    )


_CONTEXT_DECLARATION = re.compile(
    r"(?m)^(?:private\s+|protected\s+|noncomputable\s+|partial\s+|unsafe\s+)*"
    r"(?P<keyword>axiom|abbrev|def|theorem|lemma|instance|structure|inductive)\s+"
    r"(?P<name>«[^»]+»|[^\s:({\[]+)(?P<signature>[^\n]*)"
)


def source_declarations(
    source: str, *, before_line: int | None = None
) -> tuple[ContextEntry, ...]:
    """Collect the declarations a Lean file introduces before a given line.

    A VC file carries its own axioms and definitions, and Lean has no cheap way
    to enumerate an environment, so the file itself is the global context.
    """
    entries = []
    for match in _CONTEXT_DECLARATION.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
        if before_line is not None and line >= before_line:
            break
        signature = match.group("signature").strip()
        signature = signature.split(":=", 1)[0].strip() or match.group("keyword")
        entries.append(
            ContextEntry(match.group("name").strip("«»"), signature)
        )
    return tuple(entries)


def discover_theorem_name(source: str) -> str:
    """Return the first declaration proved by `sorry`, else the last one."""
    spans = declaration_spans(source)
    lines = source.splitlines()
    for span in spans:
        body = "\n".join(lines[span.first_line - 1: span.last_line])
        if _SORRY_TOKEN.search(body):
            return span.name
    if spans:
        return spans[-1].name
    raise ProverProtocolError("no Lean theorem declaration found")


def render_certificate(
    source: str, sorry: SorryGoal, tactics: Sequence[str]
) -> str:
    """Replace one `sorry` placeholder with the `by` block that proves it."""
    lines = source.splitlines(keepends=True)
    if not 1 <= sorry.start.line <= len(lines):
        raise ProverProtocolError(f"sorry position outside source: {sorry.start}")
    start_index = sum(len(line) for line in lines[: sorry.start.line - 1])
    start = start_index + sorry.start.column
    end_index = sum(len(line) for line in lines[: sorry.end.line - 1])
    end = end_index + sorry.end.column
    if source[start:end].strip() != "sorry":
        raise ProverProtocolError(
            f"expected `sorry` at {sorry.start}, found {source[start:end]!r}"
        )
    if not tactics:
        raise ProverProtocolError("cannot render a certificate without tactics")
    # Indent relative to the line holding the placeholder, not to the `sorry`
    # column, so that `  := sorry` yields a conventional four-space block.
    sorry_line = lines[sorry.start.line - 1]
    indent = " " * (len(sorry_line) - len(sorry_line.lstrip()) + 2)
    body = f"\n{indent}".join(tactic.strip() for tactic in tactics)
    return f"{source[:start]}by\n{indent}{body}{source[end:]}"
