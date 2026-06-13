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

This module imports from :mod:`vmlease.model` only — it is engine-free in this
milestone (the RE2 regex pair lands separately). The describe format is
single-sourced through :func:`_describe` (D10(D)): ``'<key> <value-repr>: <reason>'``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vmlease.model import Assertion, Outcome


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
        _empty_kind("stdout_empty", "stdout"),
        _empty_kind("stderr_empty", "stderr"),
    )
}
