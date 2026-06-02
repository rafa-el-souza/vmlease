"""Logic-free template rendering — stdlib only, shell-`$`-safe.

vmlease's templates are cloud-init / shell text, which is full of ``$HOME``,
``$(...)`` and ``$@``. A ``$``-delimited substituter (``string.Template``) would
collide with all of that. So we use a **non-``$`` delimiter** — ``@@name@@`` — and
a tiny ``re`` substitution: shell ``$`` passes through untouched, and only our
named slots are filled.

Templates are dumb by rule: NO loops/conditionals in the
template — all branching lives in Python (``DistroProfile``), so flat variable
interpolation is all that is ever needed (and the reason a templating dependency
like Jinja2 is unwarranted). Rendering is **fail-loud** (StrictUndefined-style):
an unfilled slot or an unused mapping key raises, never silently no-ops.
"""

from __future__ import annotations

import re

# A slot is ``@@name@@`` where name is an identifier-ish token.
_SLOT_RE = re.compile(r"@@([a-zA-Z_][a-zA-Z0-9_]*)@@")


class TemplateError(ValueError):
    """A template referenced an unprovided slot, or a mapping key went unused."""


def find_slots(template_text: str) -> set[str]:
    """Return the set of ``@@name@@`` slot names referenced in ``template_text``."""
    return set(_SLOT_RE.findall(template_text))


def render(template_text: str, mapping: dict[str, str]) -> str:
    """Substitute every ``@@name@@`` from ``mapping``; fail loud on any mismatch.

    Raises :class:`TemplateError` when a slot in the template has no mapping
    entry (an under-specified render) OR a mapping key is never used by the
    template (a stale/typo'd caller). Both directions are checked so a drifted
    template and a drifted caller are caught at render time, not in a silently
    half-filled cloud-init on a real host.
    """
    referenced = find_slots(template_text)
    provided = set(mapping)
    missing = referenced - provided
    if missing:
        raise TemplateError(f"template references unprovided slot(s): {sorted(missing)}")
    unused = provided - referenced
    if unused:
        raise TemplateError(f"mapping has key(s) the template never uses: {sorted(unused)}")
    return _SLOT_RE.sub(lambda m: mapping[m.group(1)], template_text)
