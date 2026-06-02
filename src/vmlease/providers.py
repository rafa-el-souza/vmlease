"""Provider seam — the ``Provider`` Protocol + a Hetzner ``hcloud`` impl.

The provider is the only layer that talks to a cloud. It is a typed Protocol so
the runner composes against an interface a ``FakeProvider`` can satisfy in unit
tests — **no test ever touches the network**.

The Hetzner impl wraps the ``hcloud`` CLI via :mod:`subprocess`. It **never
reads or logs the API token**: it relies on the operator's already-active
``hcloud`` context (``hcloud context create …`` done out-of-band), so the token
lives only in ``~/.config/hcloud/cli.toml`` and is consumed implicitly. This
module builds argv lists (pure, unit-tested) and shells out; the runner's
``plan`` path makes **zero** provider calls.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vmlease.model import Host

if TYPE_CHECKING:
    from vmlease.model import HostSpec

# A subprocess runner: argv -> completed process. The injection seam that lets
# tests drive the Hetzner impl with a fake subprocess (a parameter, not module
# state).
SubprocessRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class ProviderError(RuntimeError):
    """A provider operation failed (non-zero CLI exit, unparseable output)."""


@runtime_checkable
class Provider(Protocol):
    """The cloud seam the runner depends on. Mock this in tests."""

    def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
        """Provision one VM from ``spec`` with ``cloud_init`` user-data; return it once it has an IP."""
        ...

    def destroy(self, host: Host) -> None:
        """Delete one VM. MUST be idempotent (a re-delete is not an error)."""
        ...

    def list_labeled(self, run_id: str) -> list[Host]:
        """Return every live VM carrying this run's ``vmlease=<run-id>`` label."""
        ...


# --------------------------------------------------------------------------- #
# Hetzner argv builders (pure — unit-tested without any subprocess)
# --------------------------------------------------------------------------- #
def _labels_args(labels: dict[str, str]) -> list[str]:
    """Render ``--label k=v`` pairs in a deterministic (sorted) order."""
    out: list[str] = []
    for key in sorted(labels):
        out += ["--label", f"{key}={labels[key]}"]
    return out


def build_create_argv(spec: HostSpec, user_data_path: str) -> list[str]:
    """argv for ``hcloud server create`` from a :class:`HostSpec`.

    Pure: builds the command without running it (the impl runs it). The cloud-init
    user-data is passed via ``--user-data-from-file <path>`` (the impl writes the
    rendered script to a temp file first). No SSH key is registered with the
    provider — the throwaway public key is injected through cloud-init's
    ``authorized_keys`` instead, so nothing lingers in the provider account.
    """
    # NB: `hcloud server create` has NO `--output json` (verified on a real host —
    # it errors `unknown flag: --output`). It prints plain text carrying the id +
    # IPv4, parsed by ``parse_create_text``.
    firewall_args = ["--firewall", spec.firewall] if spec.firewall else []
    return [
        "hcloud",
        "server",
        "create",
        "--name",
        spec.name,
        "--image",
        spec.image,
        "--type",
        spec.server_type,
        "--user-data-from-file",
        user_data_path,
        *firewall_args,
        *_labels_args(spec.labels),
    ]


def build_delete_argv(host: Host) -> list[str]:
    """argv for ``hcloud server delete`` by server id."""
    return ["hcloud", "server", "delete", host.id]


def build_list_argv(run_id: str) -> list[str]:
    """argv for ``hcloud server list`` filtered to this run's label."""
    from vmlease.safety import label_selector

    return [
        "hcloud",
        "server",
        "list",
        "--selector",
        label_selector(run_id),
        "--output",
        "json",
    ]


