# Exporter auto-deployment for new Slurm nodes

Automation that keeps the Prometheus targets in sync with the Slurm cluster
and deploys the jobstats exporters (cgroup, node, nvidia-gpu) to newly added
nodes via salt.

Runs on the Prometheus server on a systemd timer (4x daily by default).

## What it does on each run

1. Queries Slurm for the current node list and GPU info
   (`sinfo -h -N -o '%N|%G'`). A node counts as a GPU node if its GRES
   contains a `gpu:` entry.
2. For each targets file in `/db/prometheus/targets/`:
   - `cgroup_targets.yml` (port 9306) — all nodes
   - `gpu_targets.yml` (port 9445) — GPU nodes only
   - `node_targets.yml` (port 9100) — all nodes

   appends any node not already present as `'<node>.inband:<port>'`,
   preserving the existing file format. Writes are atomic, so Prometheus
   file_sd never sees a partial file (no Prometheus reload needed with
   file_sd).
3. Runs a readiness check on every node pending deployment:
   - **Slurm state** must be healthy — nodes that are down, drained,
     draining, failed, in maintenance, powered off, rebooting, or not
     responding (`*`/`~`/`#`/... state flags) are skipped;
   - **ssh reachability** — the node must answer via clush/ssh;
   - **load** — the 1-min load average must not exceed
     `CPUs * --max-load-ratio` (default 1.0), so heavily utilized nodes
     are not disturbed.

   Nodes failing any check are skipped with a warning and automatically
   re-checked on the next timer run.
4. Deploys the exporters to ready nodes that haven't been deployed to
   yet (cgroup + node exporters on every new node, nvidia-gpu exporter
   only on new GPU nodes):

   ```
   clush -o "-oConnectTimeout=3" -w <new_nodes> 'salt-call state.apply engaging.role.cgroup-exporter'
   clush -o "-oConnectTimeout=3" -w <new_nodes> 'salt-call state.apply engaging.role.node-exporter'
   clush -o "-oConnectTimeout=3" -w <new_nodes> 'salt-call state.apply engaging.role.nvidia-gpu-exporter'
   ```

Deployment success is tracked per exporter in
`/var/lib/exporter-autodeploy/state.json`. If a clush/salt run fails (e.g. a
node is down), the affected nodes stay pending and deployment is retried on
the next timer run — salt states are idempotent, so re-applying is safe.

On the very first run (no state file yet), nodes already listed in the
targets files are assumed to be deployed already, so salt is not re-applied
to the whole cluster.

## Installation (on the Prometheus server)

```bash
cp autodeploy_exporters.py /usr/local/bin/
chmod +x /usr/local/bin/autodeploy_exporters.py
cp exporter-autodeploy.service exporter-autodeploy.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now exporter-autodeploy.timer
```

Requirements on that host: python3 (stdlib only), `sinfo` (slurm client),
`clush` with passwordless ssh/root access to the compute nodes, write access
to `/db/prometheus/targets`.

## Usage

```bash
# See what would happen, change nothing:
autodeploy_exporters.py --dry-run

# Only update target files, skip salt deployment:
autodeploy_exporters.py --no-deploy

# Exclude nodes by regex (e.g. test partition nodes):
autodeploy_exporters.py --exclude '^testnode'

# Allow deployment to busier nodes (skip only if load1 > 2x CPU count):
autodeploy_exporters.py --max-load-ratio 2.0

# Run immediately via systemd:
systemctl start exporter-autodeploy.service
journalctl -u exporter-autodeploy.service
```

Configuration defaults (targets dir, node suffix, ports, salt states, state
file path) live at the top of `autodeploy_exporters.py` and can be
overridden with `--targets-dir` / `--state-file`.

## Notes

- The script never removes targets; decommissioned nodes must be cleaned
  out of the yml files (and the state file) manually.
- GPU detection relies on Slurm GRES being configured for the node. If a
  brand-new GPU node is added before its `Gres=gpu:...` line is set in
  slurm.conf, it will initially be treated as CPU-only; the gpu exporter
  is picked up automatically on a later run once GRES is configured.
