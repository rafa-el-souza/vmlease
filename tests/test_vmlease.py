#!/usr/bin/env python3
"""Unit tests for vmlease — all mocked, NO network, NO real VMs.

stdlib unittest only. Run with:
    uv run python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
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
    results,
    runner,
    safety,
    ssh,
    templating,
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


class FakeSshRunner:
    """Scripted SshRunner: returns exit 0 by default, or raises/fails per config."""

    def __init__(self, *, fail_on: str | None = None, raise_on: str | None = None) -> None:
        self.ran: list[str] = []
        self._fail_on = fail_on
        self._raise_on = raise_on

    def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
        self.ran.append(probe.id)
        if self._raise_on is not None and probe.id == self._raise_on:
            raise ssh.SshError(f"boom on {probe.id}")
        code = 7 if probe.id == self._fail_on else 0
        return ProbeResult(probe_id=probe.id, tag=probe.tag, exit_code=code, stdout=f"out-{probe.id}", stderr="")


def _fake_keypair(tmp: Path) -> keypair.Keypair:
    d = tmp / "kp"
    d.mkdir(parents=True, exist_ok=True)
    priv = d / "id_ed25519"
    priv.write_text("PRIV", encoding="utf-8")
    return keypair.Keypair(directory=d, private_key_path=priv, public_key="ssh-ed25519 AAAA probe")


_BATTERY_JSON = json.dumps(
    {
        "name": "demo-battery",
        "probes": [
            {"id": "P1", "title": "subid", "command": "grep x /etc/subuid", "tag": "read-only", "classifies": "L2"},
            {"id": "P6", "title": "linger", "command": "loginctl enable-linger", "tag": "mutating:operator-space"},
            {"id": "P12", "title": "batch", "command": "sudo true", "tag": "mutating:host-root"},
        ],
    }
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

    def test_battery_ordered_groups_by_tag(self) -> None:
        b = battery_mod.parse_battery(_BATTERY_JSON)
        order = [p.tag for p in b.ordered()]
        self.assertEqual(
            order,
            [model.ProbeTag.READ_ONLY, model.ProbeTag.MUTATING_OPERATOR_SPACE, model.ProbeTag.MUTATING_HOST_ROOT],
        )

    def test_battery_ordered_stable_within_group(self) -> None:
        # two host-root probes keep authoring order (the dependency order)
        doc = {
            "name": "x",
            "probes": [
                {"id": "B2", "title": "second", "command": "c", "tag": "mutating:host-root"},
                {"id": "B1", "title": "first", "command": "c", "tag": "mutating:host-root"},
            ],
        }
        b = battery_mod.parse_battery(json.dumps(doc))
        self.assertEqual([p.id for p in b.ordered()], ["B2", "B1"])


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
        prov = providers.HetznerProvider(runner=_fake_subprocess(0, out))
        host = prov.create_with_cloudinit(self._spec(), "#!/bin/bash\necho hi")
        self.assertEqual((host.id, host.ipv4), ("5", "1.1.1.1"))

    def test_create_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_subprocess(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.create_with_cloudinit(self._spec(), "#!/bin/bash")

    def test_destroy_idempotent_on_not_found(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_subprocess(1, "", "server not found"))
        prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)  # no raise

    def test_destroy_non_transient_error_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_subprocess(1, "", "forbidden"))
        with self.assertRaises(providers.ProviderError):
            prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)

    def test_destroy_retries_transient_timeout_then_succeeds(self) -> None:
        # first call times out (transient), second succeeds → no raise, 2 calls
        calls = {"n": 0}

        def flaky(argv: list[str]) -> subprocess.CompletedProcess[str]:
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(argv, 1, "", "request timeout, please retry")
            return subprocess.CompletedProcess(argv, 0, "", "")

        prov = providers.HetznerProvider(runner=flaky)
        prov.destroy(model.Host(id="9", name="n", ipv4=""), sleep=lambda _s: None)
        self.assertEqual(calls["n"], 2)

    def test_destroy_persistent_timeout_eventually_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_subprocess(1, "", "request timeout, please retry"))
        with self.assertRaises(providers.ProviderError):
            prov.destroy(model.Host(id="9", name="n", ipv4=""), attempts=3, sleep=lambda _s: None)

    def test_list_labeled(self) -> None:
        out = json.dumps([{"id": 1, "name": "a", "labels": {"vmlease": "r1"}}])
        prov = providers.HetznerProvider(runner=_fake_subprocess(0, out))
        self.assertEqual(len(prov.list_labeled("r1")), 1)

    def test_list_labeled_failure_raises(self) -> None:
        prov = providers.HetznerProvider(runner=_fake_subprocess(1, "", "boom"))
        with self.assertRaises(providers.ProviderError):
            prov.list_labeled("r1")

    def test_provider_protocol_satisfied(self) -> None:
        self.assertIsInstance(FakeProvider(), providers.Provider)


# --------------------------------------------------------------------------- #
# battery loader
# --------------------------------------------------------------------------- #
class TestBattery(unittest.TestCase):
    def test_parse_ok(self) -> None:
        b = battery_mod.parse_battery(_BATTERY_JSON)
        self.assertEqual(b.name, "demo-battery")
        self.assertEqual(len(b.probes), 3)
        self.assertEqual(b.probes[0].classifies, "L2")

    def test_parse_bad_json(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery("{nope")

    def test_parse_non_object_root(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(json.dumps([1, 2, 3]))

    def test_parse_probe_not_object(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(json.dumps({"name": "x", "probes": ["not-an-object"]}))

    def test_parse_missing_name(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(json.dumps({"probes": [{"id": "P", "title": "t", "command": "c", "tag": "read-only"}]}))

    def test_parse_empty_probes(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(json.dumps({"name": "x", "probes": []}))

    def test_parse_missing_probe_field(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(json.dumps({"name": "x", "probes": [{"id": "P", "title": "t", "command": "c"}]}))

    def test_parse_unknown_tag(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(json.dumps({"name": "x", "probes": [{"id": "P", "title": "t", "command": "c", "tag": "weird"}]}))

    def test_parse_duplicate_id(self) -> None:
        doc = {"name": "x", "probes": [
            {"id": "P", "title": "t", "command": "c", "tag": "read-only"},
            {"id": "P", "title": "t2", "command": "c2", "tag": "read-only"},
        ]}
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.parse_battery(json.dumps(doc))

    def test_load_battery_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "b.json"
            p.write_text(_BATTERY_JSON, encoding="utf-8")
            b = battery_mod.load_battery(p)
            self.assertEqual(b.name, "demo-battery")

    def test_load_battery_missing_file(self) -> None:
        with self.assertRaises(battery_mod.BatteryError):
            battery_mod.load_battery(Path("/no/such/battery.json"))


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
            battery=battery_mod.parse_battery(_BATTERY_JSON),
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
        m = runner.Matrix(battery_mod.parse_battery(_BATTERY_JSON), ("ubuntu",), "cpx22", "run-fw", firewall="my-firewall")
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
        self.assertEqual(items[0].probe_count, 3)

    def test_plan_surfaces_cost_guard(self) -> None:
        m = self._matrix(distros=("ubuntu", "debian", "fedora"))
        with self.assertRaises(safety.CostGuardError):
            runner.plan(m, cost_guard=safety.CostGuard(max_hosts=2))

    def test_plan_unknown_distro(self) -> None:
        m = runner.Matrix(battery=battery_mod.parse_battery(_BATTERY_JSON), distro_keys=("nope",), server_type="cpx22", run_token="t-okay")
        with self.assertRaises(distro.UnknownDistroError):
            runner.plan(m)


# --------------------------------------------------------------------------- #
# cli — plan subcommand (zero provider calls)
# --------------------------------------------------------------------------- #
class TestCli(unittest.TestCase):
    def _write_battery(self, d: str) -> str:
        p = Path(d) / "b.json"
        p.write_text(_BATTERY_JSON, encoding="utf-8")
        return str(p)

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
            p = Path(d) / "bad.json"
            p.write_text("{nope", encoding="utf-8")
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
        r = OpenSshRunnerForTest(_fake_subprocess(7, "out", "err"))
        probe = Probe(id="P1", title="t", command="c", tag=ProbeTag.READ_ONLY)
        res = r.run_probe(self._host(), probe)
        self.assertEqual((res.exit_code, res.stdout), (7, "out"))

    def test_wait_until_ready_polls_then_succeeds(self) -> None:
        # fail twice then succeed; sleeper is a no-op recorder
        seq = [1, 1, 0]
        calls = {"n": 0}

        def runner_fn(argv: list[str]) -> subprocess.CompletedProcess[str]:
            rc = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return subprocess.CompletedProcess(argv, rc, "", "")

        slept: list[float] = []
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=runner_fn, sleeper=slept.append)
        r.wait_until_ready(self._host(), attempts=5)
        self.assertEqual(calls["n"], 3)

    def test_wait_until_ready_times_out(self) -> None:
        r = ssh.OpenSshRunner("probe", Path("/tmp/k"), runner=_fake_subprocess(1), sleeper=lambda _x: None)
        with self.assertRaises(ssh.SshError):
            r.wait_until_ready(self._host(), attempts=2)

    def test_ssh_runner_protocol_satisfied(self) -> None:
        self.assertIsInstance(FakeSshRunner(), ssh.SshRunner)


def OpenSshRunnerForTest(runner_fn: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> ssh.OpenSshRunner:
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
# results serialization
# --------------------------------------------------------------------------- #
class TestResults(unittest.TestCase):
    def _run(self) -> model.HostRun:
        spec = HostSpec(name="vmlease-r1-ubuntu", image="ubuntu-24.04", server_type="cpx22", distro_key="ubuntu")
        res = (ProbeResult("P1", ProbeTag.READ_ONLY, 0, "ok", ""),)
        return model.HostRun(host_spec=spec, detail="## os-release\nID=ubuntu", results=res)

    def test_filename_deterministic(self) -> None:
        self.assertEqual(results.results_filename("r1", "20260601T000000Z"), "vmlease-r1-20260601T000000Z.json")

    def test_serialize_round_trips(self) -> None:
        text = results.serialize_run("r1", "20260601T000000Z", [self._run()])
        doc = json.loads(text)
        self.assertEqual(doc["run_id"], "r1")
        self.assertEqual(doc["hosts"][0]["probes"][0]["id"], "P1")
        self.assertTrue(doc["hosts"][0]["probes"][0]["ok"])

    def test_write_results_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = results.write_results(Path(d) / "out", "r1", "20260601T000000Z", [self._run()])
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "vmlease-r1-20260601T000000Z.json")


# --------------------------------------------------------------------------- #
# runner.execute — the teardown-ALWAYS guarantee
# --------------------------------------------------------------------------- #
class TestExecute(unittest.TestCase):
    def _matrix(self, distros: tuple[str, ...] = ("ubuntu", "debian")) -> runner.Matrix:
        return runner.Matrix(battery_mod.parse_battery(_BATTERY_JSON), distros, "cpx22", "run-xyz")

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
        # battery ran in tag order on each host (P1 read-only first, P12 host-root last)
        self.assertEqual(fssh.ran[-1], "P12")

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
        m = runner.Matrix(battery_mod.parse_battery(_BATTERY_JSON), ("ubuntu", "debian", "fedora"), "cpx22", "run-par")
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
        self.assertIn("WARNING: teardown", runs[0].detail)  # failure noted, not raised

    def test_parallel_one_failure_does_not_discard_others(self) -> None:
        prov = FakeProvider()

        class _SelectiveSsh:
            def run_probe(self, host: Host, probe: Probe) -> ProbeResult:
                if "debian" in host.name:
                    raise ssh.SshError("debian unreachable")
                return ProbeResult(probe.id, probe.tag, 0, "ok", "")

        m = runner.Matrix(battery_mod.parse_battery(_BATTERY_JSON), ("ubuntu", "debian"), "cpx22", "run-par2")
        with tempfile.TemporaryDirectory() as d:
            runs = runner.execute(m, prov, lambda _o, _k: _SelectiveSsh(), _fake_keypair(Path(d)), "probe", max_parallel=2)
        self.assertEqual(len(runs), 2)
        self.assertEqual(len(prov.destroyed), 2)
        by_distro = {r.host_spec.distro_key: r for r in runs}
        self.assertTrue(by_distro["ubuntu"].results)  # preserved
        self.assertTrue(by_distro["debian"].detail.startswith("ERROR:"))


# --------------------------------------------------------------------------- #
# cli — run (confirm gate) / reap / status, with provider + ssh stubbed
# --------------------------------------------------------------------------- #
class TestCliRun(unittest.TestCase):
    def _write_battery(self, d: str) -> str:
        p = Path(d) / "b.json"
        p.write_text(_BATTERY_JSON, encoding="utf-8")
        return str(p)

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
        return runner.Matrix(battery_mod.parse_battery(_BATTERY_JSON), ("arch",), "cpx22", "run-rw")

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
        m = runner.Matrix(battery_mod.parse_battery(_BATTERY_JSON), ("ubuntu",), "cpx22", "run-nat")
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

    def test_render_rescue_script_fills_slots_and_probes_disk(self) -> None:
        s = archbuild.render_rescue_script("https://m/Arch.qcow2", "a" * 64)
        self.assertIn("https://m/Arch.qcow2", s)
        self.assertIn("a" * 64, s)
        # the script probes the disk (never hardcodes sda) and re-verifies the sha
        self.assertIn("lsblk", s)
        self.assertIn("sha256sum -c", s)
        self.assertIn("qemu-img convert -O raw", s)
        # shell vars pass through the @@ templating untouched
        self.assertIn("$disk", s)

    def _resolved(self) -> archimage.ResolvedImage:
        from vmlease.archimage import ArchVersion, ResolvedImage
        return ResolvedImage(version=ArchVersion(date=20260601, serial=539459), qcow2_bytes=b"img", expected_sha256="a" * 64)

    def _deps(self, *, ssh_out: str = "RESCUE_WRITE_OK", ssh_rc: int = 0, cli_rc: int = 0, verify_raises: bool = False) -> tuple[archbuild.RescueWriteDeps, list[list[str]]]:
        from vmlease.archimage import ArchImageError
        cli_calls: list[list[str]] = []
        resolved = self._resolved()

        def verify(_profile: distro.DistroProfile) -> archimage.ResolvedImage:
            if verify_raises:
                raise ArchImageError("bad signature")
            return resolved

        def cli(argv: list[str]) -> tuple[int, str, str]:
            cli_calls.append(argv)
            return (cli_rc, "", "" if cli_rc == 0 else "boom")

        def ssh_root(_ip: str, _script: str) -> tuple[int, str]:
            return (ssh_rc, ssh_out)

        deps = archbuild.RescueWriteDeps(
            verify=verify, cli=cli, ssh_root=ssh_root,
            wait_rescue_ready=lambda _ip: None,
        )
        return deps, cli_calls

    def _host(self) -> Host:
        return Host(id="42", name="vmlease-run-rw-arch", ipv4="1.2.3.4")

    def test_rescue_write_host_happy_path(self) -> None:
        deps, cli_calls = self._deps()
        archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")
        verbs = [a[2] for a in cli_calls]  # hcloud server <verb>
        self.assertEqual(verbs, ["enable-rescue", "reset", "disable-rescue", "reset"])

    def test_rescue_write_host_write_failure_raises(self) -> None:
        deps, _ = self._deps(ssh_out="RESCUE_FAIL: sha mismatch", ssh_rc=14)
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")

    def test_rescue_write_host_cli_failure_raises(self) -> None:
        deps, _ = self._deps(cli_rc=1)
        with self.assertRaises(archbuild.ArchBuildError):
            archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")

    def test_trust_gate_aborts_before_any_mutation(self) -> None:
        # verify (SHA256 + pinned-key signature) runs FIRST; a bad image raises
        # BEFORE any hcloud cli step → zero mutations against an untrusted image.
        from vmlease.archimage import ArchImageError
        deps, cli_calls = self._deps(verify_raises=True)
        with self.assertRaises(ArchImageError):
            archbuild.rescue_write_host(self._host(), distro.get_profile("arch"), deps, "mykey")
        self.assertEqual(cli_calls, [])  # NOTHING ran — fail-closed before enable-rescue

    def test_rescue_image_url(self) -> None:
        from vmlease.archimage import latest_version
        v = latest_version("v20260601.539459/")
        url = archbuild.rescue_image_url(v, distro.get_profile("arch"))
        self.assertTrue(url.endswith("v20260601.539459/Arch-Linux-x86_64-cloudimg.qcow2"))

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
# distro — the rescue-write Arch profile
# --------------------------------------------------------------------------- #
class TestDistroRescue(unittest.TestCase):
    def test_arch_needs_rescue_write(self) -> None:
        arch = distro.get_profile("arch")
        self.assertTrue(arch.needs_rescue_write)
        self.assertEqual(arch.rescue_image, "Arch-Linux-x86_64-cloudimg.qcow2")
        self.assertEqual(arch.default_image, "debian-13")  # cheap base to rescue-write

    def test_native_distros_do_not_need_rescue_write(self) -> None:
        for key in ("ubuntu", "debian", "fedora"):
            self.assertFalse(distro.get_profile(key).needs_rescue_write)


if __name__ == "__main__":
    unittest.main()
