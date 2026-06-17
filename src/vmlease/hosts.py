"""Host-list expander: the ``--hosts`` / ``--distros`` grammar, parse front-end.

The CLI host axis is a pure **expander** (D-15): a *parse* step turns the
comma-separated ``[name=]family[@version]`` string into unresolved
:class:`HostEntry` records, and a later *resolve* step (added in a subsequent
group) maps entries to fully-resolved host specs (version-defaulting, naming,
registry lookup). Splitting the two keeps every defaulting / naming / validation
rule operating on the entry model rather than on raw strings — a future
file-based host list is then an additive second parse front-end.

This module makes **no provider calls** and does **no** registry lookup: ``parse``
is purely structural (it rejects only empty/malformed entries), and
:func:`validate_name` enforces the provider-agnostic host-name charset (D-12).
Family/version existence and name-validation *application* belong to resolve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The entry delimiters of the ``--hosts`` / ``--keep`` grammars. A host name must
# contain none of them (D-12) so the grammars stay unambiguous.
_NAME_DELIMITERS = (",", "@", "=", ":")

# RFC1123 label charset — the universal hostname-safe lower bound every cloud
# provider accepts (NOT a Hetzner-specific rule); ≤63 chars (D-12).
_NAME_CHARSET = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_NAME_MAX_LEN = 63


class HostSpecError(ValueError):
    """A ``--hosts`` / ``--distros`` entry is structurally malformed, or a host
    name fails the provider-agnostic charset (D-5, D-12)."""


@dataclass(frozen=True)
class HostEntry:
    """One unresolved host-list entry: ``[name=]family[@version]`` parsed but not
    yet resolved.

    ``name`` is the optional explicit identity (``None`` when unnamed — resolve
    auto-names it). ``family`` is the distro family selector as written (NOT yet
    checked against the registry). ``version`` is the requested version (``None``
    when bare — resolve fills the family default).
    """

    name: str | None
    family: str
    version: str | None


def parse(spec: str) -> list[HostEntry]:
    """Parse a ``--hosts`` / ``--distros`` string into :class:`HostEntry` records.

    The comma is the entry separator (no cross-entry grouping shorthand — D-5), so
    ``"ubuntu@22.04,24.04"`` is **two** entries, not a grouped one. Each entry is
    ``[name=]family[@version]``: split on the first ``=`` for the optional name,
    then on the first ``@`` for the optional version. Purely structural — rejects
    empty entries / empty name / empty family / empty version with
    :class:`HostSpecError`, but does NOT check the family against the registry or
    apply the name charset (resolve owns both).
    """
    entries: list[HostEntry] = []
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            raise HostSpecError(
                f"empty host entry in {spec!r} (entries are comma-separated; "
                f"no leading/trailing/double commas)"
            )
        name: str | None
        if "=" in entry:
            name, rest = entry.split("=", 1)
            name = name.strip()
            rest = rest.strip()
            if not name:
                raise HostSpecError(f"empty host name in entry {entry!r} (use 'name=family[@version]')")
        else:
            name, rest = None, entry
        version: str | None
        if "@" in rest:
            family, version = rest.split("@", 1)
            family = family.strip()
            version = version.strip()
            if not version:
                raise HostSpecError(f"empty version in entry {entry!r} (use 'family@version')")
        else:
            family, version = rest, None
        if not family:
            raise HostSpecError(f"empty family in entry {entry!r} (grammar is '[name=]family[@version]')")
        entries.append(HostEntry(name=name, family=family, version=version))
    return entries


def validate_name(name: str) -> None:
    """Validate a host ``name`` fail-closed against the provider-agnostic charset.

    Raises :class:`HostSpecError` if the name is empty, contains any entry
    delimiter (``, @ = :``), exceeds ``63`` chars, or fails the RFC1123 label
    charset ``^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`` (D-12). Pure / provider-agnostic;
    applies to both explicit and auto-derived names.
    """
    if not name:
        raise HostSpecError("host name must be non-empty")
    bad = [d for d in _NAME_DELIMITERS if d in name]
    if bad:
        raise HostSpecError(
            f"host name {name!r} must not contain the entry delimiter(s) {bad} "
            f"(reserved by the --hosts / --keep grammar)"
        )
    if len(name) > _NAME_MAX_LEN:
        raise HostSpecError(
            f"host name {name!r} is {len(name)} chars; the limit is {_NAME_MAX_LEN}"
        )
    if not _NAME_CHARSET.match(name):
        raise HostSpecError(
            f"host name {name!r} is not a valid RFC1123 label "
            f"(lowercase a-z, 0-9, '-'; must start/end alphanumeric)"
        )
