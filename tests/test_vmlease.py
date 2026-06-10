#!/usr/bin/env python3
"""Unit tests for vmlease — all mocked, NO network, NO real VMs.

stdlib unittest only. Run with:
    uv run python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# vmlease is a real package, so import it directly — this keeps mypy --strict
# name-resolution working for the typed fakes below (importlib.import_module
# would erase the symbol types).
from vmlease import (
    archbuild,
    archimage,
    cli,
    cloudinit,
    distro,
    keypair,
    model,
    providers,
    rescue_image,
    results,
    runner,
    safety,
    ssh,
    templating,
    workload,
)
from vmlease import battery as battery_mod
from vmlease.model import Host, HostSpec, Probe, ProbeResult, ProbeTag


# --------------------------------------------------------------------------- #
# Fakes (the mock seams — no network ever)
# --------------------------------------------------------------------------- #
class FakeProvider:
    """In-memory Provider: records create/destroy/list calls (thread-safe)."""

    def __init__(self) -> None:
        self.created: list[HostSpec] = []
        self.cloud_inits: list[str] = []
        self.destroyed: list[Host] = []
        self._live: dict[str, Host] = {}
        self._lock = threading.Lock()

    def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
        # each host gets a DISTINCT ip so a parallel run mirrors reality.
        with self._lock:
            host = Host(id=f"id-{spec.name}", name=spec.name, ipv4=f"10.0.0.{len(self.created) + 1}", labels=dict(spec.labels))
            self.created.append(spec)
            self.cloud_inits.append(cloud_init)
            self._live[host.id] = host
        return host

    def destroy(self, host: Host) -> None:
        with self._lock:
            self.destroyed.append(host)
            self._live.pop(host.id, None)

    def list_labeled(self, run_id: str) -> list[Host]:
        sel = f"{safety.LABEL_KEY}={run_id}"
        return [h for h in self._live.values() if f"{safety.LABEL_KEY}={h.labels.get(safety.LABEL_KEY)}" == sel]


def _fake_subprocess(
    returncode: int, stdout: str = "", stderr: str = ""
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return _run


def _fake_provider_runner(
    returncode: int, stdout: str = "", stderr: str = ""
) -> providers.SubprocessRunner:
    """A 2-arg provider subprocess seam (argv, timeout) -> CompletedProcess."""

    def _run(argv: list[str], _timeout: float | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return _run


class FakeSshRunner:
    """Scripted SshRunner: returns exit 0 by default, or raises/fails per config."""

    def __init__(
        self, *, fail_on: str | None = None, raise_on: str | None = None, raise_upload: bool = False
    ) -> None:
        self.ran: list[str] = []  # ordered call log: "upload:<remote>" then probe ids
        self.uploads: list[tuple[Path, str]] = []
        self._fail_on = fail_on
        self._raise_on = raise_on
        self._raise_upload = raise_upload

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        self.ran.append(probe.id)
        if self._raise_on is not None and probe.id == self._raise_on:
            raise ssh.SshError(f"boom on {probe.id}")
        code = 7 if probe.id == self._fail_on else 0
        return ProbeResult(probe_id=probe.id, tag=probe.tag, exit_code=code, stdout=f"out-{probe.id}", stderr="")

    def upload(self, host: Host, local: Path, remote: str) -> None:
        self.ran.append(f"upload:{remote}")
        self.uploads.append((local, remote))
        if self._raise_upload:
            raise ssh.SshError(f"upload of {local} failed")

    def wait_until_ready(self, host: Host) -> None:
        # no-op: the fake host is always "ready" (no readiness logged, so the
        # call log stays upload-then-detail like the live readiness gate).
        return None

    def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
        self.ran.append(f"stream:{command}")
        return 0

    def upload_dir(self, host: Host, local: Path, remote: str) -> None:
        self.ran.append(f"upload_dir:{remote}")


def _demo_workload() -> workload.ProbeWorkload:
    """The probe battery as a workload — the injected default for runner tests."""
    return workload.ProbeWorkload(_resolve_toml(_DEMO_BATTERY))


def _fake_keypair(tmp: Path) -> keypair.Keypair:
    d = tmp / "kp"
    d.mkdir(parents=True, exist_ok=True)
    priv = d / "id_ed25519"
    priv.write_text("PRIV", encoding="utf-8")
    return keypair.Keypair(directory=d, private_key_path=priv, public_key="ssh-ed25519 AAAA probe")


# --------------------------------------------------------------------------- #
# TOML battery fixtures — a small authoring helper so fixtures are real bundles
# --------------------------------------------------------------------------- #
def _toml_str(value: str) -> str:
    """Render a Python string as a TOML literal multi-line string (no escaping)."""
    return f"'''{value}'''"


def _battery_toml(name: str, probes: tuple[dict[str, object], ...]) -> str:
    """Build a ``battery.toml`` manifest from probe dicts.

    Each probe dict carries ``id``/``title``/``tag`` plus exactly one of ``run``
    (an inline block) or ``script`` (a path); ``classifies``/``timeout`` optional.
    Probes are written in the given order — that order IS the execution order, so
    callers list probes in their intended authoring (formerly tag-rank) order.
    """
    lines = [f"name = {_toml_str(name)}", ""]
    for p in probes:
        lines.append("[[probe]]")
        lines.append(f"id = {_toml_str(str(p['id']))}")
        lines.append(f"title = {_toml_str(str(p['title']))}")
        lines.append(f"tag = {_toml_str(str(p['tag']))}")
        if "classifies" in p:
            lines.append(f"classifies = {_toml_str(str(p['classifies']))}")
        if "timeout" in p:
            lines.append(f"timeout = {p['timeout']!r}")
        if "script" in p:
            lines.append(f"script = {_toml_str(str(p['script']))}")
        else:
            lines.append(f"run = {_toml_str(str(p['run']))}")
        lines.append("")
    return "\n".join(lines)


def _resolve_toml(manifest: str, scripts: dict[str, str] | None = None) -> model.Battery:
    """Resolve a manifest (plus optional ``{path: contents}`` scripts) to a Battery.

    Writes the bundle to a throwaway temp dir and loads it through the real
    loader, so fixtures exercise the TOML + script-resolution path end-to-end.
    """
    d = Path(tempfile.mkdtemp())
    for rel, contents in (scripts or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    p = d / "battery.toml"
    p.write_text(manifest, encoding="utf-8")
    return battery_mod.load_battery(p)


def _write_battery_bundle(
    d: str, manifest: str, scripts: dict[str, str] | None = None
) -> str:
    """Write a ``battery.toml`` (+ optional script files) into ``d``; return its path."""
    base = Path(d)
    for rel, contents in (scripts or {}).items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    p = base / "battery.toml"
    p.write_text(manifest, encoding="utf-8")
    return str(p)


_DEMO_BATTERY = _battery_toml(
    "demo-battery",
    (
        {"id": "P1", "title": "subid", "run": "grep x /etc/subuid", "tag": "read-only", "classifies": "L2"},
        {"id": "P6", "title": "linger", "run": "loginctl enable-linger", "tag": "mutating:operator-space"},
        {"id": "P12", "title": "batch", "run": "sudo true", "tag": "mutating:host-root"},
    ),
)


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
class TestModel(unittest.TestCase):
    def test_probe_result_ok(self) -> None:
        ok = model.ProbeResult("P1", model.ProbeTag.READ_ONLY, 0, "out", "")
        bad = model.ProbeResult("P1", model.ProbeTag.READ_ONLY, 3, "", "err")
        self.assertTrue(ok.ok)
        self.assertFalse(bad.ok)

    def test_probe_result_timed_out_defaults_false(self) -> None:
        res = model.ProbeResult("P1", model.ProbeTag.READ_ONLY, 0, "out", "")
        self.assertFalse(res.timed_out)

    def test_probe_timeout_defaults_none(self) -> None:
        p = model.Probe(id="P1", title="t", command="c", tag=model.ProbeTag.READ_ONLY)
        self.assertIsNone(p.timeout)

    def test_probe_source_defaults_empty(self) -> None:
        p = model.Probe(id="P1", title="t", command="c", tag=model.ProbeTag.READ_ONLY)
        self.assertEqual(p.source, "")

    def test_probe_source_carries_provenance(self) -> None:
        p = model.Probe(id="P1", title="t", command="c", tag=model.ProbeTag.READ_ONLY, source="prep.sh")
        self.assertEqual(p.source, "prep.sh")

    def test_battery_probes_preserve_authoring_order(self) -> None:
        # probes run + record in authoring order regardless of tag; a read-only
        # probe authored AFTER a host-root probe stays after it (no tag-rank sort).
        manifest = _battery_toml("x", (
            {"id": "SETUP", "title": "setup", "run": "c", "tag": "mutating:host-root"},
            {"id": "VERIFY", "title": "verify", "run": "c", "tag": "read-only"},
        ))
        b = _resolve_toml(manifest)
        self.assertEqual([p.id for p in b.probes], ["SETUP", "VERIFY"])


# --------------------------------------------------------------------------- #
# safety
# --------------------------------------------------------------------------- #
class TestSafety(unittest.TestCase):
    def test_make_run_id_normalizes(self) -> None:
        self.assertEqual(safety.make_run_id("2026-06-01 OpRun!"), "2026-06-01-oprun")

    def test_make_run_id_deterministic(self) -> None:
        self.assertEqual(safety.make_run_id("abc-run"), safety.make_run_id("abc-run"))

    def test_make_run_id_rejects_too_short(self) -> None:
        with self.assertRaises(ValueError):
            safety.make_run_id("a")

    def test_make_run_id_rejects_empty_after_norm(self) -> None:
        with self.assertRaises(ValueError):
            safety.make_run_id("___")

    def test_run_label_and_selector(self) -> None:
        self.assertEqual(safety.run_label("r1"), {"vmlease": "r1"})
        self.assertEqual(safety.label_selector("r1"), "vmlease=r1")

    def test_cost_guard_passes_within_bounds(self) -> None:
        safety.CostGuard().check(["cpx22", "cpx22"])  # no raise

    def test_cost_guard_caps_host_count(self) -> None:
        g = safety.CostGuard(max_hosts=2)
        with self.assertRaises(safety.CostGuardError):
            g.check(["cpx22", "cpx22", "cpx22"])

    def test_cost_guard_rejects_non_allowlisted_type(self) -> None:
        with self.assertRaises(safety.CostGuardError):
            safety.CostGuard().check(["cpx22", "ccx63"])


# --------------------------------------------------------------------------- #
# providers — argv builders + parsers (pure) + impl via injected runner
# --------------------------------------------------------------------------- #
class TestProviderArgv(unittest.TestCase):
    def _spec(self) -> model.HostSpec:
        return model.HostSpec(
            name="vmlease-r1-ubuntu", image="ubuntu-24.04", server_type="cpx22",
            distro_key="ubuntu", labels={"vmlease": "r1", "a": "b"},
        )

    def test_create_argv_firewall_present_and_absent(self) -> None:
        # no firewall on the spec -> no --firewall in argv
        self.assertNotIn("--firewall", providers.build_create_argv(self._spec(), "/tmp/c"))
        # firewall set -> --firewall <name> present
        fw_spec = model.HostSpec(
            name="n", image="i", server_type="cpx22", distro_key="ubuntu",
            labels={"vmlease": "r1"}, firewall="my-firewall",
        )
        argv = providers.build_create_argv(fw_spec, "/tmp/c")
        self.assertIn("--firewall", argv)
        self.assertEqual(argv[argv.index("--firewall") + 1], "my-firewall")

    def test_create_argv_sorted_labels(self) -> None:
        argv = providers.build_create_argv(self._spec(), "/tmp/cloud.init")
        self.assertEqual(argv[:3], ["hcloud", "server", "create"])
        # labels rendered in sorted key order: a=b appears before vmlease=r1
        self.assertLess(argv.index("a=b"), argv.index("vmlease=r1"))
        self.assertIn("--user-data-from-file", argv)
        self.assertEqual(argv[argv.index("--user-data-from-file") + 1], "/tmp/cloud.init")

    def test_delete_and_list_argv(self) -> None:
        host = model.Host(id="42", name="n", ipv4="1.2.3.4")
        self.assertEqual(providers.build_delete_argv(host), ["hcloud", "server", "delete", "42"])
        self.assertIn("vmlease=r1", providers.build_list_argv("r1"))

    def test_parse_create_text(self) -> None:
        # the plain-text shape `hcloud server create` actually prints (observed on a real host)
        out = "Waiting for server ... done\nServer 12345678 created\nIPv4: 123.0.0.1\nIPv6: ...\n"
        host = providers.parse_create_text(out, "vmlease-r1-arch", {"vmlease": "r1"})
        self.assertEqual((host.id, host.ipv4, host.name), ("12345678", "123.0.0.1", "vmlease-r1-arch"))
        self.assertEqual(host.labels, {"vmlease": "r1"})

    def test_parse_create_text_missing_id(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_create_text("IPv4: 1.2.3.4\n", "n", {})

    def test_parse_create_text_missing_ipv4(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_create_text("Server 5 created\n", "n", {})

    def test_parse_list_output(self) -> None:
        out = json.dumps([{"id": 1, "name": "a"}, {"id": 2, "name": "b", "public_net": {"ipv4": {"ip": "5.5.5.5"}}}])
        hosts = providers.parse_list_output(out)
        self.assertEqual([h.id for h in hosts], ["1", "2"])
        self.assertEqual(hosts[1].ipv4, "5.5.5.5")

    def test_parse_list_output_not_array(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_list_output(json.dumps({"not": "a list"}))

    def test_parse_list_output_skips_non_dict_elements(self) -> None:
        out = json.dumps([{"id": 1, "name": "a"}, "garbage", 7])
        hosts = providers.parse_list_output(out)
        self.assertEqual([h.id for h in hosts], ["1"])

    def test_host_from_server_no_ipv4(self) -> None:
        # public_net absent/malformed -> ipv4 falls back to "" (via the list path,
        # which still uses --output json + _host_from_server)
        hosts = providers.parse_list_output(json.dumps([{"id": 3, "name": "n"}]))
        self.assertEqual(hosts[0].ipv4, "")

    def test_parse_list_output_bad_json(self) -> None:
        with self.assertRaises(providers.ProviderError):
            providers.parse_list_output("{not json")


class TestHetznerProviderImpl(unittest.TestCase):
    def _spec(self) -> model.HostSpec:
        return model.HostSpec(name="n", image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "r1"})

    def test_create_success(self) -> None:
        out = "Server 5 created\nIPv4: 1.1.1.1\n"
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, out))
        host = prov.create_with_cloudinit(self._spec(), "#!/bin/bash\necho hi")
        self.assertEqual((host.id, host.ipv4), ("5", "1.1.1.1"))

    def test_create_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.create_with_cloudinit(self._spec(), "#!/bin/bash")

    def test_destroy_idempotent_on_not_found(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "server not found"))
        prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)  # no raise

    def test_destroy_non_transient_error_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "forbidden"))
        with self.assertRaises(providers.ProviderError):
            prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)

    def test_destroy_retries_transient_timeout_then_succeeds(self) -> None:
        # first call times out (transient), second succeeds → no raise, 2 calls
        calls = {"n": 0}

        def flaky(argv: list[str], _timeout: float | None) -> subprocess.CompletedProcess[str]:
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(argv, 1, "", "request timeout, please retry")
            return subprocess.CompletedProcess(argv, 0, "", "")

        prov = providers.HetznerProvider(runner=flaky)
        prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)
        self.assertEqual(calls["n"], 2)

    def test_destroy_persistent_timeout_eventually_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "request timeout, please retry"))
        with self.assertRaises(providers.ProviderError):
            prov.destroy(model.Host(id="9", name="n", ipv4=""), attempts=3, sleep=lambda _s: None)

    def test_destroy_passes_delete_timeout_to_seam(self) -> None:
        # the delete subprocess is bounded — the ctor's delete_timeout reaches the seam
        seen: list[float | None] = []

        def capture(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        prov = providers.HetznerProvider(runner=capture, delete_timeout=37.5)
        prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)
        self.assertEqual(seen, [37.5])

    def test_destroy_default_delete_timeout_is_the_module_default(self) -> None:
        seen: list[float | None] = []

        def capture(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        providers.HetznerProvider(runner=capture).destroy(
            model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None
        )
        self.assertEqual(seen, [providers.DEFAULT_DELETE_TIMEOUT])

    def test_destroy_wedged_subprocess_killed_surfaces_as_provider_error(self) -> None:
        # a delete that never returns (TimeoutExpired) is killed and surfaces as a
        # ProviderError (reap-able teardown failure), NOT a hang, and is NOT retried.
        calls = {"n": 0}

        def wedged(argv: list[str], timeout: float | None) -> subprocess.CompletedProcess[str]:
            calls["n"] += 1
            raise subprocess.TimeoutExpired(argv, timeout or 0.0)

        prov = providers.HetznerProvider(runner=wedged)
        with self.assertRaises(providers.ProviderError):
            prov.destroy(model.Host(id="9", name="n", ipv4=""), attempts=4, sleep=lambda _s: None)
        self.assertEqual(calls["n"], 1)  # timeout is fail-fast, not retried

    def test_list_labeled(self) -> None:
        out = json.dumps([{"id": 1, "name": "a", "labels": {"vmlease": "r1"}}])
        prov = providers.HetznerProvider(runner=_fake_provider_runner(0, out))
        self.assertEqual(len(prov.list_labeled("r1")), 1)

    def test_list_labeled_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_provider_runner(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.list_labeled("r1")

    def test_provider_protocol_satisfied(self) -> None:
        self.assertIsInstance(FakeProvider(), providers.Provider)


# --------------------------------------------------------------------------- #
# battery loader
# --------------------------------------------------------------------------- #
class TestBattery(unittest.TestCase):
    def _one(self, **over: object) -> dict[str, object]:
        base: dict[str, object] = {"id": "P", "title": "t", "run": "c", "tag": "read-only"}
        base.update(over)
        return base

    def test_parse_ok(self) -> None:
        spec = battery_mod.parse_battery(_DEMO_BATTERY)
        self.assertEqual(spec.name, "demo-battery")
        self.assertEqual(len(spec.probes), 3)
        self.assertEqual(spec.probes[0].classifies, "L2")

    def test_resolve_ok(self) -> None:
        # a well-formed manifest with a run probe AND a script probe resolves.
        manifest = _battery_toml("mixed", (
            {"id": "R", "title": "inline", "run": "uname -r", "tag": "read-only"},
            {"id": "S", "title": "scripted", "script": "prep.sh", "tag": "read-only"},
        ))
        b = _resolve_toml(manifest, {"prep.sh": "echo hi\n"})
        self.assertEqual(b.name, "mixed")
        self.assertEqual(b.probes[0].command, "uname -r")
        self.assertEqual(b.probes[0].source, "<inline>")
        self.assertEqual(b.probes[1].command, "echo hi\n")
        self.assertEqual(b.probes[1].source, "prep.sh")

    def test_parse_bad_toml(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery("name = ")

    def test_parse_missing_name(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(_battery_toml("x", (self._one(),)).replace("name = '''x'''\n", ""))

    def test_parse_empty_probes(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery("name = '''x'''\n")

    def test_parse_missing_probe_field(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\nrun = '''c'''\ntag = '''read-only'''\n"
            )

    def test_parse_unknown_tag(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(_battery_toml("x", (self._one(tag="weird"),)))

    def test_parse_neither_run_nor_script(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\ntag = '''read-only'''\n"
            )

    def test_parse_both_run_and_script(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\nscript = '''s.sh'''\n"
            )

    def test_parse_unrecognized_root_key(self) -> None:
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            battery_mod.parse_battery(
                "name = '''x'''\nbogus = '''v'''\n\n[[probe]]\nid = '''P'''\n"
                "title = '''t'''\ntag = '''read-only'''\nrun = '''c'''\n"
            )
        self.assertIn("bogus", str(ctx.exception))

    def test_parse_probe_not_a_table(self) -> None:
        # a `probe` array element that is not a table is rejected.
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery("name = '''x'''\nprobe = ['''not-a-table''']\n")

    def test_parse_run_not_a_string(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = 5\n"
            )

    def test_parse_script_empty_string(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nscript = ''''''\n"
            )

    def test_parse_unrecognized_probe_key(self) -> None:
        # a `timout` typo must fail loud, not silently use the default timeout.
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\ntimout = 30\n"
            )

    def test_parse_duplicate_id(self) -> None:
        manifest = _battery_toml("x", (
            {"id": "P", "title": "t", "run": "c", "tag": "read-only"},
            {"id": "P", "title": "t2", "run": "c2", "tag": "read-only"},
        ))
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(manifest)

    def test_parse_timeout_absent_is_none(self) -> None:
        # back-compat: a battery without per-probe timeout loads, timeout None.
        spec = battery_mod.parse_battery(_DEMO_BATTERY)
        self.assertIsNone(spec.probes[0].timeout)

    def test_parse_timeout_value_carried(self) -> None:
        spec = battery_mod.parse_battery(_battery_toml("x", (self._one(timeout=42),)))
        self.assertEqual(spec.probes[0].timeout, 42.0)

    def test_parse_timeout_non_positive_raises(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(_battery_toml("x", (self._one(timeout=0),)))

    def test_parse_timeout_non_numeric_raises(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\ntimeout = '''soon'''\n"
            )

    def test_parse_timeout_bool_rejected(self) -> None:
        # bool is an int subclass; ``true`` must not be silently read as ``1``.
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(
                "name = '''x'''\n\n[[probe]]\nid = '''P'''\ntitle = '''t'''\n"
                "tag = '''read-only'''\nrun = '''c'''\ntimeout = true\n"
            )

    # --- resolution + symlink-safe containment ---------------------------- #
    def test_resolve_script_absolute_path_rejected(self) -> None:
        manifest = _battery_toml("x", (self._one(script="/etc/passwd"),))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(manifest)

    def test_resolve_script_dotdot_escape_rejected(self) -> None:
        manifest = _battery_toml("x", (self._one(script="../secret.sh"),))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(manifest)

    def test_resolve_script_symlink_out_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as outside_d, tempfile.TemporaryDirectory() as bundle_d:
            secret = Path(outside_d) / "secret.sh"
            secret.write_text("echo leak\n", encoding="utf-8")
            link = Path(bundle_d) / "prep.sh"
            link.symlink_to(secret)  # contained-looking name, out-of-tree target
            manifest = _battery_toml("x", (self._one(script="prep.sh"),))
            (Path(bundle_d) / "battery.toml").write_text(manifest, encoding="utf-8")
            with self.assertRaises(battery_mod.BatteryError):
                battery_mod.load_battery(Path(bundle_d) / "battery.toml")

    def test_resolve_missing_script_names_probe_and_path(self) -> None:
        manifest = _battery_toml("x", (self._one(script="prep.sh"),))
        with self.assertRaises(battery_mod.BatteryError) as ctx:
            _resolve_toml(manifest)
        self.assertIn("'P'", str(ctx.exception))
        self.assertIn("prep.sh", str(ctx.exception))

    def test_resolve_empty_script_file_rejected(self) -> None:
        manifest = _battery_toml("x", (self._one(script="prep.sh"),))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(manifest, {"prep.sh": "   \n\t"})

    def test_resolve_empty_run_block_rejected(self) -> None:
        manifest = _battery_toml("x", (self._one(run="   \n"),))
        with self.assertRaises(battery_mod.BatteryError):
            _resolve_toml(manifest)

    def test_resolve_run_only_battery_no_script_files(self) -> None:
        # a run-only battery loads with no script files present beside it.
        b = _resolve_toml(_DEMO_BATTERY)
        self.assertEqual([p.source for p in b.probes], ["<inline>", "<inline>", "<inline>"])

    def test_load_battery_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = _write_battery_bundle(d, _DEMO_BATTERY)
            b = battery_mod.load_battery(Path(p))
            self.assertEqual(b.name, "demo-battery")

    def test_load_battery_missing_file(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.load_battery(Path("/no/such/battery.toml"))


# --------------------------------------------------------------------------- #
# battery.lint_battery — non-fatal authoring guardrails
# --------------------------------------------------------------------------- #
class TestBatteryLint(unittest.TestCase):
    def _p(self, pid: str, cmd: str, tag: ProbeTag) -> Probe:
        return Probe(id=pid, title=pid, command=cmd, tag=tag)

    def _b(self, *probes: Probe) -> model.Battery:
        return model.Battery(name="t", probes=tuple(probes))

    def test_clean_battery_no_warnings(self) -> None:
        b = self._b(
            self._p("A", "uname -a", ProbeTag.READ_ONLY),
            self._p("B", "do-setup; exit $?", ProbeTag.MUTATING_HOST_ROOT),
        )
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_no_order_warning_for_mixed_tag_order(self) -> None:
        # a read-only probe authored AFTER a host-root probe no longer warns —
        # execution is authoring order, so there is no reorder to surprise on.
        b = self._b(
            self._p("SETUP", "do-setup; exit $?", ProbeTag.MUTATING_HOST_ROOT),
            self._p("VERIFY", "check; exit $?", ProbeTag.READ_ONLY),
        )
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_vacuous_ok_conditional_echo_warns(self) -> None:
        b = self._b(self._p("V", "grep x f && echo OK || echo FAIL", ProbeTag.READ_ONLY))
        warns = battery_mod.lint_battery(b)
        self.assertTrue(any("'V'" in w and "exit $rc" in w for w in warns))

    def test_vacuous_ok_trailing_echo_warns(self) -> None:
        b = self._b(self._p("V", "do-thing; echo DONE", ProbeTag.READ_ONLY))
        self.assertTrue(any("'V'" in w for w in battery_mod.lint_battery(b)))

    def test_exit_gated_command_not_flagged(self) -> None:
        b = self._b(self._p("V", "grep x f && echo OK || echo FAIL; exit $?", ProbeTag.READ_ONLY))
        self.assertEqual(battery_mod.lint_battery(b), ())

    def test_exit_word_inside_echo_string_is_not_gating(self) -> None:
        # the c004 false-negative: "exit" appears only inside an echo string, the
        # command is NOT actually exit-gated -> must still warn (statement-level check)
        b = self._b(self._p("A", 'echo "setup exit: $RC"; check && echo OK || echo FAIL', ProbeTag.READ_ONLY))
        self.assertTrue(any("'A'" in w for w in battery_mod.lint_battery(b)))

    def test_plain_command_not_flagged(self) -> None:
        b = self._b(self._p("V", "uname -a", ProbeTag.READ_ONLY))
        self.assertEqual(battery_mod.lint_battery(b), ())


# --------------------------------------------------------------------------- #
# distro
# --------------------------------------------------------------------------- #
class TestDistro(unittest.TestCase):
    def test_known_profiles(self) -> None:
        for key in distro.DEFAULT_DISTRO_KEYS:
            self.assertEqual(distro.get_profile(key).key, key)

    def test_unknown_profile_raises(self) -> None:
        with self.assertRaises(distro.UnknownDistroError):
            distro.get_profile("plan9")

    def test_debian_has_uidmap(self) -> None:
        self.assertIn("uidmap", distro.get_profile("debian").packages)

    def test_system_update_manager_defaults(self) -> None:
        self.assertIn("apt-get upgrade", distro.system_update_command(distro.get_profile("ubuntu")))
        self.assertIn("dnf -y upgrade", distro.system_update_command(distro.get_profile("fedora")))
        self.assertIn("pacman -Syu", distro.system_update_command(distro.get_profile("arch")))

    def test_system_update_override_wins(self) -> None:
        p = distro.DistroProfile(key="x", default_image="i", package_manager="apt", packages=("p",), system_update_override="custom-update")
        self.assertEqual(distro.system_update_command(p), "custom-update")

    def test_system_update_unknown_manager_raises(self) -> None:
        p = distro.DistroProfile(key="x", default_image="i", package_manager="zypper", packages=("p",))
        with self.assertRaises(distro.UnknownPackageManagerError):
            distro.system_update_command(p)

    def test_profiles_registries_are_read_only(self) -> None:
        # global-state hygiene: the two module-level registries are wrapped in
        # MappingProxyType — being that type IS the read-only guarantee (it has no
        # __setitem__/__delitem__, so any mutation raises at runtime). Assert the
        # type rather than attempting a mutation (which mypy --strict correctly
        # rejects on a Mapping — itself proof the static type is read-only too).
        from types import MappingProxyType

        self.assertIsInstance(distro.PROFILES, MappingProxyType)
        self.assertIsInstance(distro._SYSTEM_UPDATE_BY_MANAGER, MappingProxyType)


# --------------------------------------------------------------------------- #
# runner — build_host_specs + plan (zero provider calls)
# --------------------------------------------------------------------------- #
class TestRunner(unittest.TestCase):
    def _matrix(self, distros: tuple[str, ...] = ("ubuntu", "debian")) -> runner.Matrix:
        return runner.Matrix(
            workload=_demo_workload(),
            distro_keys=tuple(distros),
            server_type="cpx22",
            run_token="run-xyz",
        )

    def test_build_host_specs_labels_and_names(self) -> None:
        specs = runner.build_host_specs(self._matrix())
        self.assertEqual([s.distro_key for s in specs], ["ubuntu", "debian"])
        for s in specs:
            self.assertEqual(s.labels, {"vmlease": "run-xyz"})
            self.assertTrue(s.name.startswith("vmlease-run-xyz-"))
            self.assertEqual(s.firewall, "")  # no firewall by default

    def test_build_host_specs_threads_firewall(self) -> None:
        m = runner.Matrix(_demo_workload(), ("ubuntu",), "cpx22", "run-fw", firewall="my-firewall")
        specs = runner.build_host_specs(m)
        self.assertEqual(specs[0].firewall, "my-firewall")

    def test_build_host_specs_deterministic(self) -> None:
        a = runner.build_host_specs(self._matrix())
        b = runner.build_host_specs(self._matrix())
        self.assertEqual([s.name for s in a], [s.name for s in b])

    def test_plan_makes_no_provider_calls(self) -> None:
        # plan only needs the matrix; a provider is never constructed or passed.
        items = runner.plan(self._matrix())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].workload_summary, "probes=3")

    def test_plan_surfaces_cost_guard(self) -> None:
        m = self._matrix(distros=("ubuntu", "debian", "fedora"))
        with self.assertRaises(safety.CostGuardError):
            runner.plan(m, cost_guard=safety.CostGuard(max_hosts=2))

    def test_plan_unknown_distro(self) -> None:
        m = runner.Matrix(workload=_demo_workload(), distro_keys=("nope",), server_type="cpx22", run_token="t-okay")
        with self.assertRaises(distro.UnknownDistroError):
            runner.plan(m)

    def test_plan_validates_good_upload(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "w.whl"
            local.write_bytes(b"WHEEL")
            m = runner.Matrix(
                _demo_workload(), ("ubuntu",), "cpx22", "run-up",
                uploads=(model.UploadSpec(local=local, remote="~/w.whl"),),
            )
            items = runner.plan(m)
            self.assertEqual(len(items), 1)

    def test_plan_rejects_bad_upload_source(self) -> None:
        # fail-closed: a bad upload aborts plan (zero provider calls) before spend
        m = runner.Matrix(
            _demo_workload(), ("ubuntu",), "cpx22", "run-up",
            uploads=(model.UploadSpec(local=Path("/no/such/file.whl"), remote="~/f.whl"),),
        )
        with self.assertRaises(safety.UploadError):
            runner.plan(m)

    def test_plan_rejects_bad_upload_remote(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "w.whl"
            local.write_bytes(b"WHEEL")
            m = runner.Matrix(
                _demo_workload(), ("ubuntu",), "cpx22", "run-up",
                uploads=(model.UploadSpec(local=local, remote="~/../etc/x"),),
            )
            with self.assertRaises(safety.UploadError):
                runner.plan(m)


# --------------------------------------------------------------------------- #
# cli — plan subcommand (zero provider calls)
# --------------------------------------------------------------------------- #
class TestCli(unittest.TestCase):
    def _write_battery(self, d: str) -> str:
        return _write_battery_bundle(d, _DEMO_BATTERY)

    def test_plan_surfaces_lint_warnings_to_stderr(self) -> None:
        # a vacuously-ok probe → plan warns (non-fatal) on stderr, still exits 0
        with tempfile.TemporaryDirectory() as d:
            manifest = _battery_toml("lint", (
                {"id": "V", "title": "v", "run": "grep x f && echo OK || echo FAIL", "tag": "read-only"},
            ))
            p = Path(_write_battery_bundle(d, manifest))
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = cli.main(["plan", "--battery", str(p), "--distros", "ubuntu", "--run-token", "lint-run"])
            self.assertEqual(rc, 0)  # non-fatal
            self.assertIn("warning:", err.getvalue())
            self.assertIn("exit $rc", err.getvalue())

    def test_plan_ok(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["plan", "--battery", self._write_battery(d), "--distros", "ubuntu,debian", "--run-token", "cli-run"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("demo-battery", out)
            self.assertIn("NOTHING PROVISIONED", out)
            self.assertIn("vmlease-cli-run-ubuntu", out)

    def test_plan_bad_battery_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "battery.toml"
            p.write_text("name = ", encoding="utf-8")
            rc = cli.main(["plan", "--battery", str(p), "--run-token", "cli-run"])
            self.assertEqual(rc, 2)

    def test_plan_cost_guard_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc = cli.main([
                "plan", "--battery", self._write_battery(d),
                "--distros", "ubuntu,debian,fedora,arch", "--max-hosts", "2", "--run-token", "cli-run",
            ])
            self.assertEqual(rc, 2)

    def test_plan_unknown_distro_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc = cli.main(["plan", "--battery", self._write_battery(d), "--distros", "plan9", "--run-token", "cli-run"])
            self.assertEqual(rc, 2)

    def test_parse_upload_default_remote(self) -> None:
        spec = cli._parse_upload("/src/dist/sandbox_ai-0.1.whl")
        self.assertEqual(spec.local, Path("/src/dist/sandbox_ai-0.1.whl"))
        self.assertEqual(spec.remote, "~/sandbox_ai-0.1.whl")

    def test_parse_upload_explicit_remote_splits_first_colon(self) -> None:
        spec = cli._parse_upload("/src/w.whl:/opt/a:b/w.whl")
        self.assertEqual(spec.local, Path("/src/w.whl"))
        self.assertEqual(spec.remote, "/opt/a:b/w.whl")  # split on the FIRST colon only

    def test_upload_flag_repeatable_into_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ns = cli.build_parser().parse_args([
                "plan", "--battery", self._write_battery(d), "--distros", "ubuntu", "--run-token", "cli-run",
                "--upload", "/a/x.whl", "--upload", "/b/y.bin:~/dest/y.bin",
            ])
            m = cli._matrix_from_args(ns, _demo_workload())
            self.assertEqual(len(m.uploads), 2)
            self.assertEqual(m.uploads[0].remote, "~/x.whl")
            self.assertEqual((m.uploads[1].local, m.uploads[1].remote), (Path("/b/y.bin"), "~/dest/y.bin"))

    def test_plan_rejects_bad_upload_no_provider_call(self) -> None:
        # the free-plan fail-closed path: a symlink source is refused at plan,
        # exit 2, and (since plan makes zero provider calls) nothing is provisioned.
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "real.whl"
            target.write_bytes(b"WHEEL")
            link = Path(d) / "link.whl"
            os.symlink(target, link)
            rc = cli.main([
                "plan", "--battery", self._write_battery(d), "--distros", "ubuntu",
                "--run-token", "cli-run", "--upload", str(link),
            ])
            self.assertEqual(rc, 2)


# --------------------------------------------------------------------------- #
# templating
# --------------------------------------------------------------------------- #
class TestTemplating(unittest.TestCase):
    def test_render_fills_slots_and_passes_shell_dollar(self) -> None:
        out = templating.render('hi @@name@@ $HOME $(date)', {"name": "probe"})
        self.assertEqual(out, "hi probe $HOME $(date)")

    def test_render_missing_slot_raises(self) -> None:
        with self.assertRaises(templating.TemplateError):
            templating.render("@@a@@ @@b@@", {"a": "x"})

    def test_render_unused_key_raises(self) -> None:
        with self.assertRaises(templating.TemplateError):
            templating.render("@@a@@", {"a": "x", "b": "y"})

    def test_find_slots(self) -> None:
        self.assertEqual(templating.find_slots("@@a@@ @@b@@ $c"), {"a", "b"})


# --------------------------------------------------------------------------- #
# cloudinit — per-distro render (the apt debian/ubuntu divergence)
# --------------------------------------------------------------------------- #
class TestCloudInit(unittest.TestCase):
    def test_apt_uses_distro_specific_docker_repo(self) -> None:
        deb = cloudinit.render_cloudinit(distro.get_profile("debian"), "probe", "ssh-ed25519 AAAA")
        ubu = cloudinit.render_cloudinit(distro.get_profile("ubuntu"), "probe", "ssh-ed25519 AAAA")
        self.assertIn("download.docker.com/linux/debian", deb)
        self.assertIn("download.docker.com/linux/ubuntu", ubu)
        # debian must NOT carry the ubuntu repo path and vice versa
        self.assertNotIn("download.docker.com/linux/ubuntu", deb)

    def test_operator_and_pubkey_injected(self) -> None:
        out = cloudinit.render_cloudinit(distro.get_profile("ubuntu"), "alice", "ssh-ed25519 KEY alice")
        self.assertIn("alice", out)
        self.assertIn("ssh-ed25519 KEY alice", out)
        # all logic inside a main function; no global-scope mutable vars
        self.assertIn("vmlease_cloudinit_main", out)
        # system refresh + sudoers validation folded in
        self.assertIn("apt-get upgrade", out)
        self.assertIn("visudo -c -f", out)

    def test_arch_extra_setup_nf_tables(self) -> None:
        out = cloudinit.render_cloudinit(distro.get_profile("arch"), "probe", "ssh-ed25519 AAAA")
        self.assertIn("nf_tables", out)
        self.assertIn("pacman", out)

    def test_fedora_dnf(self) -> None:
        out = cloudinit.render_cloudinit(distro.get_profile("fedora"), "probe", "ssh-ed25519 AAAA")
        self.assertIn("dnf -y install", out)

    def test_unknown_manager_raises(self) -> None:
        bad = distro.DistroProfile(key="x", default_image="img", package_manager="zypper", packages=("p",))
        with self.assertRaises(cloudinit.CloudInitError):
            cloudinit.render_install_block(bad)

    # --- finalize fragment: native-image distros set the sentinel in place -- #
    def test_native_distro_finalize_is_byte_identical_ending(self) -> None:
        # The default finalize fragment must reproduce the pre-fragment ending
        # EXACTLY — non-rescue provisioning is unchanged in render, not just
        # behavior. Capture the expected final-step text and assert equality.
        out = cloudinit.render_cloudinit(distro.get_profile("ubuntu"), "probe", "ssh-ed25519 AAAA")
        expected_ending = (
            "  # --- readiness sentinel the harness polls for over SSH ------------------\n"
            "  touch /var/lib/vmlease-ready\n"
            '  echo "vmlease cloud-init complete for $operator (ubuntu)"\n'
            "}\n\n"
            'vmlease_cloudinit_main "$@"\n'
        )
        self.assertTrue(
            out.endswith(expected_ending),
            msg=f"native finalize ending drifted; got tail:\n{out[-len(expected_ending):]!r}",
        )
        # The native path must NOT carry any reboot-resume machinery.
        self.assertNotIn("systemctl reboot", out)
        self.assertNotIn("vmlease-ready.service", out)

    def test_native_finalize_fragment_selected_default(self) -> None:
        self.assertEqual(distro.get_profile("ubuntu").finalize_fragment, distro.FINALIZE_FRAGMENT_DEFAULT)
        self.assertEqual(distro.get_profile("fedora").finalize_fragment, distro.FINALIZE_FRAGMENT_DEFAULT)

    # --- finalize fragment: rescue-write distros reboot into the new kernel - #
    def test_rescue_write_finalize_fragment_selected_reboot_resume(self) -> None:
        # Selection is profile data keyed on needs_rescue_write — arch is the
        # only rescue-write distro today.
        arch = distro.get_profile("arch")
        self.assertTrue(arch.needs_rescue_write)
        self.assertEqual(arch.finalize_fragment, distro.FINALIZE_FRAGMENT_RESCUE_WRITE)

    def test_arch_finalize_reboots_and_defers_sentinel(self) -> None:
        out = cloudinit.render_cloudinit(distro.get_profile("arch"), "probe", "ssh-ed25519 AAAA")
        # (a) kernel-bump detection via the running kernel's modules.dep going missing
        self.assertIn('test -f "/lib/modules/$(uname -r)/modules.dep"', out)
        # (b) once-only reboot guard marker — written AND the reboot branch is
        # actually GATED on it (the marker is checked, not merely created), so a
        # later boot that re-enters this path does not reboot a second time.
        self.assertIn("/var/lib/vmlease-kernel-reboot.done", out)
        self.assertIn('! test -f "$vmlease_reboot_guard"', out)
        # (c) self-disabling systemd oneshot that touches the sentinel next boot
        self.assertIn("/etc/systemd/system/vmlease-ready.service", out)
        self.assertIn("Type=oneshot", out)
        self.assertIn("systemctl disable vmlease-ready.service", out)
        # daemon-reload precedes enable so a not-yet-loaded unit can't make
        # `enable` exit non-zero and abort before the reboot under pipefail
        self.assertIn("systemctl daemon-reload", out)
        self.assertLess(out.index("systemctl daemon-reload"), out.index("systemctl enable vmlease-ready.service"))
        self.assertIn("rm -f /etc/systemd/system/vmlease-ready.service", out)
        # ordered after modules are loaded so the nf_tables/ip_tables drop-in lands
        self.assertIn("After=systemd-modules-load.service", out)
        # (d) the actual reboot
        self.assertIn("systemctl reboot", out)
        # (e) the no-bump / post-reboot branch still touches the sentinel as today
        self.assertIn("touch /var/lib/vmlease-ready", out)

    def test_arch_does_not_touch_sentinel_before_reboot(self) -> None:
        # On the bumped-kernel path the sentinel must NOT be touched before the
        # reboot — the harness must not connect until the upgraded kernel runs.
        # Assert the reboot precedes the (sole) post-reboot touch in the rendered
        # script's bumped-kernel branch.
        out = cloudinit.render_cloudinit(distro.get_profile("arch"), "probe", "ssh-ed25519 AAAA")
        reboot_at = out.index("systemctl reboot")
        # The finalizing sentinel is the bare, indented `touch` command in the
        # else-branch — distinct from the oneshot unit's `ExecStart=/usr/bin/touch`
        # (which legitimately precedes the reboot, as the unit must be written
        # before rebooting). The bare command must come AFTER the reboot.
        bare_touch_at = out.index("    touch /var/lib/vmlease-ready")
        self.assertLess(reboot_at, bare_touch_at, "sentinel touched before reboot on the bumped-kernel path")

    def test_finalize_render_seam_strict_for_all_default_distros(self) -> None:
        # Smoke: the strict render seam holds for arch (reboot-resume) and a
        # native distro — no unfilled or extra slots.
        for key in ("ubuntu", "arch"):
            with self.subTest(distro=key):
                out = cloudinit.render_cloudinit(distro.get_profile(key), "probe", "ssh-ed25519 AAAA")
                self.assertNotIn("@@", out)

    def test_unknown_finalize_fragment_raises(self) -> None:
        # A profile whose finalize_fragment names a nonexistent template fails
        # loud at the render seam (mirrors the unknown-install-manager guard).
        class _BadFinalizeProfile(distro.DistroProfile):
            @property
            def finalize_fragment(self) -> str:
                return "does-not-exist"

        bad = _BadFinalizeProfile(key="x", default_image="img", package_manager="apt", packages=("p",))
        with self.assertRaises(cloudinit.CloudInitError):
            cloudinit.render_finalize_block(bad)


# --------------------------------------------------------------------------- #
# keypair — generate via injected runner; cleanup
# --------------------------------------------------------------------------- #
class TestKeypair(unittest.TestCase):
    def test_generate_success(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            def fake_keygen(argv: list[str]) -> subprocess.CompletedProcess[str]:
                # ssh-keygen writes id_ed25519.pub next to -f; emulate it
                idx = argv.index("-f")
                Path(argv[idx + 1] + ".pub").write_text("ssh-ed25519 AAAA probe\n", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "", "")

            kp = keypair.generate_keypair("run1", runner=fake_keygen, base_dir=Path(d))
            self.assertEqual(kp.public_key, "ssh-ed25519 AAAA probe")
            self.assertTrue(kp.directory.exists())
            kp.cleanup()
            self.assertFalse(kp.directory.exists())

    def test_generate_keygen_failure_raises_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as d, self.assertRaises(keypair.KeypairError):
            keypair.generate_keypair("run1", runner=_fake_subprocess(1, "", "nope"), base_dir=Path(d))

    def test_generate_empty_pubkey_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            def fake_keygen(argv: list[str]) -> subprocess.CompletedProcess[str]:
                idx = argv.index("-f")
                Path(argv[idx + 1] + ".pub").write_text("", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, "", "")

            with self.assertRaises(keypair.KeypairError):
                keypair.generate_keypair("run1", runner=fake_keygen, base_dir=Path(d))

    def test_build_keygen_argv(self) -> None:
        argv = keypair.build_keygen_argv(Path("/tmp/k"), "vmlease-r1")
        self.assertEqual(argv[:4], ["ssh-keygen", "-t", "ed25519", "-N"])


# --------------------------------------------------------------------------- #
# ssh — argv builder + run_probe + readiness poll
# --------------------------------------------------------------------------- #
class TestSsh(unittest.TestCase):
    def _host(self) -> Host:
        return Host(id="1", name="n", ipv4="9.9.9.9")

    def test_build_ssh_argv(self) -> None:
        argv = ssh.build_ssh_argv(self._host(), "probe", Path("/tmp/k"), "id")
        self.assertIn("probe@9.9.9.9", argv)
        self.assertEqual(argv[-1], "id")
        self.assertIn("BatchMode=yes", argv)
        # recycled-IP safety: discard the persistent host-key store so a reused
        # IP with a new host key is never a refused connection (run-2 bug).
        self.assertIn("UserKnownHostsFile=/dev/null", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)

    def test_run_probe_captures_exit(self) -> None:
        r = OpenSshRunnerForTest(_fake_ssh_subprocess(7, "out", "err"))
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        res = r.run_probe(self._host(), probe)
        self.assertEqual((res.exit_code, res.stdout), (7, "out"))
        self.assertFalse(res.timed_out)  # a within-timeout probe is not a timeout

    def test_run_probe_blocking_seam_records_timed_out_result(self) -> None:
        # T1: a slow/blocking transport that raises TimeoutExpired is RECORDED as a
        # timed-out result (exit 124, timed_out=True, partial output), not raised
        # and not a hang.
        def slow(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout, output="partial-out", stderr="partial-err")

        r = OpenSshRunnerForTest(slow)
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        res = r.run_probe(self._host(), probe)
        self.assertTrue(res.timed_out)
        self.assertEqual(res.exit_code, 124)
        self.assertFalse(res.ok)
        self.assertIn("partial-out", res.stdout)  # best-effort partial preserved
        self.assertIn("partial-err", res.stderr)
        self.assertIn("timed out after", res.stderr)  # the note is appended

    def test_run_probe_timeout_empty_partial_is_fine(self) -> None:
        # a platform that doesn't populate partial output → empty, still recorded.
        def slow(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout)

        r = OpenSshRunnerForTest(slow)
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        res = r.run_probe(self._host(), probe)
        self.assertTrue(res.timed_out)
        self.assertEqual(res.stdout, "")
        self.assertIn("timed out after", res.stderr)

    def test_run_probe_resolves_per_probe_timeout(self) -> None:
        # the probe's own timeout wins over the runner default and reaches the seam.
        seen: list[float] = []

        def capture(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner(
            "probe", Path("/tmp/k"), runner=capture, sleeper=lambda _x: None, probe_timeout_default=600.0
        )
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY, timeout=12.5)
        r.run_probe(self._host(), probe)
        self.assertEqual(seen, [12.5])

    def test_run_probe_uses_runner_default_when_probe_has_none(self) -> None:
        seen: list[float] = []

        def capture(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(timeout)
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner(
            "probe", Path("/tmp/k"), runner=capture, sleeper=lambda _x: None, probe_timeout_default=77.0
        )
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        r.run_probe(self._host(), probe)
        self.assertEqual(seen, [77.0])

    def test_build_ssh_argv_has_no_tt(self) -> None:
        # the probe argv must NOT force a PTY (-tt) — that would merge stdout/stderr.
        argv = ssh.build_ssh_argv(self._host(), "probe", Path("/tmp/k"), "id")
        self.assertNotIn("-tt", argv)

    def test_build_scp_argv(self) -> None:
        argv = ssh.build_scp_argv(self._host(), "probe", Path("/tmp/k"), Path("/src/w.whl"), "~/w.whl")
        self.assertEqual(argv[0], "scp")
        # the same recycled-IP hardening as build_ssh_argv
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("UserKnownHostsFile=/dev/null", argv)
        self.assertIn("StrictHostKeyChecking=accept-new", argv)
        self.assertIn("ConnectTimeout=10", argv)
        self.assertIn("-i", argv)
        # the -- option terminator precedes the two positional path args
        self.assertEqual(argv[-3:], ["--", "/src/w.whl", "probe@9.9.9.9:~/w.whl"])

    def test_upload_runs_scp_via_seam(self) -> None:
        seen: list[list[str]] = []

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        r.upload(self._host(), Path("/src/w.whl"), "~/w.whl")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "scp")
        self.assertEqual(seen[0][-1], "probe@9.9.9.9:~/w.whl")

    def test_upload_raises_ssh_error_on_nonzero(self) -> None:
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=_fake_ssh_subprocess(1, "", "no route"), sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError):
            r.upload(self._host(), Path("/src/w.whl"), "~/w.whl")

    def test_upload_timeout_raises_ssh_error_not_timeout_expired(self) -> None:
        # a hung transfer (seam raises TimeoutExpired) becomes SshError, not a raw
        # TimeoutExpired — the transport contract the caller catches.
        def runner_fn(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout)

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError):
            r.upload(self._host(), Path("/src/w.whl"), "~/w.whl")

    def test_wait_until_ready_polls_then_succeeds(self) -> None:
        # fail twice, then the sentinel lands, then the module-tree probe passes
        # (the 4th call): 3 sentinel polls + the in-loop module-tree assertion.
        seq = [1, 1, 0, 0]
        calls = {"n": 0}

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            rc = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return subprocess.CompletedProcess(argv, rc, "6.9.0-arch1\n", "")

        slept: list[float] = []
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=slept.append)
        r.wait_until_ready(self._host(), attempts=5)
        self.assertEqual(calls["n"], 4)  # 3 sentinel polls + 1 module-tree assertion

    def test_wait_until_ready_times_out(self) -> None:
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=_fake_ssh_subprocess(1), sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError):
            r.wait_until_ready(self._host(), attempts=2)

    def test_wait_until_ready_default_budget_spans_a_reboot(self) -> None:
        # the default attempt budget must comfortably span a Hetzner reboot
        # (≈4 min): reboot-resume only touches the sentinel post-reboot. At
        # ≤2s/attempt the budget must clear ~240s.
        import inspect

        default = inspect.signature(ssh.OpenSshRunner.wait_until_ready).parameters["attempts"].default
        self.assertGreaterEqual(default, 120)
        self.assertGreaterEqual(default * 2.0, 240.0)  # 2s backoff cap x budget >= 4 min

    def test_wait_until_ready_rejects_missing_module_tree(self) -> None:
        # sentinel present (exit 0), but the module-tree assertion exits non-zero
        # with the running kernel on stdout: readiness must fail with the skew.
        seen_commands: list[str] = []

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            command = argv[-1]
            seen_commands.append(command)
            if command == "test -f /var/lib/vmlease-ready":
                return subprocess.CompletedProcess(argv, 0, "", "")
            # the module-tree probe: stdout carries the version, exit != 0 = skew
            return subprocess.CompletedProcess(argv, 1, "6.9.0-arch1\n", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError) as ctx:
            r.wait_until_ready(self._host())
        msg = str(ctx.exception)
        self.assertIn("6.9.0-arch1", msg)  # names the running kernel version
        self.assertIn("rescue/modules skew", msg)  # identifies the skew
        # the module-tree assertion actually ran (not just the sentinel)
        self.assertTrue(any("modules.dep" in c for c in seen_commands))

    def test_wait_until_ready_healthy_host_passes(self) -> None:
        # sentinel present AND module-tree probe exits 0 → ready, returns normally.
        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "6.9.0-arch1\n", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        r.wait_until_ready(self._host())  # no raise == ready

    def test_wait_until_ready_retries_module_probe_transport_blip(self) -> None:
        # Right after the post-reboot oneshot touches the sentinel the host is
        # fresh from a reboot: the module probe can hit a transient SSH transport
        # failure (exit 255, EMPTY stdout — the remote command never ran). That
        # must NOT be misread as a skew; the loop retries and succeeds once a
        # later module probe returns exit 0 with a version.
        seq: list[tuple[int, str]] = [
            (0, ""),  # sentinel present
            (255, ""),  # module probe: transport blip, empty stdout → retry
            (0, ""),  # sentinel present
            (0, "6.9.0-arch1\n"),  # module probe: real success → ready
        ]
        calls = {"n": 0}

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            rc, out = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return subprocess.CompletedProcess(argv, rc, out, "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)
        r.wait_until_ready(self._host(), attempts=5)  # no raise == retried past the blip
        self.assertEqual(calls["n"], 4)  # blip retried, did not raise skew

    def test_ssh_runner_protocol_satisfied(self) -> None:
        self.assertIsInstance(FakeSshRunner(), ssh.SshRunner)

    # --- D9.2 streaming command execution + hard-timeout/kill ---

    def test_build_ssh_stream_argv_forces_pty_and_keepalive(self) -> None:
        argv = ssh.build_ssh_stream_argv(self._host(), "probe", Path("/tmp/k"), "make check")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-tt", argv)  # PTY → the dropped client SIGHUPs the remote
        self.assertIn("ServerAliveInterval=15", argv)
        self.assertIn("ServerAliveCountMax=3", argv)
        self.assertIn("UserKnownHostsFile=/dev/null", argv)  # recycled-IP hardening preserved
        self.assertEqual(argv[-1], "make check")

    def test_run_streaming_delivers_output_and_returns_exit(self) -> None:
        seen_argv: list[list[str]] = []

        def fake_stream(argv: list[str], on_output: Callable[[str], None], timeout: float) -> int:
            seen_argv.append(argv)
            on_output("building\n")
            on_output("FAILED\n")
            return 2  # a non-zero gate exit is DATA, not a transport error

        chunks: list[str] = []
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), stream_runner=fake_stream)
        rc = r.run_streaming(self._host(), "make check", chunks.append, timeout=60.0)
        self.assertEqual(rc, 2)
        self.assertEqual(chunks, ["building\n", "FAILED\n"])  # delivered incrementally
        self.assertIn("-tt", seen_argv[0])
        self.assertEqual(seen_argv[0][-1], "make check")

    def test_run_streaming_timeout_raises_ssh_error(self) -> None:
        def fake_stream(argv: list[str], on_output: Callable[[str], None], timeout: float) -> int:
            raise ssh.SshError(f"timed out after {timeout}s and was killed")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), stream_runner=fake_stream)
        with self.assertRaises(ssh.SshError):
            r.run_streaming(self._host(), "sleep 999", lambda _s: None, timeout=1.0)

    def test_default_stream_runner_streams_local_output_and_returns_exit(self) -> None:
        # the REAL default impl, exercised with a LOCAL command (no ssh, no socket):
        # proves the pump-thread + line delivery + exit-code path.
        chunks: list[str] = []
        rc = ssh._default_stream_runner(["printf", "alpha\\nbeta\\n"], chunks.append, 10.0)
        self.assertEqual(rc, 0)
        self.assertEqual("".join(chunks), "alpha\nbeta\n")

    def test_default_stream_runner_nonzero_exit_returned(self) -> None:
        rc = ssh._default_stream_runner(["sh", "-c", "exit 5"], lambda _s: None, 10.0)
        self.assertEqual(rc, 5)  # a non-zero exit is returned, not raised

    def test_default_stream_runner_timeout_kills_and_raises(self) -> None:
        # a local command that outlives the timeout is killed and raises SshError
        with self.assertRaises(ssh.SshError):
            ssh._default_stream_runner(["sleep", "10"], lambda _s: None, 0.2)

    # --- D9.3 recursive directory push (safe symlinks) ---

    def test_build_rsync_argv_safe_links_and_hardening(self) -> None:
        argv = ssh.build_rsync_argv(self._host(), "probe", Path("/tmp/k"), Path("/src/tree"), "~/dest")
        self.assertEqual(argv[0], "rsync")
        self.assertIn("-a", argv)
        self.assertIn("--safe-links", argv)  # out-of-tree symlinks dropped — no exfil
        e_idx = argv.index("-e")
        self.assertIn("UserKnownHostsFile=/dev/null", argv[e_idx + 1])  # hardening rides in -e ssh
        self.assertIn("StrictHostKeyChecking=accept-new", argv[e_idx + 1])
        # -- guard, then source-with-trailing-slash (contents-into-dest), then dest
        self.assertEqual(argv[-3:], ["--", "/src/tree/", "probe@9.9.9.9:~/dest"])

    def test_build_rsync_argv_quotes_key_path_with_space(self) -> None:
        # a space in the key path must not word-split the -e ssh value
        argv = ssh.build_rsync_argv(self._host(), "probe", Path("/tmp/my key/id"), Path("/src/tree"), "~/dest")
        e_val = argv[argv.index("-e") + 1]
        self.assertIn("'/tmp/my key/id'", e_val)

    def test_upload_dir_runs_rsync_via_seam(self) -> None:
        seen: list[list[str]] = []

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as d:
            r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn)
            r.upload_dir(self._host(), Path(d), "~/dest")
        self.assertEqual(seen[0][0], "rsync")
        self.assertIn("--safe-links", seen[0])

    def test_upload_dir_raises_ssh_error_on_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=_fake_ssh_subprocess(23, "", "rsync failed"))
            with self.assertRaises(ssh.SshError):
                r.upload_dir(self._host(), Path(d), "~/dest")

    def test_upload_dir_timeout_raises_ssh_error_not_timeout_expired(self) -> None:
        # a hung directory push (seam raises TimeoutExpired) becomes SshError.
        def runner_fn(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(argv, timeout)

        with tempfile.TemporaryDirectory() as d:
            r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn)
            with self.assertRaises(ssh.SshError):
                r.upload_dir(self._host(), Path(d), "~/dest")

    def test_upload_dir_validates_source_before_transfer(self) -> None:
        seen: list[list[str]] = []

        def runner_fn(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn)
        with self.assertRaises(safety.UploadError):
            r.upload_dir(self._host(), Path("/no/such/dir"), "~/dest")
        self.assertEqual(seen, [])  # validated fail-closed before any rsync


def _fake_ssh_subprocess(
    returncode: int, stdout: str = "", stderr: str = ""
) -> Callable[[list[str], float], subprocess.CompletedProcess[str]]:
    """A 2-arg (argv, timeout) ssh subprocess seam returning a fixed result."""

    def _run(argv: list[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return _run


def OpenSshRunnerForTest(
    runner_fn: Callable[[list[str], float], subprocess.CompletedProcess[str]],
) -> ssh.OpenSshRunner:
    return ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=lambda _x: None)


# --------------------------------------------------------------------------- #
# safety.reap
# --------------------------------------------------------------------------- #
class TestReap(unittest.TestCase):
    def test_reap_destroys_labeled(self) -> None:
        prov = FakeProvider()
        prov.create_with_cloudinit(HostSpec(name="n", image="i", server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "r1"}), "ci")
        reaped = safety.reap(prov, "r1")
        self.assertEqual(len(reaped), 1)
        self.assertEqual(len(prov.destroyed), 1)


# --------------------------------------------------------------------------- #
# safety — upload source / remote-dest validators (fail-closed before spend)
# --------------------------------------------------------------------------- #
class TestUploadValidation(unittest.TestCase):
    def test_source_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.whl"
            f.write_bytes(b"WHEEL")
            safety.validate_upload_source(f)  # no raise

    def test_source_missing_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(Path(d) / "nope.whl")
            self.assertIn("does not exist", str(cm.exception))

    def test_source_symlink_final_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "real.whl"
            target.write_bytes(b"WHEEL")
            link = Path(d) / "link.whl"
            os.symlink(target, link)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(link)
            self.assertIn("symlink", str(cm.exception))

    def test_source_symlink_in_parent_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            realdir = Path(d) / "realdir"
            realdir.mkdir()
            f = realdir / "w.whl"
            f.write_bytes(b"WHEEL")
            linkdir = Path(d) / "linkdir"
            os.symlink(realdir, linkdir)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(linkdir / "w.whl")
            self.assertIn("symlink component", str(cm.exception))

    def test_source_directory_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(Path(d))
            self.assertIn("not a regular file", str(cm.exception))

    def test_source_fifo_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fifo = Path(d) / "pipe"
            os.mkfifo(fifo)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_source(fifo)
            self.assertIn("not a regular file", str(cm.exception))

    def test_source_unreadable_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.whl"
            f.write_bytes(b"WHEEL")
            os.chmod(f, 0o000)
            try:
                with self.assertRaises(safety.UploadError) as cm:
                    safety.validate_upload_source(f)
                self.assertIn("not readable", str(cm.exception))
            finally:
                os.chmod(f, 0o600)  # restore so TemporaryDirectory can clean up

    def test_dir_source_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            safety.validate_upload_dir_source(Path(d))  # no raise — a plain readable dir

    def test_dir_source_missing_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_dir_source(Path(d) / "nope")
            self.assertIn("does not exist", str(cm.exception))

    def test_dir_source_symlink_final_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            realdir = Path(d) / "realdir"
            realdir.mkdir()
            link = Path(d) / "linkdir"
            os.symlink(realdir, link)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_dir_source(link)
            self.assertIn("symlink", str(cm.exception))

    def test_dir_source_symlink_in_parent_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            realdir = Path(d) / "realdir"
            (realdir / "sub").mkdir(parents=True)
            linkdir = Path(d) / "linkdir"
            os.symlink(realdir, linkdir)
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_dir_source(linkdir / "sub")
            self.assertIn("symlink component", str(cm.exception))

    def test_dir_source_non_directory_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.whl"
            f.write_bytes(b"WHEEL")
            with self.assertRaises(safety.UploadError) as cm:
                safety.validate_upload_dir_source(f)
            self.assertIn("not a directory", str(cm.exception))

    def test_dir_source_unreadable_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "sub"
            sub.mkdir()
            os.chmod(sub, 0o000)
            try:
                with self.assertRaises(safety.UploadError) as cm:
                    safety.validate_upload_dir_source(sub)
                self.assertIn("not readable", str(cm.exception))
            finally:
                os.chmod(sub, 0o700)  # restore so TemporaryDirectory can clean up

    def test_remote_happy_path(self) -> None:
        safety.validate_remote_dest("~/w.whl")  # no raise
        safety.validate_remote_dest("/opt/app/wheels/w-1.0+local.whl")  # no raise

    def test_remote_empty_refused(self) -> None:
        with self.assertRaises(safety.UploadError) as cm:
            safety.validate_remote_dest("")
        self.assertIn("empty", str(cm.exception))

    def test_remote_traversal_refused(self) -> None:
        with self.assertRaises(safety.UploadError) as cm:
            safety.validate_remote_dest("~/../../etc/cron.d/x")
        self.assertIn("..", str(cm.exception))

    def test_remote_leading_dash_refused(self) -> None:
        with self.assertRaises(safety.UploadError) as cm:
            safety.validate_remote_dest("-oProxyCommand=evil")
        self.assertIn("-", str(cm.exception))

    def test_remote_bad_char_refused(self) -> None:
        with self.assertRaises(safety.UploadError) as cm:
            safety.validate_remote_dest("~/w.whl; rm -rf /")
        self.assertIn("disallowed character", str(cm.exception))


# --------------------------------------------------------------------------- #
# results serialization
# --------------------------------------------------------------------------- #
class TestResults(unittest.TestCase):
    def _run(self) -> model.HostRun:
        spec = HostSpec(name="vmlease-r1-ubuntu", image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu")
        res = (
            ProbeResult("P1", ProbeTag.READ_ONLY, 0, "ok", ""),
            ProbeResult("P2", ProbeTag.READ_ONLY, 124, "", "probe timed out after 5.0s", timed_out=True),
        )
        return model.HostRun(host_spec=spec, detail="## os-release\nID=ubuntu", results=res)

    def test_filename_deterministic(self) -> None:
        self.assertEqual(results.results_filename("r1", "20260601T000000Z"), "vmlease-r1-20260601T000000Z.json")

    def test_serialize_round_trips(self) -> None:
        text = results.serialize_run("r1", "20260601T000000Z", [self._run()])
        doc = json.loads(text)
        self.assertEqual(doc["run_id"], "r1")
        self.assertEqual(doc["hosts"][0]["probes"][0]["id"], "P1")
        self.assertTrue(doc["hosts"][0]["probes"][0]["ok"])
        self.assertFalse(doc["hosts"][0]["probes"][0]["timed_out"])  # normal probe
        self.assertEqual(doc["hosts"][0]["probes"][1]["exit_code"], 124)
        self.assertTrue(doc["hosts"][0]["probes"][1]["timed_out"])  # timed-out probe is marked in the JSON

    def test_write_results_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = results.write_results(Path(d) / "out", "r1", "20260601T000000Z", [self._run()])
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "vmlease-r1-20260601T000000Z.json")

    def _run_named(self, name: str) -> model.HostRun:
        spec = HostSpec(name=name, image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu")
        return model.HostRun(host_spec=spec, detail="ok", results=())

    def test_incremental_writer_path_known_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = results.IncrementalResultsWriter(Path(d) / "out", "r1", "20260601T000000Z")
            self.assertEqual(w.path.name, "vmlease-r1-20260601T000000Z.json")
            self.assertFalse(w.path.exists())  # deterministic, but nothing written yet

    def test_incremental_writer_accumulates_each_host(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            w = results.IncrementalResultsWriter(Path(d) / "out", "r1", "20260601T000000Z")
            path = w.add(self._run_named("host-a"))
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([h["name"] for h in doc["hosts"]], ["host-a"])  # first host present
            path2 = w.add(self._run_named("host-b"))
            self.assertEqual(path2, w.path)  # same deterministic file
            doc2 = json.loads(path2.read_text(encoding="utf-8"))
            self.assertEqual([h["name"] for h in doc2["hosts"]], ["host-a", "host-b"])  # both now


# --------------------------------------------------------------------------- #
# runner.execute — the teardown-ALWAYS guarantee
# --------------------------------------------------------------------------- #
class TestExecute(unittest.TestCase):
    def _matrix(self, distros: tuple[str, ...] = ("ubuntu", "debian")) -> runner.Matrix:
        return runner.Matrix(_demo_workload(), distros, "cpx22", "run-xyz")

    def _factory(self, ssh_runner: ssh.SshRunner) -> Callable[[str, keypair.Keypair], ssh.SshRunner]:
        return lambda _op, _kp: ssh_runner

    def test_happy_path_provisions_probes_teardown(self) -> None:
        prov = FakeProvider()
        fssh = FakeSshRunner()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(), prov, self._factory(fssh), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(prov.created), 2)
        self.assertEqual(len(prov.destroyed), 2)  # ALWAYS torn down
        # battery ran in authoring order on each host (P1 first, P12 authored last)
        self.assertEqual(fssh.ran[-1], "P12")

    def _upload_matrix(self, remote: str = "~/w.whl") -> tuple[runner.Matrix, Path]:
        # a matrix carrying one upload; returns it plus the (real, valid) local file
        tmp = Path(tempfile.mkdtemp())
        local = tmp / "w.whl"
        local.write_bytes(b"WHEEL")
        m = runner.Matrix(
            _demo_workload(), ("ubuntu",), "cpx22", "run-up",
            uploads=(model.UploadSpec(local=local, remote=remote),),
        )
        return m, local

    def test_upload_precedes_first_probe(self) -> None:
        # the lifecycle contract: upload lands after readiness, before detail/battery
        prov = FakeProvider()
        fssh = FakeSshRunner()
        m, local = self._upload_matrix()
        with tempfile.TemporaryDirectory() as d:
            runner.execute(m, prov, self._factory(fssh), _fake_keypair(Path(d)), "probe")
        self.assertEqual(fssh.uploads, [(local, "~/w.whl")])
        # "upload:~/w.whl" appears in the call log BEFORE the first probe (_detail)
        self.assertEqual(fssh.ran[0], "upload:~/w.whl")
        self.assertEqual(fssh.ran[1], "_detail")

    def test_upload_failure_is_host_error_and_tears_down(self) -> None:
        # an upload that raises SshError is a transport host-failure (error HostRun),
        # NOT a probe non-zero — and the host is still torn down.
        prov = FakeProvider()
        failing = FakeSshRunner(raise_upload=True)
        m, _local = self._upload_matrix()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, self._factory(failing), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].detail.startswith("ERROR:"))  # transport failure recorded
        self.assertEqual(runs[0].results, ())  # no probes ran
        self.assertEqual(len(prov.destroyed), 1)  # torn down despite the failure

    def test_cloudinit_rendered_per_distro(self) -> None:
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            runner.execute(self._matrix(("debian", "ubuntu")), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe")
        self.assertIn("linux/debian", prov.cloud_inits[0])
        self.assertIn("linux/ubuntu", prov.cloud_inits[1])

    def test_teardown_fires_when_probe_raises(self) -> None:
        # load-bearing safety: a mid-probe transport raise still tears down AND
        # is recorded as an error HostRun (per-host isolation — no propagation).
        prov = FakeProvider()
        raising = FakeSshRunner(raise_on="P6")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu",)), prov, self._factory(raising), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(prov.created), 1)
        self.assertEqual(len(prov.destroyed), 1)  # destroyed despite the raise
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].detail.startswith("ERROR:"))  # recorded, not lost
        self.assertEqual(runs[0].results, ())

    def test_one_host_failure_does_not_discard_others(self) -> None:
        # the first-run lesson: a later host's failure must NOT lose earlier
        # hosts' results. ubuntu probes fine; debian's transport raises.
        prov = FakeProvider()

        class _SelectiveSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                if "debian" in host.name:
                    raise ssh.SshError("debian unreachable")
                return ProbeResult(probe.id, probe.tag, 0, "ok", "")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu", "debian")), prov, lambda _o, _k: _SelectiveSsh(), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 2)  # BOTH recorded
        self.assertEqual(len(prov.destroyed), 2)  # both torn down
        ubuntu = next(r for r in runs if r.host_spec.distro_key == "ubuntu")
        debian = next(r for r in runs if r.host_spec.distro_key == "debian")
        self.assertTrue(ubuntu.results)  # ubuntu data preserved
        self.assertTrue(debian.detail.startswith("ERROR:"))  # debian error captured

    def test_teardown_cleans_keypair(self) -> None:
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            kp = _fake_keypair(Path(d))
            runner.execute(self._matrix(("ubuntu",)), prov, self._factory(FakeSshRunner()), kp, "probe")
            self.assertFalse(kp.directory.exists())

    def test_probe_nonzero_exit_is_captured_not_raised(self) -> None:
        prov = FakeProvider()
        failing = FakeSshRunner(fail_on="P12")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu",)), prov, self._factory(failing), _fake_keypair(Path(d)), "probe")
        p12 = next(r for r in runs[0].results if r.probe_id == "P12")
        self.assertFalse(p12.ok)  # captured, not raised

    def test_parallel_preserves_order_and_tears_down_all(self) -> None:
        prov = FakeProvider()
        m = runner.Matrix(_demo_workload(), ("ubuntu", "debian", "fedora"), "cpx22", "run-par")
        # a fresh FakeSshRunner per host (thread-safe-ish; each host independent)
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, lambda _o, _k: FakeSshRunner(), _fake_keypair(Path(d)), "probe", max_parallel=3)
        # results in MATRIX order regardless of completion order
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian", "fedora"])
        self.assertEqual(len(prov.created), 3)
        self.assertEqual(len(prov.destroyed), 3)  # all torn down

    def test_teardown_failure_does_not_lose_results(self) -> None:
        # a destroy that raises must NOT discard the probe data — it becomes a
        # WARNING note appended to the (preserved) HostRun. The first-run lesson.
        class _DestroyFails(FakeProvider):
            def destroy(self, host: Host) -> None:
                super().destroy(host)  # still record it locally
                raise providers.ProviderError("request timeout")

        prov = _DestroyFails()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu",)), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].results)  # probe data preserved
        self.assertIn(runner.TEARDOWN_WARNING_PREFIX, runs[0].detail)  # failure noted, not raised

    def test_teardown_fires_on_base_exception_then_it_propagates(self) -> None:
        # T2: a workload raising a BaseException (KeyboardInterrupt) AFTER the host
        # is created → the host is STILL destroyed by the finally, and the
        # BaseException keeps propagating (the run aborts).
        prov = FakeProvider()

        class _Interrupting:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                raise KeyboardInterrupt("operator hit Ctrl-C")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        with tempfile.TemporaryDirectory() as d, self.assertRaises(KeyboardInterrupt):
            runner.execute(self._matrix(("ubuntu",)), prov, lambda _o, _k: _Interrupting(), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(prov.created), 1)
        self.assertEqual(len(prov.destroyed), 1)  # torn down despite the BaseException

    def test_create_failure_records_error_and_skips_teardown(self) -> None:
        # F3: a failure BEFORE create_with_cloudinit succeeds → no host exists, so
        # the finally's host-is-None arm attempts NO teardown; the seeded error is
        # recorded as the HostRun (zero results) and the run is NOT aborted.
        class _CreateFails(FakeProvider):
            def create_with_cloudinit(self, spec: HostSpec, cloud_init: str) -> Host:
                raise providers.ProviderError("create rejected")

        prov = _CreateFails()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._matrix(("ubuntu",)), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0].detail.startswith("ERROR:"))  # create failure recorded
        self.assertEqual(runs[0].results, ())  # no probes ran
        self.assertEqual(len(prov.destroyed), 0)  # host never created → no teardown

    def test_parallel_abort_persists_already_completed_host(self) -> None:
        # F4: in the parallel path, a host (fedora) finishes cleanly but its
        # as_completed turn hasn't arrived when another host (debian) raises
        # KeyboardInterrupt. The abort-time drain must STILL fire on_host_complete
        # for the already-done fedora (so its result persists like the serial path)
        # before the KeyboardInterrupt propagates.
        #
        # Determinism — the construction pins the completion order to A, debian,
        # fedora and parks the main consumer until ALL three are done:
        #   * ubuntu (A) completes first; its on_host_complete PARKS the consumer
        #     until ``fedora_done`` so the loop accumulates the other two while held.
        #   * debian raises KeyboardInterrupt and signals ``debian_done`` — it
        #     COMPLETES (and is queued by as_completed) BEFORE fedora.
        #   * fedora waits for ``debian_done`` then completes — so it is done-but-
        #     not-yet-yielded when, on unpark, the loop reaches debian's turn and
        #     the KeyboardInterrupt unwinds. Only the drain can persist fedora.
        prov = FakeProvider()
        ubuntu_done = threading.Event()
        debian_done = threading.Event()
        fedora_done = threading.Event()
        seen: list[str] = []
        seen_lock = threading.Lock()

        class _OrchestratedAbort:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                if "debian" in host.name:
                    ubuntu_done.wait(timeout=5.0)  # raise strictly after ubuntu is done
                    debian_done.set()
                    raise KeyboardInterrupt("operator hit Ctrl-C")
                if "fedora" in host.name:
                    debian_done.wait(timeout=5.0)  # complete strictly after debian
                    fedora_done.set()
                    return ProbeResult(probe.id, probe.tag, 0, "ok", "")
                # ubuntu: completes first and fastest (no waits), parking the sink.
                return ProbeResult(probe.id, probe.tag, 0, "ok", "")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        def sink(hr: model.HostRun) -> None:
            # ubuntu is the first future done, so it is yielded first. Mark it done
            # (releasing debian's raise) and PARK the consumer until BOTH debian and
            # fedora are done — so debian's raise and fedora's clean completion both
            # land while the consumer is held, making fedora the done-but-unyielded
            # host that ONLY the abort-time drain can persist.
            if hr.host_spec.distro_key == "ubuntu":
                ubuntu_done.set()
                fedora_done.wait(timeout=5.0)
            with seen_lock:
                seen.append(hr.host_spec.distro_key)

        m = runner.Matrix(_demo_workload(), ("ubuntu", "debian", "fedora"), "cpx22", "run-abort")
        with tempfile.TemporaryDirectory() as d, self.assertRaises(KeyboardInterrupt):
            runner.execute(
                m, prov, lambda _o, _k: _OrchestratedAbort(), _fake_keypair(Path(d)), "probe",
                max_parallel=3, on_host_complete=sink,
            )
        # Exactly ubuntu (normal loop, after unpark) then fedora (abort-time drain);
        # debian raised, so it is NOT persisted. Without the drain `seen` would be just
        # ["ubuntu"] — so this equality distinguishes drain-path persistence, not merely
        # that fedora appears somehow.
        self.assertEqual(seen, ["ubuntu", "fedora"])

    def test_on_host_complete_called_per_host_in_completion_order(self) -> None:
        # the incremental sink fires once per host as it completes (serial path)
        prov = FakeProvider()
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(
                self._matrix(("ubuntu", "debian")), prov, self._factory(FakeSshRunner()),
                _fake_keypair(Path(d)), "probe",
                on_host_complete=lambda hr: seen.append(hr.host_spec.distro_key),
            )
        self.assertEqual(seen, ["ubuntu", "debian"])  # one call per host
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian"])

    def test_parallel_on_host_complete_called_once_per_host_main_thread(self) -> None:
        # parallel path: sink fires once per host from the MAIN thread; aggregate
        # stays matrix-ordered regardless of completion order.
        prov = FakeProvider()
        seen: list[str] = []
        main_thread = threading.current_thread()
        threads_seen: list[bool] = []

        def sink(hr: model.HostRun) -> None:
            threads_seen.append(threading.current_thread() is main_thread)
            seen.append(hr.host_spec.distro_key)

        m = runner.Matrix(_demo_workload(), ("ubuntu", "debian", "fedora"), "cpx22", "run-sink")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(
                m, prov, lambda _o, _k: FakeSshRunner(), _fake_keypair(Path(d)), "probe",
                max_parallel=3, on_host_complete=sink,
            )
        self.assertEqual(sorted(seen), ["debian", "fedora", "ubuntu"])  # each host once
        self.assertTrue(all(threads_seen))  # all sink calls on the main thread
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian", "fedora"])

    def test_parallel_one_failure_does_not_discard_others(self) -> None:
        prov = FakeProvider()

        class _SelectiveSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                if "debian" in host.name:
                    raise ssh.SshError("debian unreachable")
                return ProbeResult(probe.id, probe.tag, 0, "ok", "")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        m = runner.Matrix(_demo_workload(), ("ubuntu", "debian"), "cpx22", "run-par2")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, lambda _o, _k: _SelectiveSsh(), _fake_keypair(Path(d)), "probe", max_parallel=2)
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(prov.destroyed), 2)
        by_distro = {r.host_spec.distro_key: r for r in runs}
        self.assertTrue(by_distro["ubuntu"].results)  # preserved
        self.assertTrue(by_distro["debian"].detail.startswith("ERROR:"))


# --------------------------------------------------------------------------- #
# workload seam — the injected Workload Protocol (D9.1)
# --------------------------------------------------------------------------- #
class _RecordingWorkload:
    """A Workload that records each host it runs on — for seam/ordering tests."""

    plan_summary = "recording"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, spec: HostSpec, host: Host, ssh: ssh.SshRunner, /) -> model.HostRun:
        self.calls.append(host.name)
        return model.HostRun(host_spec=spec, detail="recorded", results=())


class TestWorkloadSeam(unittest.TestCase):
    def test_probe_workload_satisfies_protocol(self) -> None:
        self.assertIsInstance(_demo_workload(), workload.Workload)

    def test_a_non_probe_workload_satisfies_protocol(self) -> None:
        # the seam admits other workloads — a fake conforms structurally
        self.assertIsInstance(_RecordingWorkload(), workload.Workload)

    def test_injected_workload_runs_once_per_host_in_matrix_order(self) -> None:
        prov = FakeProvider()
        rec = _RecordingWorkload()
        m = runner.Matrix(rec, ("ubuntu", "debian"), "cpx22", "run-inj")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, lambda _o, _k: FakeSshRunner(), _fake_keypair(Path(d)), "probe")
        self.assertEqual([r.host_spec.distro_key for r in runs], ["ubuntu", "debian"])  # matrix order
        self.assertEqual(len(rec.calls), 2)  # exactly one run() per host
        self.assertEqual([n.rsplit("-", 1)[-1] for n in rec.calls], ["ubuntu", "debian"])

    def test_runner_gates_readiness_before_invoking_workload(self) -> None:
        # the runner owns readiness: wait_until_ready fires BEFORE workload.run
        prov = FakeProvider()
        order: list[str] = []

        class _ReadyRecordingSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                return ProbeResult(probe.id, probe.tag, 0, "", "")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                order.append("ready")

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        class _OrderWorkload:
            plan_summary = "order"

            def run(self, spec: HostSpec, host: Host, ssh: ssh.SshRunner, /) -> model.HostRun:
                order.append("run")
                return model.HostRun(host_spec=spec, detail="", results=())

        m = runner.Matrix(_OrderWorkload(), ("ubuntu",), "cpx22", "run-ready")
        with tempfile.TemporaryDirectory() as d:
            runner.execute(m, prov, lambda _o, _k: _ReadyRecordingSsh(), _fake_keypair(Path(d)), "probe")
        self.assertEqual(order, ["ready", "run"])

    def test_plan_renders_the_workload_summary(self) -> None:
        wl = _demo_workload()
        items = runner.plan(runner.Matrix(wl, ("ubuntu",), "cpx22", "run-ps"))
        self.assertEqual(items[0].workload_summary, "probes=3")
        self.assertEqual(items[0].workload_summary, wl.plan_summary)


class _ScriptedTimeoutSsh:
    """An SshRunner whose ``run_probe`` returns timed-out results for named probes.

    ``timeout_ids`` is the set of probe ids that come back ``timed_out`` (exit 124);
    everything else (including the ``_detail`` snapshot) returns a normal exit-0
    result. For exercising the consecutive-timeout breaker without a real socket.
    """

    def __init__(self, timeout_ids: set[str]) -> None:
        self._timeout_ids = timeout_ids
        self.ran: list[str] = []

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        self.ran.append(probe.id)
        if probe.id in self._timeout_ids:
            return ProbeResult(probe.id, probe.tag, 124, "", "timed out after 1.0s", timed_out=True)
        return ProbeResult(probe.id, probe.tag, 0, f"out-{probe.id}", "")

    def upload(self, host: Host, local: Path, remote: str) -> None:
        return None

    def wait_until_ready(self, host: Host) -> None:
        return None

    def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
        return 0

    def upload_dir(self, host: Host, local: Path, remote: str) -> None:
        return None


class TestProbeWorkloadBreaker(unittest.TestCase):
    """T1b: timeout does not abort the battery; K consecutive timeouts stop it."""

    def _battery(self) -> model.Battery:
        # four probes; they run in authoring order Q1..Q4.
        manifest = _battery_toml("b", (
            {"id": "Q1", "title": "t", "run": "c", "tag": "read-only"},
            {"id": "Q2", "title": "t", "run": "c", "tag": "read-only"},
            {"id": "Q3", "title": "t", "run": "c", "tag": "read-only"},
            {"id": "Q4", "title": "t", "run": "c", "tag": "read-only"},
        ))
        return _resolve_toml(manifest)

    def _spec(self) -> model.HostSpec:
        return model.HostSpec(name="n", image="i", server_type="cpx22", distro_key="ubuntu")

    def _host(self) -> Host:
        return Host(id="1", name="n", ipv4="9.9.9.9")

    def test_isolated_timeout_continues_battery(self) -> None:
        # one timeout in the middle, the next probe NOT a timeout → battery runs on,
        # every later probe's result is present.
        wl = workload.ProbeWorkload(self._battery())
        ssh_fake = _ScriptedTimeoutSsh({"Q2"})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual([r.probe_id for r in run.results], ["Q1", "Q2", "Q3", "Q4"])
        self.assertTrue(run.results[1].timed_out)  # the isolated timeout is recorded
        self.assertFalse(run.results[3].timed_out)  # later probes still ran
        self.assertNotIn("battery stopped", run.detail)

    def test_k_consecutive_timeouts_stop_battery(self) -> None:
        # Q2 and Q3 both time out (K=2) → battery stops; Q4 is NOT run; prior
        # results (Q1, Q2, Q3) are preserved and the detail carries the note.
        wl = workload.ProbeWorkload(self._battery())
        ssh_fake = _ScriptedTimeoutSsh({"Q2", "Q3"})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual([r.probe_id for r in run.results], ["Q1", "Q2", "Q3"])  # Q4 not run
        self.assertNotIn("Q4", ssh_fake.ran)  # the breaker really stopped the loop
        self.assertIn("battery stopped", run.detail)
        self.assertIn("2 consecutive probe timeouts", run.detail)
        self.assertIn("'Q4'", run.detail)  # names the probe(s) not run

    def test_non_consecutive_timeouts_do_not_trip(self) -> None:
        # Q1 and Q3 time out but Q2 resets the counter → never K-in-a-row, full run.
        wl = workload.ProbeWorkload(self._battery())
        ssh_fake = _ScriptedTimeoutSsh({"Q1", "Q3"})
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual(len(run.results), 4)  # all four ran
        self.assertNotIn("battery stopped", run.detail)

    def test_breaker_constant_default_is_two(self) -> None:
        self.assertEqual(workload.MAX_CONSECUTIVE_TIMEOUTS, 2)

    def test_mixed_tag_battery_runs_in_authoring_order(self) -> None:
        # a host-root probe authored before a read-only one executes (and records)
        # first — authoring order, not tag-rank; the read-only verifier runs after.
        manifest = _battery_toml("b", (
            {"id": "SETUP", "title": "t", "run": "c", "tag": "mutating:host-root"},
            {"id": "VERIFY", "title": "t", "run": "c", "tag": "read-only"},
        ))
        wl = workload.ProbeWorkload(_resolve_toml(manifest))
        ssh_fake = _ScriptedTimeoutSsh(set())
        run = wl.run(self._spec(), self._host(), ssh_fake)
        self.assertEqual(ssh_fake.ran, ["_detail", "SETUP", "VERIFY"])
        self.assertEqual([r.probe_id for r in run.results], ["SETUP", "VERIFY"])

    def test_script_probe_resolves_and_executes_via_workload(self) -> None:
        # a `script` probe's resolved command (the file's contents) reaches the
        # transport unchanged — exercising the script-ref path end-to-end.
        manifest = _battery_toml("b", (
            {"id": "SCRIPTED", "title": "t", "script": "prep.sh", "tag": "read-only"},
        ))
        script_body = "set -euo pipefail\nuname -r\nexit 0\n"
        battery = _resolve_toml(manifest, {"prep.sh": script_body})

        seen: list[str] = []

        class _CommandRecordingSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                seen.append(probe.command)
                return ProbeResult(probe.id, probe.tag, 0, "", "")

            def upload(self, host: Host, local: Path, remote: str) -> None:
                return None

            def wait_until_ready(self, host: Host) -> None:
                return None

            def run_streaming(self, host: Host, command: str, on_output: Callable[[str], None], /, *, timeout: float) -> int:
                return 0

            def upload_dir(self, host: Host, local: Path, remote: str) -> None:
                return None

        wl = workload.ProbeWorkload(battery)
        wl.run(self._spec(), self._host(), _CommandRecordingSsh())
        self.assertIn(script_body, seen)  # the file's contents reached the transport


# --------------------------------------------------------------------------- #
# cli — run (confirm gate) / reap / status, with provider + ssh stubbed
# --------------------------------------------------------------------------- #
class TestCliRun(unittest.TestCase):
    def _write_battery(self, d: str) -> str:
        return _write_battery_bundle(d, _DEMO_BATTERY)

    def test_run_aborts_without_confirm(self) -> None:
        # _cmd_run reads via injected reader; "n" aborts before provisioning
        with tempfile.TemporaryDirectory() as d:
            ns = cli.build_parser().parse_args([
                "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                "--results-dir", str(Path(d) / "r"), "--timestamp", "T", "--run-token", "cli-run",
            ])
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._cmd_run(ns, reader=lambda _p: "n")
            self.assertEqual(rc, 0)
            self.assertIn("aborted", buf.getvalue())
            self.assertFalse((Path(d) / "r").exists())  # no results written

    def test_run_cost_guard_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc = cli.main([
                "run", "--battery", self._write_battery(d), "--distros", "ubuntu,debian,fedora,arch",
                "--max-hosts", "2", "--results-dir", str(Path(d) / "r"), "--timestamp", "T", "--run-token", "cli-run",
            ])
            self.assertEqual(rc, 2)

    def test_run_arch_without_ssh_key_path_refused(self) -> None:
        # a rescue-write distro needs BOTH --ssh-key and --ssh-key-path; omitting
        # the path is refused AFTER the confirm (so no host is provisioned).
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli.main([
                        "run", "--battery", self._write_battery(d), "--distros", "arch",
                        "--ssh-key", "mykey",  # name given, but no --ssh-key-path
                        "--results-dir", str(Path(d) / "r"), "--timestamp", "T",
                        "--run-token", "cli-run", "--yes",
                    ])
            self.assertEqual(rc, 2)

    def test_run_success_writes_results(self) -> None:
        # stub the provider + keypair + ssh so the run path executes end-to-end
        # with no network. --yes skips the confirm prompt.
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            with mock.patch.object(cli, "HetznerProvider", FakeProvider), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 0)
            self.assertTrue((rdir / "vmlease-cli-run-20260601T000000Z.json").exists())

    def test_run_probe_timeout_reaches_ssh_runner(self) -> None:
        # --probe-timeout (run-only) must be threaded into OpenSshRunner via the
        # factory as probe_timeout_default; capture the kwarg the factory passes.
        from unittest import mock

        captured: dict[str, object] = {}

        def _capture(*_a: object, **k: object) -> FakeSshRunner:
            captured.update(k)
            return FakeSshRunner()

        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            with mock.patch.object(cli, "HetznerProvider", FakeProvider), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", _capture):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes", "--probe-timeout", "42.5",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(captured.get("probe_timeout_default"), 42.5)

    def test_run_writes_results_incrementally_per_host(self) -> None:
        # the per-host sink rewrites the results file as each host lands; after a
        # clean run the deterministic path holds every host and rc is 0.
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            with mock.patch.object(cli, "HetznerProvider", FakeProvider), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu,debian",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 0)
            written = json.loads((rdir / "vmlease-cli-run-20260601T000000Z.json").read_text())
            self.assertEqual(len(written["hosts"]), 2)

    def test_run_end_to_end_smoke_ties_the_lifecycle_together(self) -> None:
        # F5: one realistic, fully-mocked `vmlease run` over TWO distros that ties
        # the whole lifecycle together — the unit tests cover each behavior in
        # isolation; this asserts they compose. The live billable smoke (real
        # machinectl/VM) stays deferred; nothing here opens a socket.
        #
        # Asserts in ONE run: clean rc; results file holds BOTH hosts (incremental
        # persistence end-to-end); a timed-out probe is recorded (exit 124) and did
        # NOT abort the battery (every probe present); destroy fired for BOTH hosts
        # (per-host finally teardown); and --probe-timeout reached OpenSshRunner.
        from unittest import mock

        captured: dict[str, object] = {}

        class _TimeoutOneProbeSsh(FakeSshRunner):
            # normal results for every probe EXCEPT P6, which times out (exit 124,
            # timed_out=True) — a SINGLE timeout, below the K=2 breaker, so the
            # battery runs to completion and the timeout is just recorded.
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                self.ran.append(probe.id)
                if probe.id == "P6":
                    return ProbeResult(probe.id, probe.tag, 124, "", "timed out after 42.5s", timed_out=True)
                return ProbeResult(probe.id, probe.tag, 0, f"out-{probe.id}", "")

        def _capture(*_a: object, **k: object) -> _TimeoutOneProbeSsh:
            captured.update(k)
            return _TimeoutOneProbeSsh()

        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", _capture):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu,fedora",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes", "--probe-timeout", "42.5",
                ])
            self.assertEqual(rc, 0)  # clean run
            # --probe-timeout reached the ssh runner factory.
            self.assertEqual(captured.get("probe_timeout_default"), 42.5)
            # destroy fired for BOTH hosts (per-host finally teardown).
            self.assertEqual(len(prov.destroyed), 2)
            # results file exists and holds BOTH hosts (incremental persistence).
            path = rdir / "vmlease-cli-run-20260601T000000Z.json"
            self.assertTrue(path.exists())
            written = json.loads(path.read_text())
            self.assertEqual(len(written["hosts"]), 2)
            self.assertEqual(
                sorted(h["distro"] for h in written["hosts"]), ["fedora", "ubuntu"]
            )
            # the timed-out P6 is recorded (exit 124) and the battery did NOT abort:
            # every battery probe is present on each host.
            for host in written["hosts"]:
                ids = [p["id"] for p in host["probes"]]
                self.assertEqual(ids, ["P1", "P6", "P12"])  # full battery, none dropped
                p6 = next(p for p in host["probes"] if p["id"] == "P6")
                self.assertEqual(p6["exit_code"], 124)  # the timeout is recorded
                self.assertFalse(p6["ok"])

    def test_run_teardown_failure_returns_nonzero_and_reaps(self) -> None:
        # a destroy that raises (host stays live) → HostRun.detail carries the
        # teardown warning → _cmd_run reaps the run label and returns non-zero.
        from unittest import mock

        class _DestroyFailsOnce(FakeProvider):
            def __init__(self) -> None:
                super().__init__()
                self.destroy_calls = 0

            def destroy(self, host: Host) -> None:
                # First teardown (in-run) fails, leaving the host live; the reap
                # backstop's destroy then succeeds (a transient provider blip).
                self.destroy_calls += 1
                if self.destroy_calls == 1:
                    raise providers.ProviderError("request timeout")
                super().destroy(host)

        prov = _DestroyFailsOnce()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            buf = io.StringIO()
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()), \
                 redirect_stderr(buf):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 1)
            self.assertIn("teardown failed", buf.getvalue())
            # the backstop reap names the host(s) it cleaned up (like `vmlease reap`)
            self.assertIn("reaped vmlease-cli-run-ubuntu", buf.getvalue())
            # reap was attempted: list_labeled then destroy on the still-live host.
            self.assertTrue(prov.list_labeled(safety.make_run_id("cli-run")) == [])

    def test_run_abort_reaps_and_reraises(self) -> None:
        # an operator interrupt mid-run: the host that finished is on disk, the
        # run label is reaped, and the KeyboardInterrupt keeps propagating.
        from unittest import mock

        class _Interrupting(FakeSshRunner):
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                raise KeyboardInterrupt("operator hit Ctrl-C")

        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            buf = io.StringIO()
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: _Interrupting()), \
                 redirect_stderr(buf), \
                 self.assertRaises(KeyboardInterrupt):
                cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertIn("aborted", buf.getvalue())
            # the run label was reaped even though the interrupt propagated.
            self.assertEqual(prov.list_labeled(safety.make_run_id("cli-run")), [])

    def test_run_teardown_failure_and_backstop_reap_also_fails(self) -> None:
        # teardown fails (host stays live) AND the backstop reap ALSO raises
        # ProviderError: _cmd_run must still return non-zero (no traceback) and
        # print the actionable `vmlease reap --run-token` manual-cleanup hint.
        from unittest import mock

        class _DestroyAlwaysFails(FakeProvider):
            def destroy(self, host: Host) -> None:
                # In-run teardown fails (host stays live), and the backstop
                # reap's destroy fails too — the host can't be reaped.
                raise providers.ProviderError("request timeout")

        prov = _DestroyAlwaysFails()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            buf = io.StringIO()
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: FakeSshRunner()), \
                 redirect_stderr(buf):
                rc = cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertEqual(rc, 1)  # non-zero, NOT a traceback
            self.assertIn("backstop reap ALSO failed", buf.getvalue())
            self.assertIn("vmlease reap --run-token cli-run", buf.getvalue())

    def test_run_abort_and_backstop_reap_also_fails_reraises_interrupt(self) -> None:
        # an operator interrupt mid-run AND the backstop reap ALSO raises
        # ProviderError: the ORIGINAL KeyboardInterrupt (not ProviderError) must
        # propagate, and the actionable manual-reap hint must be printed.
        from unittest import mock

        class _Interrupting(FakeSshRunner):
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                raise KeyboardInterrupt("operator hit Ctrl-C")

        class _ReapFails(FakeProvider):
            def list_labeled(self, run_id: str) -> list[Host]:
                raise providers.ProviderError("request timeout")

        prov = _ReapFails()
        with tempfile.TemporaryDirectory() as d:
            rdir = Path(d) / "r"
            buf = io.StringIO()
            with mock.patch.object(cli, "HetznerProvider", lambda: prov), \
                 mock.patch.object(cli, "generate_keypair", lambda rid: _fake_keypair(Path(d))), \
                 mock.patch.object(cli, "OpenSshRunner", lambda *a, **k: _Interrupting()), \
                 redirect_stderr(buf), \
                 self.assertRaises(KeyboardInterrupt):
                cli.main([
                    "run", "--battery", self._write_battery(d), "--distros", "ubuntu",
                    "--results-dir", str(rdir), "--timestamp", "20260601T000000Z",
                    "--run-token", "cli-run", "--yes",
                ])
            self.assertIn("backstop reap ALSO failed", buf.getvalue())
            self.assertIn("vmlease reap --run-token cli-run", buf.getvalue())

    def test_reap_lists_destroyed(self) -> None:
        from unittest import mock

        prov = FakeProvider()
        prov.create_with_cloudinit(HostSpec(name="n", image="i", server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "cli-run"}), "ci")
        buf = io.StringIO()
        with mock.patch.object(cli, "HetznerProvider", lambda: prov), redirect_stdout(buf):
            rc = cli.main(["reap", "--run-token", "cli-run"])
        self.assertEqual(rc, 0)
        self.assertIn("reaped 1 host", buf.getvalue())

    def test_status_lists_live(self) -> None:
        from unittest import mock

        prov = FakeProvider()
        prov.create_with_cloudinit(HostSpec(name="n", image="i", server_type="cpx22", distro_key="ubuntu", labels={"vmlease": "cli-run"}), "ci")
        buf = io.StringIO()
        with mock.patch.object(cli, "HetznerProvider", lambda: prov), redirect_stdout(buf):
            rc = cli.main(["status", "--run-token", "cli-run"])
        self.assertEqual(rc, 0)
        self.assertIn("1 live host", buf.getvalue())

    def test_reap_provider_error_returns_1(self) -> None:
        from unittest import mock

        class _Boom:
            def list_labeled(self, run_id: str) -> list[Host]:
                raise providers.ProviderError("boom")

        with mock.patch.object(cli, "HetznerProvider", _Boom):
            rc = cli.main(["reap", "--run-token", "cli-run"])
        self.assertEqual(rc, 1)


# --------------------------------------------------------------------------- #
# archimage — resolve-latest + SHA256 + pinned-key signature verify
# --------------------------------------------------------------------------- #
class TestArchImage(unittest.TestCase):
    _INDEX = """
    <a href="v20260101.100000/">v20260101.100000/</a>
    <a href="v20260515.530093/">v20260515.530093/</a>
    <a href="v20260301.420000/">v20260301.420000/</a>
    """

    def test_parse_versions_dedupes_and_sorts(self) -> None:
        vs = archimage.parse_versions(self._INDEX + " v20260515.530093")
        self.assertEqual([v.tag for v in vs], ["v20260101.100000", "v20260301.420000", "v20260515.530093"])

    def test_latest_version(self) -> None:
        self.assertEqual(archimage.latest_version(self._INDEX).tag, "v20260515.530093")

    def test_latest_version_serial_breaks_tie(self) -> None:
        idx = "v20260515.1 v20260515.30 v20260515.2"
        self.assertEqual(archimage.latest_version(idx).serial, 30)

    def test_no_versions_raises(self) -> None:
        with self.assertRaises(archimage.ArchImageError):
            archimage.latest_version("nothing here")

    def test_urls(self) -> None:
        v = archimage.latest_version(self._INDEX)
        self.assertTrue(archimage.qcow2_url(v).endswith("v20260515.530093/Arch-Linux-x86_64-cloudimg.qcow2"))
        self.assertTrue(archimage.sha256_url(v).endswith(".qcow2.SHA256"))
        self.assertTrue(archimage.sig_url(v).endswith(".qcow2.sig"))

    def test_parse_expected_sha256(self) -> None:
        digest = "a" * 64
        self.assertEqual(archimage.parse_expected_sha256(f"{digest}  Arch-Linux-x86_64-cloudimg.qcow2"), digest)

    def test_parse_expected_sha256_missing(self) -> None:
        with self.assertRaises(archimage.ArchImageError):
            archimage.parse_expected_sha256("no digest here")

    def test_verify_sha256_ok_and_mismatch(self) -> None:
        data = b"qcow2-bytes"
        good = hashlib.sha256(data).hexdigest()
        archimage.verify_sha256(data, good)  # no raise
        with self.assertRaises(archimage.ArchImageError):
            archimage.verify_sha256(data, "b" * 64)

    def test_build_gpg_verify_argv(self) -> None:
        argv = archimage.build_gpg_verify_argv("/t/x.sig", "/t/x.qcow2", "/t/keyring.gpg")
        self.assertIn("--keyring", argv)
        self.assertIn("/t/keyring.gpg", argv)
        self.assertEqual(argv[-3:], ["--verify", "/t/x.sig", "/t/x.qcow2"])

    def test_verify_signature_accepts_pinned_key(self) -> None:
        fp = "ABCD1234" * 5  # 40-hex fingerprint
        status = f"[GNUPG:] VALIDSIG {fp} 2026-05-15 ...\n"
        archimage.verify_signature("s", "q", "k", fp, gpg_runner=_fake_subprocess(0, status))  # no raise

    def test_verify_signature_pinned_primary_matches_subkey_sig(self) -> None:
        # Real gpg shape: VALIDSIG <signing-subkey-fp> <date> <ts> ... <primary-fp>.
        # arch-boxes signs with a [S] subkey under a certify-only [C] primary, so
        # pinning the PRIMARY fingerprint must validate a subkey-made signature.
        primary = archimage.DEFAULT_ARCH_KEY_FINGERPRINT
        subkey = "00FF" * 10  # the subkey that actually signed (different fp)
        status = f"[GNUPG:] VALIDSIG {subkey} 2026-06-01 1700000000 0 4 0 22 10 00 {primary}\n"
        archimage.verify_signature("s", "q", "k", primary, gpg_runner=_fake_subprocess(0, status))  # no raise

    def test_verify_signature_rejects_wrong_key(self) -> None:
        good = "ABCD1234" * 5
        other = "9999FFFF" * 5
        status = f"[GNUPG:] VALIDSIG {other} ...\n"
        with self.assertRaises(archimage.ArchImageError):
            archimage.verify_signature("s", "q", "k", good, gpg_runner=_fake_subprocess(0, status))

    def test_verify_signature_gpg_nonzero(self) -> None:
        with self.assertRaises(archimage.ArchImageError):
            archimage.verify_signature("s", "q", "k", "F" * 40, gpg_runner=_fake_subprocess(1, "", "bad sig"))

    def test_verify_signature_skips_noise_lines_and_bare_validsig(self) -> None:
        fp = "ABCD1234" * 5
        # a non-VALIDSIG line (skipped), a bare VALIDSIG with no fields (skipped),
        # then the real line — exercises both defensive continues.
        status = f"[GNUPG:] NEWSIG\n[GNUPG:] VALIDSIG\n[GNUPG:] VALIDSIG {fp} 2026 0 0\n"
        archimage.verify_signature("s", "q", "k", fp, gpg_runner=_fake_subprocess(0, status))  # no raise

    def test_resolve_and_verify_end_to_end(self) -> None:
        fp = "ABCD1234" * 5
        qcow = b"the-disk-image"
        sha = hashlib.sha256(qcow).hexdigest()

        def text_fetcher(url: str) -> str:
            if url == archimage.MIRROR_BASE:
                return self._INDEX
            if url.endswith(".SHA256"):
                return f"{sha}  Arch-Linux-x86_64-cloudimg.qcow2"
            raise AssertionError(url)

        def fetcher(url: str) -> bytes:
            return qcow if url.endswith(".qcow2") else b"SIGBYTES"

        written: list[bytes] = []

        def write_temp(b: bytes) -> str:
            written.append(b)
            return f"/tmp/probe-{len(written)}"

        result = archimage.resolve_and_verify(
            text_fetcher=text_fetcher, fetcher=fetcher,
            gpg_runner=_fake_subprocess(0, f"[GNUPG:] VALIDSIG {fp} ...\n"),
            keyring_path="/k", expected_fingerprint=fp, write_temp=write_temp,
        )
        self.assertEqual(result.version.tag, "v20260515.530093")
        self.assertEqual(result.expected_sha256, sha)

    def test_resolve_and_verify_sha_mismatch_raises(self) -> None:
        def text_fetcher(url: str) -> str:
            return self._INDEX if url == archimage.MIRROR_BASE else f"{'0' * 64}  x.qcow2"

        with self.assertRaises(archimage.ArchImageError):
            archimage.resolve_and_verify(
                text_fetcher=text_fetcher, fetcher=lambda _u: b"data",
                gpg_runner=_fake_subprocess(0, ""), keyring_path="/k",
                expected_fingerprint="F" * 40, write_temp=lambda _b: "/tmp/x",
            )


# --------------------------------------------------------------------------- #
# runner — the rescue-write seam
# --------------------------------------------------------------------------- #
class TestRunnerRescueWrite(unittest.TestCase):
    def _arch_matrix(self) -> runner.Matrix:
        return runner.Matrix(_demo_workload(), ("arch",), "cpx22", "run-rw")

    def _factory(self, ssh_runner: ssh.SshRunner) -> Callable[[str, keypair.Keypair], ssh.SshRunner]:
        return lambda _op, _kp: ssh_runner

    def test_rescue_writer_invoked_for_arch(self) -> None:
        prov = FakeProvider()
        calls: list[tuple[str, str]] = []

        def writer(host: Host, profile: distro.DistroProfile) -> None:
            calls.append((host.name, profile.key))

        with tempfile.TemporaryDirectory() as d:
            runner.execute(self._arch_matrix(), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe", rescue_writer=writer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "arch")
        self.assertEqual(len(prov.destroyed), 1)  # still torn down

    def test_native_distro_does_not_invoke_writer(self) -> None:
        prov = FakeProvider()
        calls: list[str] = []
        m = runner.Matrix(_demo_workload(), ("ubuntu",), "cpx22", "run-nat")
        with tempfile.TemporaryDirectory() as d:
            runner.execute(m, prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe",
                           rescue_writer=lambda h, p: calls.append(h.name))
        self.assertEqual(calls, [])

    def test_missing_writer_for_arch_recorded_and_torn_down(self) -> None:
        # per-host isolation: a missing rescue-writer is captured as an error
        # HostRun (not a propagating raise) and the base host is still torn down.
        prov = FakeProvider()
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(self._arch_matrix(), prov, self._factory(FakeSshRunner()), _fake_keypair(Path(d)), "probe")
        self.assertEqual(len(prov.destroyed), 1)  # created host still torn down
        self.assertTrue(runs[0].detail.startswith("ERROR:"))


# --------------------------------------------------------------------------- #
# archbuild — ephemeral rescue-write transform (pure builders/parsers)
# --------------------------------------------------------------------------- #
class TestArchBuild(unittest.TestCase):
    def test_qcow2_name_is_cloudimg(self) -> None:
        # the cloudimg variant (has cloud-init), NOT basic
        self.assertEqual(archimage.QCOW2_NAME, "Arch-Linux-x86_64-cloudimg.qcow2")

    def test_enable_rescue_argv_no_output_flag(self) -> None:
        argv = archbuild.build_enable_rescue_argv("12345678", "mykey")
        self.assertIn("enable-rescue", argv)
        self.assertIn("linux64", argv)
        self.assertIn("mykey", argv)
        self.assertNotIn("--output", argv)  # enable-rescue has no --output

    def test_disable_and_reset_argv(self) -> None:
        self.assertEqual(archbuild.build_disable_rescue_argv("5"), ["hcloud", "server", "disable-rescue", "5"])
        self.assertEqual(archbuild.build_reset_argv("5"), ["hcloud", "server", "reset", "5"])

    def test_parse_rescue_password(self) -> None:
        out = "Rescue enabled for server 12345678 with root password: r3scueFAKEpw00\n"
        self.assertEqual(archbuild.parse_rescue_password(out), "r3scueFAKEpw00")

    def test_parse_rescue_password_missing(self) -> None:
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.parse_rescue_password("Rescue enabled, no password line\n")

    def test_render_rescue_script_remote_url_renders_curl(self) -> None:
        from vmlease.rescue_image import RemoteUrl
        s = archbuild.render_rescue_script(RemoteUrl("https://m/Arch.qcow2"), "a" * 64)
        self.assertIn("https://m/Arch.qcow2", s)
        # a metachar-free URL needs no quoting; shlex.quote leaves it bare
        self.assertIn(f"curl -fsSL https://m/Arch.qcow2 -o {archbuild.RESCUE_IMAGE_PATH}", s)
        self.assertIn("a" * 64, s)
        # the script probes the disk (never hardcodes sda) and re-verifies the sha
        self.assertIn("lsblk", s)
        self.assertIn("sha256sum -c", s)
        self.assertIn("qemu-img convert -O raw", s)
        # shell vars pass through the @@ templating untouched
        self.assertIn("$disk", s)

    def test_render_rescue_script_local_file_renders_presence_check(self) -> None:
        from vmlease.rescue_image import LocalFile
        s = archbuild.render_rescue_script(LocalFile(Path("/local/golden.qcow2")), "b" * 64)
        # a LocalFile is already pushed → the script asserts presence, does NOT curl
        self.assertIn(f"test -f {archbuild.RESCUE_IMAGE_PATH}", s)
        self.assertNotIn("curl -fsSL", s)  # no actual fetch command (the comment mentions curl)
        # both sources still re-verify the sha on the rescue side
        self.assertIn("sha256sum -c", s)
        self.assertIn("b" * 64, s)

    def test_render_fetch_cmd_remote_url_is_shell_quoted(self) -> None:
        from vmlease.rescue_image import RemoteUrl
        # a URL with a single quote + shell metachars must not break out of the
        # root-side script — shlex.quote escapes it safely.
        evil = "https://m/x.qcow2'; rm -rf /; echo '"
        cmd = archbuild.render_fetch_cmd(RemoteUrl(evil))
        self.assertEqual(
            cmd,
            f"curl -fsSL {shlex.quote(evil)} -o {archbuild.RESCUE_IMAGE_PATH} "
            "|| { echo 'RESCUE_FAIL: download failed' >&2; exit 13; }",
        )
        # the quoted argument round-trips back to the exact URL under shell parsing,
        # so the injection payload is inert (a single curl arg, not extra commands).
        _, _, rest = cmd.partition("curl -fsSL ")
        quoted_arg = rest[: rest.index(" -o ")]
        self.assertEqual(shlex.split(quoted_arg), [evil])

    # The Arch profile's spec resolves the latest mirror image; these fakes drive
    # its injected IO seams (ResolveDeps) so it produces the real RemoteUrl + sha
    # — byte-faithful to the pre-extraction resolve-latest + sha + pinned-GPG.
    _ARCH_QCOW = b"the-disk-image"

    def _arch_resolve_deps(self) -> rescue_image.ResolveDeps:
        from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT
        from vmlease.rescue_image import ResolveDeps
        sha = hashlib.sha256(self._ARCH_QCOW).hexdigest()

        def text_fetcher(url: str) -> str:
            if url == archimage.MIRROR_BASE:
                return "v20260601.539459/"
            return f"{sha}  Arch-Linux-x86_64-cloudimg.qcow2"

        def fetcher(url: str) -> bytes:
            return self._ARCH_QCOW if url.endswith(".qcow2") else b"SIGBYTES"

        return ResolveDeps(
            text_fetcher=text_fetcher, fetcher=fetcher,
            gpg_runner=_fake_subprocess(0, f"[GNUPG:] VALIDSIG {DEFAULT_ARCH_KEY_FINGERPRINT} 2026 0 0\n"),
            write_temp=lambda _b: "/tmp/stage", keyring_path="/k",
        )

    def _deps(self, *, ssh_out: str = "RESCUE_WRITE_OK", ssh_rc: int = 0, cli_rc: int = 0,
              resolve_deps: rescue_image.ResolveDeps | None = None,
              push_calls: list[tuple[str, Path, str]] | None = None,
              ) -> tuple[archbuild.RescueWriteDeps, list[list[str]]]:
        cli_calls: list[list[str]] = []

        def cli(argv: list[str]) -> tuple[int, str, str]:
            cli_calls.append(argv)
            return (cli_rc, "", "" if cli_rc == 0 else "boom")

        def ssh_root(_ip: str, _script: str) -> tuple[int, str]:
            return (ssh_rc, ssh_out)

        def push_to_rescue(ip: str, local: Path, remote: str) -> None:
            if push_calls is not None:
                push_calls.append((ip, local, remote))

        deps = archbuild.RescueWriteDeps(
            resolve_deps=resolve_deps if resolve_deps is not None else self._arch_resolve_deps(),
            cli=cli, ssh_root=ssh_root, wait_rescue_ready=lambda _ip: None,
            push_to_rescue=push_to_rescue,
        )
        return deps, cli_calls

    def _host(self) -> Host:
        return Host(id="42", name="vmlease-run-rw-arch", ipv4="1.2.3.4")

    def test_rescue_write_host_happy_path(self) -> None:
        deps, cli_calls = self._deps()
        archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")
        verbs = [a[2] for a in cli_calls]  # hcloud server <verb>
        self.assertEqual(verbs, ["enable-rescue", "reset", "disable-rescue", "reset"])

    def test_rescue_write_host_remote_url_no_push(self) -> None:
        # Arch resolves a RemoteUrl → the rescue side curls it; no scp push.
        push_calls: list[tuple[str, Path, str]] = []
        deps, _ = self._deps(push_calls=push_calls)
        archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")
        self.assertEqual(push_calls, [])

    def test_rescue_write_host_write_failure_raises(self) -> None:
        deps, _ = self._deps(ssh_out="RESCUE_FAIL: sha mismatch", ssh_rc=14)
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")

    def test_rescue_write_host_cli_failure_raises(self) -> None:
        deps, _ = self._deps(cli_rc=1)
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")

    def test_local_file_pushed_before_script(self) -> None:
        # a golden LocalFile spec → push_to_rescue invoked with the fixed remote path.
        from vmlease.rescue_image import GoldenRescueImageSpec
        qcow = b"golden-bytes"
        sha = hashlib.sha256(qcow).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(qcow)
            profile = distro.DistroProfile(
                key="golden", default_image="debian-13", package_manager="apt",
                packages=(), rescue_image=GoldenRescueImageSpec(sha256=sha, path=local),
            )
            push_calls: list[tuple[str, Path, str]] = []
            deps, _ = self._deps(push_calls=push_calls)
            archbuild.rescue_write_host(self._host(), profile, deps, "mykey")
        self.assertEqual(len(push_calls), 1)
        self.assertEqual(push_calls[0][0], "1.2.3.4")
        self.assertEqual(push_calls[0][1], local)
        self.assertEqual(push_calls[0][2], archbuild.RESCUE_IMAGE_PATH)

    def test_trust_gate_aborts_before_any_mutation(self) -> None:
        # resolve_and_verify (SHA256 + pinned-key signature) runs FIRST; a bad image
        # raises BEFORE any hcloud cli step → zero mutations against an untrusted image.
        from vmlease.archimage import ArchImageError
        from vmlease.rescue_image import GoldenRescueImageSpec
        profile = distro.DistroProfile(
            key="golden", default_image="debian-13", package_manager="apt",
            packages=(), rescue_image=GoldenRescueImageSpec(sha256="0" * 64, url="https://m/x.qcow2"),
        )
        # the fetched bytes' sha will NOT match the pinned digest → refusal.
        from vmlease.rescue_image import ResolveDeps
        rd = ResolveDeps(
            text_fetcher=lambda _u: "", fetcher=lambda _u: b"not-the-pinned-bytes",
            gpg_runner=_fake_subprocess(0, ""), write_temp=lambda _b: "/tmp/x", keyring_path="/k",
        )
        deps, cli_calls = self._deps(resolve_deps=rd)
        with self.assertRaises(ArchImageError):
            archbuild.rescue_write_host(self._host(), profile, deps, "mykey")
        self.assertEqual(cli_calls, [])  # NOTHING ran — fail-closed before enable-rescue

    def _live_fakes(self, qcow: bytes) -> tuple[Callable[[list[str], str | None], tuple[int, str, str]], list[list[str]], Callable[[str], str], Callable[[str], bytes]]:
        from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT
        sha = hashlib.sha256(qcow).hexdigest()
        runs: list[list[str]] = []

        def fake_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            runs.append(argv)
            if argv[0] == "gpg":  # the trust-gate verify: emit a VALIDSIG for the pinned key
                return (0, f"[GNUPG:] VALIDSIG {DEFAULT_ARCH_KEY_FINGERPRINT} 2026 0 0\n", "")
            if argv[0] == "ssh":
                # mimic `bash -s` for each step's stdin script: the rescue-write
                # script -> RESCUE_WRITE_OK; the rescue readiness probe
                # (cat /etc/hostname) -> `rescue` (so _wait_rescue returns).
                if stdin and "qemu-img" in stdin:
                    return (0, "RESCUE_WRITE_OK", "")
                if stdin and "hostname" in stdin:
                    return (0, "rescue\n", "")
                return (0, "", "")
            return (0, "", "")  # hcloud cli steps

        def fake_text(url: str) -> str:
            return f"{sha}  img.qcow2" if url.endswith(".SHA256") else "v20260601.539459/"

        def fake_bytes(url: str) -> bytes:
            return qcow if url.endswith(".qcow2") else b"SIGBYTES"

        return fake_run, runs, fake_text, fake_bytes

    def test_build_live_rescue_writer_wires_and_runs(self) -> None:
        # exercise the live FACTORY wiring with injected fakes (no real I/O):
        # the trust-gate verify, the ssh argv shape, the readiness retry, full path.
        fake_run, runs, fake_text, fake_bytes = self._live_fakes(b"the-disk-image")
        writer = archbuild.build_live_rescue_writer(
            "/home/op/.ssh/key", "mykey", "/tmp/keyring.gpg",
            run=fake_run, sleep=lambda _s: None,
            fetch_text=fake_text, fetch_bytes=fake_bytes, write_temp=lambda _b: "/tmp/stage",
        )
        writer(Host(id="42", name="vmlease-arch", ipv4="1.2.3.4"), distro.get_profile("arch"))
        # the trust gate (gpg verify) ran, AND the ssh argv carries the injected key
        self.assertTrue(any(a[0] == "gpg" for a in runs))
        ssh_calls = [a for a in runs if a[0] == "ssh"]
        self.assertIn("/home/op/.ssh/key", ssh_calls[0])
        self.assertIn("StrictHostKeyChecking=accept-new", ssh_calls[0])

    def test_build_live_rescue_writer_local_file_scps(self) -> None:
        # a golden LOCAL profile through the live factory → the push_to_rescue
        # closure scps the file (root@ip via the rescue key) before the script.
        from vmlease.rescue_image import GoldenRescueImageSpec
        qcow = b"golden-local"
        sha = hashlib.sha256(qcow).hexdigest()
        runs: list[list[str]] = []

        def fake_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            runs.append(argv)
            if argv[0] == "ssh":
                if stdin and "hostname" in stdin:
                    return (0, "rescue\n", "")
                return (0, "RESCUE_WRITE_OK", "")
            return (0, "", "")

        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(qcow)
            profile = distro.DistroProfile(
                key="golden", default_image="debian-13", package_manager="apt",
                packages=(), rescue_image=GoldenRescueImageSpec(sha256=sha, path=local),
            )
            writer = archbuild.build_live_rescue_writer(
                "/home/op/.ssh/rescue", "mykey", "/tmp/keyring.gpg",
                run=fake_run, sleep=lambda _s: None,
            )
            writer(Host(id="9", name="g", ipv4="1.2.3.4"), profile)
        scp_calls = [a for a in runs if a[0] == "scp"]
        self.assertEqual(len(scp_calls), 1)
        self.assertIn("/home/op/.ssh/rescue", scp_calls[0])  # the rescue key
        self.assertIn(f"root@1.2.3.4:{archbuild.RESCUE_IMAGE_PATH}", scp_calls[0])
        self.assertNotIn("gpg", [a[0] for a in runs])  # golden → no gpg verify

    def test_build_live_rescue_writer_scp_failure_raises(self) -> None:
        from vmlease.rescue_image import GoldenRescueImageSpec
        qcow = b"golden-local"
        sha = hashlib.sha256(qcow).hexdigest()

        def fake_run(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            if argv[0] == "scp":
                return (1, "", "permission denied")
            if argv[0] == "ssh" and stdin and "hostname" in stdin:
                return (0, "rescue\n", "")
            return (0, "", "")

        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(qcow)
            profile = distro.DistroProfile(
                key="golden", default_image="debian-13", package_manager="apt",
                packages=(), rescue_image=GoldenRescueImageSpec(sha256=sha, path=local),
            )
            writer = archbuild.build_live_rescue_writer(
                "/k", "mykey", "/tmp/keyring.gpg", run=fake_run, sleep=lambda _s: None,
            )
            with self.assertRaises(archbuild.ArchBuildError):
                writer(Host(id="9", name="g", ipv4="1.2.3.4"), profile)

    def test_rescue_write_host_no_spec_raises(self) -> None:
        # a profile with no rescue_image spec cannot be rescue-written (guard).
        profile = distro.DistroProfile(
            key="nope", default_image="debian-13", package_manager="apt", packages=(),
        )
        deps, _ = self._deps()
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.rescue_write_host(self._host(), profile, deps, "mykey")

    def test_build_live_rescue_writer_readiness_timeout(self) -> None:
        # verify passes, but ssh never returns 0 → bounded readiness loop raises
        fake_run, _runs, fake_text, fake_bytes = self._live_fakes(b"img")

        def run_ssh_down(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            if argv[0] == "ssh":
                return (255, "", "no route")
            return fake_run(argv, stdin)

        writer = archbuild.build_live_rescue_writer(
            "/k", "mykey", "/tmp/keyring.gpg", run=run_ssh_down, sleep=lambda _s: None,
            fetch_text=fake_text, fetch_bytes=fake_bytes, write_temp=lambda _b: "/tmp/stage",
        )
        with self.assertRaises(archbuild.ArchBuildError):
            writer(Host(id="1", name="n", ipv4="1.2.3.4"), distro.get_profile("arch"))

    def test_build_keyring_import_argv(self) -> None:
        argv = archbuild.build_keyring_import_argv("/tmp/kr.gpg", "FP")
        self.assertIn("--recv-keys", argv)
        self.assertIn("FP", argv)
        self.assertIn("/tmp/kr.gpg", argv)

    def test_ensure_arch_keyring_failure_raises(self) -> None:
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.ensure_arch_keyring("/tmp/kr.gpg", lambda _argv, _stdin: (2, "", "no network"))

    def test_ensure_arch_keyring_success(self) -> None:
        calls: list[list[str]] = []

        def ok(argv: list[str], _stdin: str | None) -> tuple[int, str, str]:
            calls.append(argv)
            return (0, "imported", "")

        archbuild.ensure_arch_keyring("/tmp/kr.gpg", ok, "FP")
        self.assertIn("--recv-keys", calls[0])

    def test_live_write_temp_stages_bytes(self) -> None:
        path = archbuild._live_write_temp(b"qcow2-bytes")
        try:
            self.assertEqual(Path(path).read_bytes(), b"qcow2-bytes")
        finally:
            Path(path).unlink()


# --------------------------------------------------------------------------- #
# rescue_image — the RescueImageSpec seam (Arch + golden)
# --------------------------------------------------------------------------- #
class TestRescueImageSpec(unittest.TestCase):
    def _arch_deps(self, qcow: bytes) -> rescue_image.ResolveDeps:
        from vmlease.archimage import DEFAULT_ARCH_KEY_FINGERPRINT
        from vmlease.rescue_image import ResolveDeps
        sha = hashlib.sha256(qcow).hexdigest()

        def text_fetcher(url: str) -> str:
            return "v20260601.539459/" if url == archimage.MIRROR_BASE else f"{sha}  x.qcow2"

        return ResolveDeps(
            text_fetcher=text_fetcher,
            fetcher=lambda u: qcow if u.endswith(".qcow2") else b"SIG",
            gpg_runner=_fake_subprocess(0, f"[GNUPG:] VALIDSIG {DEFAULT_ARCH_KEY_FINGERPRINT} 2026 0 0\n"),
            write_temp=lambda _b: "/tmp/stage", keyring_path="/k",
        )

    def _golden_deps(self, *, fetcher: Callable[[str], bytes] | None = None) -> rescue_image.ResolveDeps:
        from vmlease.rescue_image import ResolveDeps
        # text_fetcher/gpg_runner must NEVER be touched by a golden spec → make them
        # raise so any accidental use fails loudly.
        def boom_text(_u: str) -> str:
            raise AssertionError("golden spec must not call text_fetcher")

        def boom_gpg(_argv: list[str]) -> subprocess.CompletedProcess[str]:
            raise AssertionError("golden spec must not invoke gpg")

        return ResolveDeps(
            text_fetcher=boom_text, fetcher=fetcher if fetcher is not None else (lambda _u: b""),
            gpg_runner=boom_gpg, write_temp=lambda _b: "/tmp/x", keyring_path="/k",
        )

    def test_arch_spec_yields_remote_url_and_sha(self) -> None:
        from vmlease.rescue_image import ArchRescueImageSpec, RemoteUrl
        qcow = b"the-disk-image"
        sha = hashlib.sha256(qcow).hexdigest()
        spec = ArchRescueImageSpec(fingerprint=archimage.DEFAULT_ARCH_KEY_FINGERPRINT)
        resolved = spec.resolve_and_verify(self._arch_deps(qcow))
        self.assertEqual(resolved.expected_sha256, sha)
        self.assertIsInstance(resolved.source, RemoteUrl)
        assert isinstance(resolved.source, RemoteUrl)
        self.assertTrue(resolved.source.url.endswith("v20260601.539459/Arch-Linux-x86_64-cloudimg.qcow2"))

    def test_golden_url_yields_remote_url_no_gpg(self) -> None:
        from vmlease.rescue_image import GoldenRescueImageSpec, RemoteUrl
        qcow = b"golden-url-bytes"
        sha = hashlib.sha256(qcow).hexdigest()
        spec = GoldenRescueImageSpec(sha256=sha, url="https://m/golden.qcow2")
        resolved = spec.resolve_and_verify(self._golden_deps(fetcher=lambda _u: qcow))
        self.assertEqual(resolved.expected_sha256, sha)
        self.assertIsInstance(resolved.source, RemoteUrl)
        assert isinstance(resolved.source, RemoteUrl)
        self.assertEqual(resolved.source.url, "https://m/golden.qcow2")

    def test_golden_local_yields_local_file_no_gpg(self) -> None:
        from vmlease.rescue_image import GoldenRescueImageSpec, LocalFile
        qcow = b"golden-local-bytes"
        sha = hashlib.sha256(qcow).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(qcow)
            spec = GoldenRescueImageSpec(sha256=sha, path=local)
            resolved = spec.resolve_and_verify(self._golden_deps())  # fetcher unused
            self.assertEqual(resolved.expected_sha256, sha)
            self.assertIsInstance(resolved.source, LocalFile)
            assert isinstance(resolved.source, LocalFile)
            self.assertEqual(resolved.source.path, local)

    def test_golden_url_sha_mismatch_refuses(self) -> None:
        from vmlease.archimage import ArchImageError
        from vmlease.rescue_image import GoldenRescueImageSpec
        spec = GoldenRescueImageSpec(sha256="0" * 64, url="https://m/golden.qcow2")
        with self.assertRaises(ArchImageError):
            spec.resolve_and_verify(self._golden_deps(fetcher=lambda _u: b"wrong-bytes"))

    def test_golden_local_sha_mismatch_refuses(self) -> None:
        from vmlease.archimage import ArchImageError
        from vmlease.rescue_image import GoldenRescueImageSpec
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "golden.qcow2"
            local.write_bytes(b"wrong-bytes")
            spec = GoldenRescueImageSpec(sha256="0" * 64, path=local)
            with self.assertRaises(ArchImageError):
                spec.resolve_and_verify(self._golden_deps())

    def test_golden_requires_exactly_one_source(self) -> None:
        from vmlease.archimage import ArchImageError
        from vmlease.rescue_image import GoldenRescueImageSpec
        # neither url nor path
        with self.assertRaises(ArchImageError):
            GoldenRescueImageSpec(sha256="a" * 64).resolve_and_verify(self._golden_deps())
        # both url AND path
        with tempfile.TemporaryDirectory() as d:
            local = Path(d) / "x.qcow2"
            local.write_bytes(b"x")
            both = GoldenRescueImageSpec(sha256="a" * 64, url="https://m/x", path=local)
            with self.assertRaises(ArchImageError):
                both.resolve_and_verify(self._golden_deps())

    def test_both_instances_satisfy_protocol(self) -> None:
        from vmlease.rescue_image import ArchRescueImageSpec, GoldenRescueImageSpec, RescueImageSpec
        arch = ArchRescueImageSpec(fingerprint="F")
        golden = GoldenRescueImageSpec(sha256="a" * 64, url="https://m/x")
        self.assertIsInstance(arch, RescueImageSpec)
        self.assertIsInstance(golden, RescueImageSpec)


# --------------------------------------------------------------------------- #
# distro — the rescue-write Arch profile
# --------------------------------------------------------------------------- #
class TestDistroRescue(unittest.TestCase):
    def test_arch_needs_rescue_write(self) -> None:
        from vmlease.rescue_image import ArchRescueImageSpec, RescueImageSpec
        arch = distro.get_profile("arch")
        self.assertTrue(arch.needs_rescue_write)
        self.assertIsInstance(arch.rescue_image, ArchRescueImageSpec)
        self.assertIsInstance(arch.rescue_image, RescueImageSpec)  # Protocol conformance
        assert isinstance(arch.rescue_image, ArchRescueImageSpec)
        self.assertEqual(arch.rescue_image.fingerprint, archimage.DEFAULT_ARCH_KEY_FINGERPRINT)
        self.assertEqual(arch.default_image, "debian-13")  # cheap base to rescue-write

    def test_native_distros_do_not_need_rescue_write(self) -> None:
        for key in ("ubuntu", "debian", "fedora"):
            profile = distro.get_profile(key)
            self.assertFalse(profile.needs_rescue_write)
            self.assertIsNone(profile.rescue_image)


if __name__ == "__main__":
    unittest.main()