def parse_create_text(stdout: str, name: str, labels: dict[str, str]) -> Host:
    """Parse the plain-text ``hcloud server create`` output into a :class:`Host`.

    ``create`` prints (observed on a real host)::

        Server 12345678 created
        IPv4: 123.0.0.1
        ...

    The id + IPv4 are scraped from that text; ``name`` + ``labels`` are the ones
    the caller passed to create (not echoed in the output). Raises
    :class:`ProviderError` if the id or IPv4 line is absent.
    """
    id_m = re.search(r"Server\s+(\d+)\s+created", stdout)
    ip_m = re.search(r"IPv4:\s*(\S+)", stdout)
    if not id_m or not ip_m:
        raise ProviderError(f"could not parse id/IPv4 from create output: {stdout[:200]!r}")
    return Host(id=id_m.group(1), name=name, ipv4=ip_m.group(1), labels=dict(labels))


def parse_list_output(stdout: str) -> list[Host]:
    """Parse ``hcloud server list --output json`` into :class:`Host` objects."""
    try:
        doc = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"unparseable list output: {exc}") from exc
    if not isinstance(doc, list):
        raise ProviderError(f"list output is not a JSON array: {stdout[:200]!r}")
    return [_host_from_server(s) for s in doc if isinstance(s, dict)]


def _host_from_server(server: dict[str, object]) -> Host:
    """Extract the ``(id, name, ipv4, labels)`` shape from an hcloud server dict."""
    server_id = server.get("id")
    name = server.get("name")
    if server_id is None or name is None:
        raise ProviderError(f"server object missing id/name: {server!r}")
    public_net = server.get("public_net")
    ipv4 = ""
    if isinstance(public_net, dict):
        ipv4_obj = public_net.get("ipv4")
        if isinstance(ipv4_obj, dict):
            ipv4 = str(ipv4_obj.get("ip") or "")
    raw_labels = server.get("labels")
    labels = {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, dict) else {}
    return Host(id=str(server_id), name=str(name), ipv4=ipv4, labels=labels)


# --------------------------------------------------------------------------- #
# The Hetzner implementation (subprocess; not exercised by Phase-1 unit tests)
# --------------------------------------------------------------------------- #
class HetznerProvider:
    """``hcloud``-CLI-backed provider. Relies on the active context; no token here.

    ``runner`` is an injected callable (defaulting to a sterile
    :func:`subprocess.run` wrapper) so tests can drive the impl with a fake
    subprocess — the determinism/mocking seam.
    """

    def __init__(
        self,
        runner: SubprocessRunner | None = None,
    ) -> None:
        self._run: SubprocessRunner = runner or _default_runner

    def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
        with tempfile.NamedTemporaryFile("w", suffix=".cloudinit", delete=True) as fh:
            fh.write(cloud_init)
            fh.flush()
            proc = self._run(build_create_argv(spec, fh.name))
        if proc.returncode != 0:
            raise ProviderError(f"hcloud server create failed ({proc.returncode}): {proc.stderr}")
        return parse_create_text(proc.stdout, spec.name, dict(spec.labels))

    def destroy(self, host: Host, *, attempts: int = 4, sleep: Callable[[float], None] | None = None) -> None:
        """Delete a server, retrying transient API timeouts (hcloud says "please retry").

        Idempotent: a not-found delete (already reaped) is success. A "request
        timeout" / "please retry" is transient — the deletion usually completes
        server-side anyway — so retry with a short backoff rather than fail the
        run. Only a persistent non-timeout error raises :class:`ProviderError`.
        """
        import time

        _sleep = sleep if sleep is not None else time.sleep
        for attempt in range(attempts):
            proc = self._run(build_delete_argv(host))
            err = (proc.stderr or "").lower()
            if proc.returncode == 0 or "not found" in err:
                return
            transient = "timeout" in err or "please retry" in err or "rate limit" in err
            if not transient or attempt == attempts - 1:
                raise ProviderError(f"hcloud server delete failed ({proc.returncode}): {proc.stderr}")
            _sleep(min(8.0, 2.0 * (attempt + 1)))

    def list_labeled(self, run_id: str) -> list[Host]:
        proc = self._run(build_list_argv(run_id))
        if proc.returncode != 0:
            raise ProviderError(f"hcloud server list failed ({proc.returncode}): {proc.stderr}")
        return parse_list_output(proc.stdout)


def _default_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Sterile subprocess wrapper: capture text, never raise on non-zero."""
    return subprocess.run(argv, capture_output=True, text=True, check=False)
