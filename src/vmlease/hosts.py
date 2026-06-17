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
from collections import Counter
from dataclasses import dataclass

from vmlease.distro import FAMILIES, ROLLING, get_profile, host_base_name
from vmlease.model import Os, ResolvedHost

# The entry delimiters of the ``--hosts`` / ``--keep`` grammars. A host name must
# contain none of them (D-12) so the grammars stay unambiguous.
_NAME_DELIMITERS = (",", "@", "=", ":")

# RFC1123 label charset — the universal hostname-safe lower bound every cloud
# provider accepts (NOT a Hetzner-specific rule); ≤63 chars (D-12).
_NAME_CHARSET = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_NAME_MAX_LEN = 63


class HostListError(ValueError):
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
    :class:`HostListError`, but does NOT check the family against the registry or
    apply the name charset (resolve owns both).
    """
    entries: list[HostEntry] = []
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            raise HostListError(
                f"empty host entry in {spec!r} (entries are comma-separated; "
                f"no leading/trailing/double commas)"
            )
        name: str | None
        if "=" in entry:
            name, rest = entry.split("=", 1)
            name = name.strip()
            rest = rest.strip()
            if not name:
                raise HostListError(f"empty host name in entry {entry!r} (use 'name=family[@version]')")
        else:
            name, rest = None, entry
        version: str | None
        if "@" in rest:
            family, version = rest.split("@", 1)
            family = family.strip()
            version = version.strip()
            if not version:
                raise HostListError(f"empty version in entry {entry!r} (use 'family@version')")
        else:
            family, version = rest, None
        if not family:
            raise HostListError(f"empty family in entry {entry!r} (grammar is '[name=]family[@version]')")
        entries.append(HostEntry(name=name, family=family, version=version))
    return entries


def validate_name(name: str) -> None:
    """Validate a host ``name`` fail-closed against the provider-agnostic charset.

    Raises :class:`HostListError` if the name is empty, contains any entry
    delimiter (``, @ = :``), exceeds ``63`` chars, or fails the RFC1123 label
    charset ``^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`` (D-12). Pure / provider-agnostic;
    applies to both explicit and auto-derived names.
    """
    if not name:
        raise HostListError("host name must be non-empty")
    bad = [d for d in _NAME_DELIMITERS if d in name]
    if bad:
        raise HostListError(
            f"host name {name!r} must not contain the entry delimiter(s) {bad} "
            f"(reserved by the --hosts / --keep grammar)"
        )
    if len(name) > _NAME_MAX_LEN:
        raise HostListError(
            f"host name {name!r} is {len(name)} chars; the limit is {_NAME_MAX_LEN}"
        )
    if not _NAME_CHARSET.match(name):
        raise HostListError(
            f"host name {name!r} is not a valid RFC1123 label "
            f"(lowercase a-z, 0-9, '-'; must start/end alphanumeric)"
        )


def resolve(
    entries: list[HostEntry],
    *,
    server_type: str,
    firewall: str = "",
    requires: tuple[str, ...] = (),
) -> list[ResolvedHost]:
    """Resolve parsed entries into :class:`~vmlease.model.ResolvedHost` records.

    Phase 1 (per entry) resolves ``(family, version)`` via
    :func:`~vmlease.distro.get_profile` — a bare entry takes the family default,
    a rolling family takes :data:`~vmlease.distro.ROLLING`, and an unknown family
    OR unknown version (including the bogus family from a grouping-shorthand
    string such as ``"ubuntu@22.04,24.04"``) raises
    :class:`~vmlease.distro.UnknownDistroError`. The provider ``image`` is baked
    from the registry so no version re-resolution happens downstream.

    Phase 2 auto-names the whole run over the resolved multiset (D-6): explicit
    names are used verbatim (validated fail-closed); anonymous entries are named
    bare-family when their family appears once, version-suffixed when a family has
    multiple versions, and index-suffixed when the same ``(family, version)``
    repeats. ``server_type`` / ``firewall`` / ``requires`` are run-wide and stamped
    on every host as given (the CLI passes a canonical ``requires``). Raises
    :class:`HostListError` on a duplicate name (an explicit-name collision).
    """
    profiles = [get_profile(e.family, e.version) for e in entries]
    # A rolling-release family takes no @version: if the user gave one explicitly
    # AND it resolved to the rolling profile, reject it (D-4 / distro-profiles spec:
    # ``arch@<anything>`` SHALL be an error). Programmatic ``get_profile(family,
    # ROLLING)`` callers are unaffected — the guard keys on ``entry.version``.
    for entry, profile in zip(entries, profiles, strict=True):
        if entry.version is not None and profile.version == ROLLING:
            raise HostListError(
                f"rolling-release family {entry.family!r} does not take an "
                f"@version; use bare {entry.family!r}"
            )
    oses = [Os(p.family, p.version) for p in profiles]
    names = _assign_names(entries, oses)
    # Shadow guard (D-7): a host whose name equals a registry family name but is
    # NOT that family is a name/family namespace collision (e.g. ``ubuntu=debian``).
    # The default ``ubuntu`` host (name == its own family) and ``ubuntu=ubuntu@22.04``
    # are allowed — only the cross-family shadow is rejected.
    for name, os in zip(names, oses, strict=True):
        if name in FAMILIES and name != os.family:
            raise HostListError(
                f"host name {name!r} collides with the distro family {name!r} "
                f"(this host is {os.family!r}); rename it to avoid a name/family "
                f"namespace collision"
            )
    return [
        ResolvedHost(
            name=names[i], os=oses[i], image=profiles[i].image,
            server_type=server_type, firewall=firewall, requires=requires,
        )
        for i in range(len(entries))
    ]


def _assign_names(entries: list[HostEntry], oses: list[Os]) -> list[str]:
    """Two-phase auto-naming over the resolved multiset (D-6).

    Counts are computed over the **unnamed** subset only — explicit names do not
    participate in anonymous-repetition disambiguation. Returns one name per entry
    in input order; raises :class:`HostListError` on a duplicate.
    """
    unnamed = [i for i, e in enumerate(entries) if e.name is None]
    family_count: Counter[str] = Counter(oses[i].family for i in unnamed)
    version_count: Counter[Os] = Counter(oses[i] for i in unnamed)
    seen: Counter[Os] = Counter()
    names: list[str] = [""] * len(entries)
    for i, entry in enumerate(entries):
        if entry.name is not None:
            validate_name(entry.name)
            names[i] = entry.name
            continue
        os = oses[i]
        if family_count[os.family] == 1:
            names[i] = os.family
            continue
        base = host_base_name(os.family, os.version)
        if version_count[os] == 1:
            names[i] = base
        else:
            seen[os] += 1
            names[i] = f"{base}-{seen[os]}"
    dupes = sorted(n for n, c in Counter(names).items() if c > 1)
    if dupes:
        raise HostListError(
            f"duplicate host name(s) {dupes}; host names must be unique across the run "
            f"(give each colliding entry a distinct explicit name)"
        )
    return names
