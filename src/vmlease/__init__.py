"""vmlease — provision throwaway hosts, run a probe battery, tear them down.

A small, **project-agnostic** harness for empirical checks that need a fresh,
real host (the "ratified != validated" gate): spin a disposable VM per distro,
run a declarative battery of probes over SSH, capture structured results, and
**always** tear the VM down.

Layering (each seam is a typed Protocol so the layers mock cleanly):

- :mod:`vmlease.model` — frozen dataclasses + enums (the shared vocabulary).
- :mod:`vmlease.providers` — the ``Provider`` Protocol + a Hetzner impl
  (wraps the ``hcloud`` CLI; never reads the token).
- :mod:`vmlease.ssh` — the ``SshRunner`` Protocol + an OpenSSH impl.
- :mod:`vmlease.distro` — per-distro cloud-init / package profiles.
- :mod:`vmlease.battery` — load a declarative battery (probes as data).
- :mod:`vmlease.safety` — run-id/label generation, cost guard, reap.
- :mod:`vmlease.runner` — orchestrate provision -> probe -> teardown.
- :mod:`vmlease.cli` — ``plan | run | status | reap``.

Every layer sits behind a mockable seam, so the whole flow is unit-tested
without touching a network or provisioning a real VM.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
