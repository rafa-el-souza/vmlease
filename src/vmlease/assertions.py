"""Declarative probe assertions — a registry of self-describing predicates.

An assertion is a **named predicate over** :class:`~vmlease.model.Outcome`
(``exit_code``, ``stdout``, ``stderr``). Each TOML key under ``[probe.assert]``
maps to one :class:`AssertionKind` in the ``_ASSERTIONS`` registry, which owns
three responsibilities (D4):

* ``validate(value)`` — a value-shape check that raises on a bad shape (the
  registry key set IS the schema; an unknown key is rejected upstream).
* ``build(value)`` — produce an object satisfying the
  :class:`~vmlease.model.Assertion` Protocol, **bound to** the parsed value.
* the built object's ``evaluate(outcome) -> bool`` / ``describe(outcome) -> str``.

This module imports :mod:`vmlease.model` and the RE2 engine (:mod:`re2`) — the
regex pair (``*_matches``/``*_matches_not``) compiles patterns through
``re2.compile`` (D6). It must NOT import :mod:`vmlease.battery` (which imports
*this* module — a back-import would be circular). The describe format is
single-sourced through :func:`_describe` (D10(D)): ``'<key> <value-repr>: <reason>'``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import re2

from vmlease.model import Assertion, Outcome

if TYPE_CHECKING:
    from re2 import _Regexp as CompiledPattern

# RE2 automaton memory budget per compiled pattern (D8#8/D10(H)). Set EXPLICITLY
# even though it equals RE2's built-in default — we own the bound, so a future
# tightening is a one-line change, never a silently-inherited default.
_RE2_MAX_MEM = 8 * 1024 * 1024


def evaluate(assertions: tuple[Assertion, ...], outcome: Outcome) -> tuple[str, ...]:
    """Evaluate every assertion against ``outcome``; return the FAILED descriptions.

    The runner (:meth:`OpenSshRunner.run_probe`) calls this to compute a probe's
    ``ok`` verdict: an empty result means every assertion held (``ok``); a
    non-empty tuple carries the :meth:`Assertion.describe` of each assertion that
    failed, in authoring order, for the result's ``assertion_failures`` field.
    ``describe`` is only invoked on a failing assertion (its contract).
    """
    return tuple(a.describe(outcome) for a in assertions if not a.evaluate(outcome))


def _describe(key: str, value_repr: str, reason: str) -> str:
    """The one central failure-description format (D10(D)).

    ``'<key> <value-repr>: <reason>'`` — e.g. ``stdout_has "READY": substring
    not found``; ``exit_not 0: exit was 0``; ``stderr_empty: stderr was not
    empty`` (empty ``value_repr`` drops the leading space). Kinds never invent
    phrasing; they route every message through here so the summary surface is
    uniform.
    """
    head = f"{key} {value_repr}" if value_repr else key
    return f"{head}: {reason}"


# --------------------------------------------------------------------------- #
# Shape validation (D8#5/#6) — list values conjoin; an empty list is malformed.
# --------------------------------------------------------------------------- #
def _as_str_list(key: str, value: object) -> tuple[str, ...]:
    """Coerce a ``str | [str]`` value to a tuple of strings; raise on bad shape.

    An empty list ``[]`` is a no-op assertion → rejected naming the key (D8#6).
    """
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        if not value:
            raise ValueError(f"{key}: empty list is not a valid assertion value")
        for element in value:
            if not isinstance(element, str):
                raise ValueError(f"{key}: list values must be strings")
        return tuple(value)
    raise ValueError(f"{key}: expected a string or list of strings")


def _as_int(key: str, value: object) -> int:
    """Coerce an integer assertion value; raise on bad shape.

    Any int is accepted — no 0-255 range check (D8#9). ``bool`` is rejected
    (it is an ``int`` subclass, but ``exit = true`` is a shape error).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key}: expected an integer")
    return value


def _as_bool(key: str, value: object) -> bool:
    """Coerce a boolean assertion value; raise on bad shape."""
    if not isinstance(value, bool):
        raise ValueError(f"{key}: expected a boolean")
    return value


# --------------------------------------------------------------------------- #
# Value-bound assertion objects (one frozen class per family; the "kind bound
# to a value" shape). Each satisfies the model.Assertion Protocol structurally.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _ExitAssertion:
    """``exit`` / ``exit_not`` — integer compare over ``exit_code`` (D8#9)."""

    key: str
    expected: int
    negated: bool

    def evaluate(self, outcome: Outcome) -> bool:
        equal = outcome.exit_code == self.expected
        return (not equal) if self.negated else equal

    def describe(self, outcome: Outcome) -> str:
        reason = f"exit was {outcome.exit_code}"
        return _describe(self.key, str(self.expected), reason)


@dataclass(frozen=True)
class _SubstringAssertion:
    """``*_has`` / ``*_lacks`` — literal substring over a stream (D5/D8#5).

    ``has``: every value present (empty stream → false). ``lacks``: none
    present (empty stream → vacuously true).
    """

    key: str
    values: tuple[str, ...]
    stream: str  # "stdout" | "stderr"
    lacks: bool

    def _text(self, outcome: Outcome) -> str:
        return outcome.stderr if self.stream == "stderr" else outcome.stdout

    def evaluate(self, outcome: Outcome) -> bool:
        text = self._text(outcome)
        if self.lacks:
            return not any(value in text for value in self.values)
        return all(value in text for value in self.values)

    def describe(self, outcome: Outcome) -> str:
        text = self._text(outcome)
        if self.lacks:
            present = next(value for value in self.values if value in text)
            return _describe(self.key, _quote(present), "substring present")
        missing = next(value for value in self.values if value not in text)
        return _describe(self.key, _quote(missing), "substring not found")


@dataclass(frozen=True)
class _EmptyAssertion:
    """``stdout_empty`` / ``stderr_empty`` — strip-empty, bool both ways (D8#4)."""

    key: str
    expect_empty: bool
    stream: str  # "stdout" | "stderr"

    def _text(self, outcome: Outcome) -> str:
        return outcome.stderr if self.stream == "stderr" else outcome.stdout

    def evaluate(self, outcome: Outcome) -> bool:
        is_empty = self._text(outcome).strip() == ""
        return is_empty == self.expect_empty

    def describe(self, outcome: Outcome) -> str:
        reason = (
            f"{self.stream} was not empty"
            if self.expect_empty
            else f"{self.stream} was empty"
        )
        return _describe(self.key, "", reason)


def _quote(value: str) -> str:
    """Render a string value for the describe head — double-quoted (D10(D))."""
    return f'"{value}"'


def _compile_re2(key: str, pattern: str) -> CompiledPattern:
    """Compile one RE2 pattern under the explicit ``max_mem`` budget (D6/D10(H)).

    A compile failure (malformed pattern, unsupported backreference/lookaround,
    or an over-``max_mem`` automaton) surfaces as :class:`re2.error`. Caught
    NARROWLY here and re-raised as :class:`ValueError` carrying the key,
    pattern, and the RE2 message — the §5 loader wraps ``ValueError`` →
    ``BatteryError`` with the probe name, the same path the non-regex kinds use
    (D10(J)). This module never imports ``BatteryError``.
    """
    options = re2.Options()
    options.max_mem = _RE2_MAX_MEM
    options.log_errors = False  # RE2 logs compile errors to stderr by default; we surface them as
                                # BatteryError, so silence RE2's own logging to keep operator stderr clean.
    try:
        return re2.compile(pattern, options=options)
    except re2.error as exc:
        raise ValueError(f"{key} {_quote(pattern)}: invalid regex — {exc}") from exc


@dataclass(frozen=True)
class _RegexAssertion:
    """``*_matches`` / ``*_matches_not`` — RE2 pattern over a stream (D5/D6).

    Patterns are compiled at build/validate time (D10(I)); ``evaluate`` runs an
    UNANCHORED ``rx.search`` (matches anywhere — consistent with substring
    ``_has``; RE2 anchors ``^``/``$`` to the whole text unless ``(?m)``, D8#1).
    A list CONJOINS (D8#5): ``_matches`` → ALL patterns match (empty stream →
    false); ``_matches_not`` → NONE match (empty stream → vacuously true).
    """

    key: str
    patterns: tuple[str, ...]
    compiled: tuple[CompiledPattern, ...]
    stream: str  # "stdout" | "stderr"
    negated: bool

    def _text(self, outcome: Outcome) -> str:
        return outcome.stderr if self.stream == "stderr" else outcome.stdout

    def _matches(self, rx: CompiledPattern, text: str) -> bool:
        return rx.search(text) is not None

    def evaluate(self, outcome: Outcome) -> bool:
        text = self._text(outcome)
        if self.negated:
            return not any(self._matches(rx, text) for rx in self.compiled)
        return all(self._matches(rx, text) for rx in self.compiled)

    def describe(self, outcome: Outcome) -> str:
        text = self._text(outcome)
        pairs = zip(self.patterns, self.compiled, strict=True)
        if self.negated:
            present = next(p for p, rx in pairs if self._matches(rx, text))
            return _describe(self.key, _quote(present), "pattern matched")
        missing = next(p for p, rx in pairs if not self._matches(rx, text))
        return _describe(self.key, _quote(missing), "pattern did not match")


# --------------------------------------------------------------------------- #
# Registry (D4) — key → AssertionKind. The key set IS the schema.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AssertionKind:
    """A self-describing assertion family: validate + build, keyed by TOML key."""

    key: str
    validate: Callable[[object], None]
    build: Callable[[object], Assertion]


def _str_kind(key: str, stream: str, *, lacks: bool) -> AssertionKind:
    def validate(value: object) -> None:
        _as_str_list(key, value)

    def build(value: object) -> Assertion:
        return _SubstringAssertion(key, _as_str_list(key, value), stream, lacks)

    return AssertionKind(key, validate, build)


def _exit_kind(key: str, *, negated: bool) -> AssertionKind:
    def validate(value: object) -> None:
        _as_int(key, value)

    def build(value: object) -> Assertion:
        return _ExitAssertion(key, _as_int(key, value), negated)

    return AssertionKind(key, validate, build)


def _regex_kind(key: str, stream: str, *, negated: bool) -> AssertionKind:
    def compiled_for(value: object) -> tuple[tuple[str, ...], tuple[CompiledPattern, ...]]:
        patterns = _as_str_list(key, value)
        return patterns, tuple(_compile_re2(key, p) for p in patterns)

    def validate(value: object) -> None:
        # Compile here so the pure parse pass (§5 → registry validate) catches a
        # bad pattern at parse, not at evaluation (D10(I)).
        compiled_for(value)

    def build(value: object) -> Assertion:
        patterns, compiled = compiled_for(value)
        return _RegexAssertion(key, patterns, compiled, stream, negated)

    return AssertionKind(key, validate, build)


def _empty_kind(key: str, stream: str) -> AssertionKind:
    def validate(value: object) -> None:
        _as_bool(key, value)

    def build(value: object) -> Assertion:
        return _EmptyAssertion(key, _as_bool(key, value), stream)

    return AssertionKind(key, validate, build)


_ASSERTIONS: dict[str, AssertionKind] = {
    kind.key: kind
    for kind in (
        _exit_kind("exit", negated=False),
        _exit_kind("exit_not", negated=True),
        _str_kind("stdout_has", "stdout", lacks=False),
        _str_kind("stdout_lacks", "stdout", lacks=True),
        _str_kind("stderr_has", "stderr", lacks=False),
        _str_kind("stderr_lacks", "stderr", lacks=True),
        _regex_kind("stdout_matches", "stdout", negated=False),
        _regex_kind("stdout_matches_not", "stdout", negated=True),
        _regex_kind("stderr_matches", "stderr", negated=False),
        _regex_kind("stderr_matches_not", "stderr", negated=True),
        _empty_kind("stdout_empty", "stdout"),
        _empty_kind("stderr_empty", "stderr"),
    )
}
