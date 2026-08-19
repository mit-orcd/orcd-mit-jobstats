# Exporter auto-deployment for new Slurm nodes

Automation that keeps the Prometheus targets in sync with the Slurm cluster
and deploys the jobstats exporters (cgroup, node, nvidia-gpu) to newly added
nodes via salt.

Runs on an **admin node** (e.g. `admin001`) on a systemd timer (4x daily by
default). The admin node is the host with the security settings required to
push deployments to compute nodes (clush/ssh + salt); the Prometheus server
(e.g. `vcore001`) does not have that access, so it only holds the file_sd
target files, which this automation updates remotely over ssh
(`root@vcore001` by default, see `PROM_HOST` / `--prom-host`).

## Architecture

```
admin001 (this automation)                 vcore001 (Prometheus)
  - sinfo: discover new Slurm nodes          - /db/prometheus/targets/
  - clush/ssh + salt-call: deploy       ssh      cgroup_targets.yml
    exporters to compute nodes         ----->    gpu_targets.yml
  - state: /var/lib/exporter-autodeploy/         node_targets.yml
```

## What it does on each run

0. **Preflight**: verifies the Prometheus server (`--prom-host`) is
   reachable via ssh and that `/db/prometheus/targets` there exists and is
   writable. If not, the run aborts before touching anything (in
   `--dry-run` this is reported as a warning only).
1. Queries Slurm (locally) for the current node list and GPU info
   (`sinfo -h -N -o '%N|%G|%t'`). A node counts as a GPU node if its GRES
   contains a `gpu:` entry.
2. Runs a readiness check on every node pending deployment:
   - **Slurm state** must be healthy — nodes that are down, drained,
     draining, failed, in maintenance, powered off, rebooting, or not
     responding (`*`/`~`/`#`/... state flags) are skipped;
   - **ssh reachability** — the node must answer via clush/ssh;
   - **OS** — `/etc/os-release` must report RHEL or Rocky with major
     version >= 8. Nodes on anything older (e.g. CentOS/RHEL 7) are
     excluded entirely: no targets, no deployment. They are re-checked
     each run, so a node is picked up automatically after an OS
     reinstall/upgrade;
   - **load** — the 1-min load average must not exceed
     `CPUs * --max-load-ratio` (default 1.0), so heavily utilized nodes
     are not disturbed.

   Nodes failing a transient check (state/ssh/load) are skipped with a
   warning and automatically re-checked on the next timer run.
3. For each targets file in `/db/prometheus/targets/` **on the Prometheus
   server** (read and written over ssh):
   - `cgroup_targets.yml` (port 9306) — all nodes
   - `gpu_targets.yml` (port 9445) — GPU nodes only
   - `node_targets.yml` (port 9100) — all nodes

   appends ready nodes not already present as `'<node>.inband:<port>'`,
   preserving the existing file format, mode, and ownership.
   Unsupported-OS nodes are never added, so they don't become
   permanently-down scrape targets. Remote writes are atomic (temp file +
   `mv` in the same directory), so Prometheus file_sd never sees a partial
   file (no Prometheus reload needed with file_sd).
4. Deploys the exporters **from the admin node** to ready nodes that
   haven't been deployed to yet (cgroup + node exporters on every new
   node, nvidia-gpu exporter only on new GPU nodes):

   ```
   clush -o "-oConnectTimeout=3" -w <new_nodes> 'salt-call state.apply engaging.role.cgroup-exporter'
   clush -o "-oConnectTimeout=3" -w <new_nodes> 'salt-call state.apply engaging.role.node-exporter'
   clush -o "-oConnectTimeout=3" -w <new_nodes> 'salt-call state.apply engaging.role.nvidia-gpu-exporter'
   ```

Deployment success is tracked per exporter in
`/var/lib/exporter-autodeploy/state.json` (local to the admin node). If a
clush/salt run fails (e.g. a node is down), the affected nodes stay pending
and deployment is retried on the next timer run — salt states are
idempotent, so re-applying is safe.

On the very first run (no state file yet), nodes already listed in the
targets files are assumed to be deployed already, so salt is not re-applied
to the whole cluster.

## Installation (on the admin node, e.g. admin001)

Requirements on the admin node:

- python3 (stdlib only)
- `sinfo` (slurm client) working locally
- `clush` with passwordless ssh/root access to the compute nodes, and
  `salt-call` usable on the compute nodes
- passwordless ssh as `root@vcore001` (see below), with write access to
  `/db/prometheus/targets` there

Set up the ssh key to the Prometheus server (once, as root on admin001):

```bash
# Generate a dedicated key if root has none yet:
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ''
ssh-copy-id root@vcore001          # or add the pubkey to vcore001:/root/.ssh/authorized_keys

# Verify - both must succeed non-interactively:
ssh -oBatchMode=yes root@vcore001 true
ssh -oBatchMode=yes root@vcore001 'test -d /db/prometheus/targets -a -w /db/prometheus/targets && echo OK'
```

Then install the automation:

```bash
cp autodeploy_exporters.py /usr/local/bin/
chmod +x /usr/local/bin/autodeploy_exporters.py
cp exporter-autodeploy.service exporter-autodeploy.timer /etc/systemd/system/
systemctl daemon-reload

# Confirm the setup end to end (includes the preflight check against
# vcore001) without changing anything:
/usr/local/bin/autodeploy_exporters.py --dry-run

systemctl enable --now exporter-autodeploy.timer
```

If the automation previously ran on the Prometheus server itself, disable
it there (`systemctl disable --now exporter-autodeploy.timer`) and either
copy `/var/lib/exporter-autodeploy/state.json` to the admin node or just
let the first run bootstrap the state from the existing targets files.

## Usage

```bash
# See what would happen, change nothing:
autodeploy_exporters.py --dry-run

# Only update target files, skip salt deployment:
autodeploy_exporters.py --no-deploy

# Different Prometheus server / ssh user:
autodeploy_exporters.py --prom-host promdeploy@vcore002

# Old single-server layout (targets dir local to this host):
autodeploy_exporters.py --prom-host ''

# Exclude nodes by regex (e.g. test partition nodes):
autodeploy_exporters.py --exclude '^testnode'

# Allow deployment to busier nodes (skip only if load1 > 2x CPU count):
autodeploy_exporters.py --max-load-ratio 2.0

# Run immediately via systemd:
systemctl start exporter-autodeploy.service
journalctl -u exporter-autodeploy.service
```

Configuration defaults (Prometheus host, targets dir, node suffix, ports,
salt states, state file path) live at the top of `autodeploy_exporters.py`
and can be overridden with `--prom-host` / `--targets-dir` /
`--state-file`.

## Notes

- The script never removes targets; decommissioned nodes must be cleaned
  out of the yml files on vcore001 (and the state file on admin001)
  manually.
- ssh to the Prometheus server uses `-oBatchMode=yes`, so a missing or
  rejected key fails fast with a clear error instead of hanging on a
  password prompt.
- The allowed OS list and minimum major version are the `ALLOWED_OS_IDS`
  and `MIN_OS_MAJOR` constants at the top of the script (default:
  rhel/rocky, version 8).
- GPU detection relies on Slurm GRES being configured for the node. If a
  brand-new GPU node is added before its `Gres=gpu:...` line is set in
  slurm.conf, it will initially be treated as CPU-only; the gpu exporter
  is picked up automatically on a later run once GRES is configured.
