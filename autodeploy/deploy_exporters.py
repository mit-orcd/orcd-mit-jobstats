#!/usr/bin/env python3
"""
Deploy jobstats exporters to new Slurm nodes (ORCD MIT) - admin node part.

Runs on the admin node (admin001), which has clush/ssh + salt access to
the compute nodes, via a systemd timer. It does NOT touch the Prometheus
target files; those are maintained on the Prometheus server (core025) by
sync_prometheus_targets.py, which adds a node once its exporter answers.

On each run:
  1. Lists all nodes defined in Slurm (`scontrol -o show node`) and which
     of them have GPUs (gpu GRES).
  2. Forgets nodes that are no longer in Slurm from the deployment state,
     so a node re-added later under the same name is deployed again.
  3. For each exporter (cgroup + node on all nodes, nvidia-gpu on GPU
     nodes), takes the nodes not yet marked deployed and probes the
     exporter port: nodes where it already answers are marked deployed
     without running salt (this also bootstraps the state on first run).
  4. Runs a readiness check on the remaining nodes:
       - Slurm state healthy (not down/drained/maintenance/not
         responding/powered off) - otherwise retried next run;
       - reachable via ssh and load not too high - otherwise retried
         next run;
       - OS is RHEL or Rocky >= 8 - otherwise skipped (and, since no
         exporter ever answers there, never added to Prometheus).
  5. Deploys to ready nodes:
         clush -o "-oConnectTimeout=3" -w <nodes> \
             'salt-call state.apply engaging.role.<exporter>'
     Success is tracked per exporter in the state file; failed nodes are
     retried on the next run (salt states are idempotent).

Run with --dry-run to see what would change without touching anything.
"""

import argparse
import logging
import re
import subprocess
import sys

import exporter_common as ec

STATE_FILE = "/var/lib/exporter-deploy/state.json"

CLUSH_OPTS = ["-o", "-oConnectTimeout=3"]

# Skip deployment to a node if its 1-min load average exceeds
# (number of CPUs * MAX_LOAD_RATIO). Overridable with --max-load-ratio.
MAX_LOAD_RATIO = 1.0

# Second safety net (after the Slurm feature filter, ec.REQUIRED_FEATURES), checked on the node itself: only deploy to these
# OSes (/etc/os-release ID) at or above this major version.
ALLOWED_OS_IDS = {"rhel", "rocky"}
MIN_OS_MAJOR = 8

log = ec.log


def parse_os_release(text):
    """Return (os_id, major_version) parsed from /etc/os-release content."""
    os_id, version = None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("ID="):
            os_id = line[3:].strip().strip('"\'').lower()
        elif line.startswith("VERSION_ID="):
            version = line[11:].strip().strip('"\'')
    major = None
    if version:
        m = re.match(r"(\d+)", version)
        if m:
            major = int(m.group(1))
    return os_id, major


def os_supported(os_id, major):
    return os_id in ALLOWED_OS_IDS and major is not None and major >= MIN_OS_MAJOR


def check_node_ready(node, node_states, max_load_ratio, dry_run):
    """Pre-deployment check: Slurm state, ssh reachability, OS, load.

    Returns (status, reason) where status is one of:
      'ready'       - ok to deploy now
      'retry'       - temporarily unavailable, re-check next run
      'unsupported' - OS below RHEL/Rocky 8, never deploy
    """
    if not ec.slurm_state_ok(node_states):
        return "retry", "slurm state %s" % ("+".join(sorted(node_states)) or "?")
    if dry_run:
        return "ready", "slurm state ok (ssh/os/load check skipped in dry-run)"
    cmd = ["clush"] + CLUSH_OPTS + ["-N", "-w", node,
           "cat /etc/os-release && echo ___ && cat /proc/loadavg && nproc"]
    try:
        result = ec.run(cmd, timeout=60)
    except subprocess.TimeoutExpired:
        return "retry", "ssh check timed out"
    if result.returncode != 0:
        return "retry", "unreachable via ssh"
    os_part, _sep, load_part = result.stdout.partition("___")
    lines = [l.strip() for l in load_part.splitlines() if l.strip()]
    try:
        load1 = float(lines[0].split()[0])
        ncpu = int(lines[1])
    except (IndexError, ValueError):
        return "retry", "could not parse probe output: %r" % result.stdout
    os_id, major = parse_os_release(os_part)
    if not os_supported(os_id, major):
        return "unsupported", "unsupported OS %s %s (need %s >= %d)" % (
            os_id or "?", major if major is not None else "?",
            "/".join(sorted(ALLOWED_OS_IDS)), MIN_OS_MAJOR)
    if load1 > ncpu * max_load_ratio:
        return "retry", "load too high (load1=%.1f, %d CPUs)" % (load1, ncpu)
    return "ready", "ok (%s %d, load1=%.1f, %d CPUs)" % (os_id, major, load1, ncpu)


