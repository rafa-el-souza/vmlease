# Example batteries

Reference probe batteries you can run against real hosts with `vmlease run`.

- **`compose-plugin-check/battery.toml`** — a read-only, no-upload smoke that a freshly-provisioned VM has
  the docker prerequisites vmlease's cloud-init installs (Docker, the `docker compose` v2 plugin, buildx,
  `script(1)` — fedora splits the last into `util-linux-script`). It guards vmlease's per-distro install
  template against regressions. Run it after changing `src/vmlease/distro.py` or `cloudinit.py`:

  ```sh
  uv run vmlease run --battery examples/compose-plugin-check/battery.toml \
    --distros ubuntu,debian,fedora,arch --run-token compose-check \
    --results-dir /tmp/vmlease-results --timestamp "$(date -u +%Y%m%dT%H%M%SZ)" \
    --ssh-key <registered-name> --ssh-key-path <local-priv> --yes
  ```
