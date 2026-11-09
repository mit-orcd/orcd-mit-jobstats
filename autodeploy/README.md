# Exporter auto-deployment and Prometheus target sync

Automation that deploys the jobstats exporters (cgroup, node, nvidia-gpu)
to new Slurm nodes and keeps the Prometheus file_sd target files in sync
with the Slurm cluster. It is split into two independent services, one
per host, because the Prometheus server (`core025`) cannot run clush/salt
against the compute nodes:

| Host | Service | Does |
|------|---------|------|
| **admin001** | `exporter-deploy.timer` → `deploy_exporters.py` | Deploys exporters to new nodes via clush + salt |
| **core025** (Prometheus) | `prometheus-targets-sync.timer` → `sync_prometheus_targets.py` | Adds new nodes to / removes departed nodes from the target files |

The two services do not talk to each other. They are coupled only through
the exporters themselves: admin001 deploys an exporter, and core025 adds a
node to a targets file once that node's exporter answers on its port.

```
admin001                                       core025 (Prometheus)
  scontrol: list Slurm nodes                     scontrol: list Slurm nodes
  probe exporter ports                           probe exporter ports
  clush + salt-call: deploy  ──► compute ◄──     add/remove entries in
  state: /var/lib/exporter-deploy/  nodes        /db/prometheus/targets/*.yml
                                                 state: /var/lib/prometheus-targets-sync/
```

Files:

- `exporter_common.py` — shared code (exporter list, Slurm query, port probe); install on both hosts
- `deploy_exporters.py`, `exporter-deploy.service`, `exporter-deploy.timer` — admin001
- `sync_prometheus_targets.py`, `prometheus-targets-sync.service`, `prometheus-targets-sync.timer` — core025

Exporters, ports, and salt states (defined in `exporter_common.py`):

| Exporter | Targets file | Port | Salt state | Nodes |
|----------|--------------|------|------------|-------|
| cgroup | `cgroup_targets.yml` | 9306 | `engaging.role.cgroup-exporter` | all |
| gpu | `gpu_targets.yml` | 9445 | `engaging.role.nvidia-gpu-exporter` | GPU nodes (gpu GRES) |
| node | `node_targets.yml` | 9100 | `engaging.role.node-exporter` | all |

Nodes are listed with `scontrol -o show node`, which includes every node
defined in Slurm, even nodes in no partition. A node counts as a GPU node
if its `Gres` contains a `gpu` entry. Scrape addresses are
`<node>.inband:<port>`.

## admin001: `deploy_exporters.py`

Each run (4x daily by default):

1. Lists all Slurm nodes and keeps only those with the Slurm node feature
   `rocky8` (`sinfo -o "%N %f"`; `REQUIRED_FEATURES`, or `--feature`,
   repeatable to allow several, e.g. `--feature rocky8 --feature rocky9`).
   Nodes tagged e.g. `centos7` are skipped without being contacted.
2. Drops nodes that are no longer in Slurm from the deployment state, so
   a node re-added later under the same name gets deployed again.
3. For each exporter, takes nodes not yet marked deployed and probes the
   exporter port (`http://<node>.inband:<port>/metrics`). Nodes where it
   already answers are marked deployed without running salt. This also
   bootstraps the state on the first run.
4. Runs a readiness check on the remaining nodes:
   - **Slurm state** healthy — down, drained, failing, maintenance,
     powered off, rebooting, or not-responding nodes are retried next run;
   - **ssh** reachable via clush;
   - **OS** RHEL/Rocky >= 8 (`ALLOWED_OS_IDS` / `MIN_OS_MAJOR`) — a second
     check on the node itself, in case a Slurm feature is wrong. Nodes
     failing either check are never deployed, so their exporters never
     answer and core025 never adds them as targets;
   - **load** — 1-min load <= `CPUs * --max-load-ratio` (default 1.0).
5. Deploys to ready nodes:

   ```
   clush -o "-oConnectTimeout=3" -w <nodes> 'salt-call state.apply engaging.role.cgroup-exporter'
   clush -o "-oConnectTimeout=3" -w <nodes> 'salt-call state.apply engaging.role.node-exporter'
   clush -o "-oConnectTimeout=3" -w <gpu_nodes> 'salt-call state.apply engaging.role.nvidia-gpu-exporter'
   ```

   Success is recorded per exporter in `/var/lib/exporter-deploy/state.json`.
   Failed nodes stay pending and are retried next run (salt states are
   idempotent).

Requirements on admin001: python3 (stdlib only), `scontrol`, `clush` with
passwordless ssh to compute nodes, `salt-call` on compute nodes, and HTTP
access to the exporter ports. If HTTP to the nodes is blocked, use
`--no-probe`; the state file is then the only record of deployed nodes.

## core025: `sync_prometheus_targets.py`

Each run (hourly by default) edits the files in `/db/prometheus/targets`
locally:

1. Lists all Slurm nodes. Only nodes with the feature `rocky8` (same rule
   and `--feature` option as on admin001) are eligible to be targets.
2. **Add**: for each eligible node missing from a targets file (GPU nodes only
   for `gpu_targets.yml`), probes the exporter port and appends
   `'<node>.inband:<port>'` only if the exporter answers. New nodes appear
   within about an hour of admin001 deploying them.