def deploy_exporter(salt_state, nodes, dry_run):
    """Apply a salt state to nodes via clush. Returns True on success."""
    nodelist = ",".join(nodes)
    cmd = ["clush"] + CLUSH_OPTS + ["-w", nodelist,
                                    "salt-call state.apply %s" % salt_state]
    if dry_run:
        log.info("[dry-run] would run: %s", ec.quote_cmd(cmd))
        return True
    log.info("deploying %s to: %s", salt_state, nodelist)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        log.error("clush for %s exited with %d (will retry on next run)",
                  salt_state, result.returncode)
        return False
    log.info("deployed %s to %d node(s)", salt_state, len(nodes))
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Deploy jobstats exporters to new Slurm nodes (admin node).")
    parser.add_argument("--state-file", default=STATE_FILE,
                        help="deployment state file (default: %s)" % STATE_FILE)
    parser.add_argument("--exclude", metavar="REGEX", default=None,
                        help="exclude nodes whose name matches this regex")
    parser.add_argument("--feature", dest="features", action="append",
                        metavar="FEATURE", default=None,
                        help="only deploy to nodes with this Slurm feature; "
                             "repeat to allow any of several (default: %s)"
                             % ",".join(ec.REQUIRED_FEATURES))
    parser.add_argument("--max-load-ratio", type=float, default=MAX_LOAD_RATIO,
                        help="skip deployment if load1 > CPUs * ratio "
                             "(default: %s)" % MAX_LOAD_RATIO)
    parser.add_argument("--no-probe", action="store_true",
                        help="don't probe exporter ports; rely on the state "
                             "file only (salt is applied to every node not "
                             "recorded as deployed)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be done without changing anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=ec.LOG_FORMAT, stream=sys.stdout)

    try:
        slurm_nodes, gpu_nodes, node_states, node_features = ec.get_slurm_nodes(args.exclude)
    except RuntimeError as e:
        log.error("failed to query Slurm nodes: %s", e)
        return 1
    if not slurm_nodes:
        log.error("Slurm returned no nodes; refusing to continue")
        return 1
    log.info("Slurm reports %d node(s), %d with GPUs",
             len(slurm_nodes), len(gpu_nodes))
    slurm_set = set(slurm_nodes)

    required = args.features or ec.REQUIRED_FEATURES
    slurm_nodes, skipped = ec.split_by_features(slurm_nodes, node_features, required)
    if skipped:
        log.info("skipping %d node(s) without feature %s: %s", len(skipped),
                 "/".join(sorted(required)), ",".join(skipped))
    gpu_nodes, _ = ec.split_by_features(gpu_nodes, node_features, required)
    log.info("%d node(s) eligible for deployment (%d with GPUs)",
             len(slurm_nodes), len(gpu_nodes))

    try:
        state, _existed = ec.load_json(args.state_file, {})
    except (OSError, ValueError) as e:
        log.error("cannot read state file %s: %s", args.state_file, e)
        return 1
    for name in ec.EXPORTERS:
        state.setdefault(name, [])

    # Forget nodes that left Slurm.
    for name in ec.EXPORTERS:
        gone = sorted(set(state[name]) - slurm_set)
        if gone:
            log.info("%s exporter: forgetting %d node(s) no longer in Slurm: %s",
                     name, len(gone), ",".join(gone))
            state[name] = sorted(set(state[name]) & slurm_set)

    def eligible(gpu_only):
        return gpu_nodes if gpu_only else slurm_nodes

    # Nodes whose exporter already answers are deployed - no salt needed.
    pending = {}
    for name, (_file, port, _salt, gpu_only) in ec.EXPORTERS.items():
        deployed = set(state[name])
        todo = [n for n in eligible(gpu_only) if n not in deployed]
        if todo and not args.no_probe:
            running = ec.probe_exporters(todo, port)
            if running:
                log.info("%s exporter: already running on %d node(s), marking deployed",
                         name, len(running))
                state[name] = sorted(deployed | running)
                todo = [n for n in todo if n not in running]
        pending[name] = todo

    pending_union = set()
    for todo in pending.values():
        pending_union.update(todo)

    ready = set()
    for node in sorted(pending_union):
        status, reason = check_node_ready(
            node, node_states.get(node, set()), args.max_load_ratio, args.dry_run)
        if status == "ready":
            log.info("%s: ready for deployment - %s", node, reason)
            ready.add(node)
        elif status == "unsupported":
            log.warning("%s: not deploying - %s", node, reason)
        else:
            log.warning("%s: skipping this run (%s); will retry next run", node, reason)

    exit_code = 0
    for name, (_file, _port, salt_state, _gpu_only) in ec.EXPORTERS.items():
        nodes = [n for n in pending[name] if n in ready]
        if not nodes:
            log.info("%s exporter: nothing to deploy", name)
            continue
        if deploy_exporter(salt_state, nodes, args.dry_run):
            state[name] = sorted(set(state[name]) | set(nodes))
        else:
            exit_code = 1

    if args.dry_run:
        return exit_code
    try:
        ec.save_json(args.state_file, state)
    except OSError as e:
        log.error("cannot write state file %s: %s", args.state_file, e)
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