3. **Remove**: drops entries for nodes that are no longer defined in Slurm,
   or that are still in Slurm but lack the required feature (e.g. existing
   `centos7` entries). Safety rails:
   - Only **managed** entries are removed — nodes this script has seen in
     Slurm at least once. Hand-added, non-Slurm targets (e.g. infrastructure
     servers in `node_targets.yml`) are never touched.
   - A node must be ineligible in `--remove-after` consecutive runs
     (default 2) before removal, so one bad Slurm query cannot drop it.
   - If more than `--max-remove` nodes (default 20) would be removed in one
     run, nothing is removed and the run fails. Use `--force` to confirm a
     large decommission — including the first cleanup of existing centos7
     entries if there are more than 20:
     `sync_prometheus_targets.py --dry-run` (check the list), then
     `sync_prometheus_targets.py --force` twice (the 2-run grace period).
   - An empty or failed Slurm query aborts the run.

   Nodes that are down or drained but still defined in Slurm are kept.

Edits preserve the rest of each file (labels, other target groups,
indentation, mode, ownership). Writes are atomic (temp file + rename), so
Prometheus file_sd sees the change without a reload.

Requirements on core025: python3 (stdlib only), write access to
`/db/prometheus/targets`, and either a working `scontrol` (Slurm client +
munge) or `--slurm-ssh USER@HOST` with a passwordless ssh key to a host
that has one (e.g. a login node).

## Installation

### With `deploy.sh` (recommended)

Run as root on **admin001** from a checkout of this repo. It installs the
deploy service locally and the target sync on core025 over ssh
(passwordless `root@core025` key required). Each side runs a `--dry-run`
check before its timer is enabled:

```bash
./autodeploy/deploy.sh                                   # admin001 + root@core025
./autodeploy/deploy.sh --slurm-ssh root@<login-node>     # core025 has no Slurm client
./autodeploy/deploy.sh --remote-only                     # redeploy only core025
./autodeploy/deploy.sh --no-enable                       # install, don't start timers
./autodeploy/deploy.sh --help
```

On admin001 it also disables the old `exporter-autodeploy.timer`, removes
its files, and carries its state over to `/var/lib/exporter-deploy/`
(skip with `--no-migrate`). On core025 it checks that the targets
directory is writable and that `scontrol` works (locally, or via
`--slurm-ssh`, which is saved as a systemd drop-in). Re-running the
script is safe; use it to roll out updated scripts too.

### Manual

On **both** hosts, copy the scripts to `/opt/exporter-autodeploy`
(`exporter_common.py` must sit next to the script that imports it):

```bash
mkdir -p /opt/exporter-autodeploy
cp exporter_common.py deploy_exporters.py sync_prometheus_targets.py /opt/exporter-autodeploy/
chmod +x /opt/exporter-autodeploy/deploy_exporters.py /opt/exporter-autodeploy/sync_prometheus_targets.py
```

**admin001:**

```bash
cp exporter-deploy.service exporter-deploy.timer /etc/systemd/system/
systemctl daemon-reload
/opt/exporter-autodeploy/deploy_exporters.py --dry-run
systemctl enable --now exporter-deploy.timer
```

**core025:**

```bash
cp prometheus-targets-sync.service prometheus-targets-sync.timer /etc/systemd/system/
systemctl daemon-reload
/opt/exporter-autodeploy/sync_prometheus_targets.py --dry-run
systemctl enable --now prometheus-targets-sync.timer
```

If core025 has no Slurm client, add the ssh option to the unit:

```bash
systemctl edit prometheus-targets-sync.service
# [Service]
# ExecStart=
# ExecStart=/opt/exporter-autodeploy/sync_prometheus_targets.py --slurm-ssh root@<login-node>
```

### Migrating from the old combined `autodeploy_exporters.py`

The old single service ran on admin001 and edited the targets files on
vcore001 over ssh. To switch:

1. On admin001: `systemctl disable --now exporter-autodeploy.timer`, then
   remove `exporter-autodeploy.{service,timer}` and
   `/usr/local/bin/autodeploy_exporters.py`.
2. Optionally keep deployment history:
   `cp /var/lib/exporter-autodeploy/state.json /var/lib/exporter-deploy/state.json`
   (same format). Without it, the first run rebuilds the state by probing
   exporter ports.
3. Copy the current targets files from vcore001 to
   `/db/prometheus/targets` on core025 before enabling the sync timer. The
   first sync run marks every listed Slurm node as managed.

## Usage

```bash
# admin001
deploy_exporters.py --dry-run                # show what would be deployed
deploy_exporters.py --exclude '^testnode'    # skip nodes by regex
deploy_exporters.py --feature rocky8 --feature rocky9   # allowed OS features
deploy_exporters.py --max-load-ratio 2.0     # allow busier nodes
systemctl start exporter-deploy.service; journalctl -u exporter-deploy.service

# core025
sync_prometheus_targets.py --dry-run         # show adds/removes, change nothing
sync_prometheus_targets.py --no-remove       # add only
sync_prometheus_targets.py --force           # allow > --max-remove removals (decommission)
sync_prometheus_targets.py --remove-after 1  # remove as soon as a node leaves Slurm
systemctl start prometheus-targets-sync.service; journalctl -u prometheus-targets-sync.service
```

(Scripts live in `/opt/exporter-autodeploy/`.)

## Notes

- To force a re-deploy on a node (e.g. after changing a salt state), stop
  its exporter or delete it from `/var/lib/exporter-deploy/state.json`
  **and** make sure its port no longer answers. Otherwise, apply salt
  directly with clush — the automation only handles first-time deployment.
- A node that loses its GPU GRES but stays in Slurm is not removed from
  `gpu_targets.yml`; only nodes that leave Slurm entirely are removed.
- `--exclude` on core025 means: never add matching nodes, and never
  remove their existing entries.
- GPU detection relies on `Gres=gpu:...` in slurm.conf. A new GPU node
  added before its GRES is configured is treated as CPU-only until a later
  run.
