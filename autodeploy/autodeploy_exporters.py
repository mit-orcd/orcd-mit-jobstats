#!/usr/bin/env python3
"""
Automatic deployment of jobstats exporters to new Slurm nodes (ORCD MIT).

Intended to run on an admin node (e.g. admin001) that has ssh/salt
access to the compute nodes (e.g. via systemd timer, a few times
daily). The Prometheus file_sd target files live on the Prometheus
server (e.g. vcore001) and are updated remotely over ssh, since the
Prometheus server itself does not have the security settings to push
deployments to compute nodes. On each run the script:

  0. Verifies that the Prometheus server is reachable via ssh and that
     the targets directory there exists and is writable - otherwise it
     aborts before touching anything.
  1. Discovers all nodes currently known to Slurm (`sinfo`, run
     locally), including whether each node has GPUs (gpu GRES).
  2. Checks that each new (not yet deployed) node is ready for
     deployment:
       - Slurm state is healthy (not down/drained/maintenance/not
         responding) - otherwise retried next run;
       - reachable via ssh and load not too high - otherwise retried
         next run;
       - runs a supported OS: RHEL or Rocky, major version >= 8.
         Nodes on older/other OSes are excluded entirely (no targets,
         no deployment) until they are reinstalled/upgraded.
  3. Appends ready nodes missing from the Prometheus file_sd target
     files (cgroup_targets.yml, gpu_targets.yml, node_targets.yml) in
     TARGETS_DIR on the Prometheus server (--prom-host, over ssh)
     using the '<node><suffix>:<port>' convention. GPU nodes go into
     all three files; CPU-only nodes are added to cgroup and node
     targets only. Files are rewritten atomically on the remote side
     and existing content/format/ownership is preserved.
  4. Deploys the exporters (all three on GPU nodes, cgroup + node on
     CPU-only nodes) from this host to ready nodes that have not yet
     been successfully deployed, via:
         clush -o "-oConnectTimeout=3" -w <nodes> \
             'salt-call state.apply engaging.role.<exporter>'
     Deployment success is tracked per exporter in a local state file,
     so a failed salt run is retried automatically on the next timer
     run (salt states are idempotent).

Run with --dry-run to see what would change without touching anything.
Run with --prom-host '' if the targets directory is local (old
single-server layout, everything on the Prometheus server).
"""

import argparse
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# ssh destination (user@host) of the Prometheus server that holds the
# file_sd target files. Empty string means the targets dir is local to
# the host running this script.
PROM_HOST = "root@vcore001"

# Prometheus file_sd targets directory (on PROM_HOST).
TARGETS_DIR = "/db/prometheus/targets"

# ssh options used when talking to PROM_HOST. BatchMode makes a missing
# or broken ssh key fail immediately instead of prompting for a password.
PROM_SSH_OPTS = ["-oConnectTimeout=5", "-oBatchMode=yes"]

# Suffix appended to the Slurm node name to build the scrape address.
NODE_SUFFIX = ".inband"

# exporter name -> (targets file, exporter port, salt state, gpu_only)
# gpu_only exporters are added/deployed only to nodes with gpu GRES in Slurm.
EXPORTERS = {
    "cgroup": ("cgroup_targets.yml", 9306, "engaging.role.cgroup-exporter", False),
    "gpu":    ("gpu_targets.yml",    9445, "engaging.role.nvidia-gpu-exporter", True),
    "node":   ("node_targets.yml",   9100, "engaging.role.node-exporter", False),
}

# Tracks which nodes each exporter has been successfully deployed to
# (local file on the host running this script).
STATE_FILE = "/var/lib/exporter-autodeploy/state.json"

CLUSH_OPTS = ["-o", "-oConnectTimeout=3"]

# Skip deployment to a node if its 1-min load average exceeds
# (number of CPUs * MAX_LOAD_RATIO). Overridable with --max-load-ratio.
MAX_LOAD_RATIO = 1.0

# Only deploy to these OSes (/etc/os-release ID) at or above this
# major version. Anything else is excluded from targets and deployment.
ALLOWED_OS_IDS = {"rhel", "rocky"}
MIN_OS_MAJOR = 8

# Slurm node states (sinfo %t) that mean "don't touch this node now".
UNAVAILABLE_STATE_RE = re.compile(
    r"down|drain|drng|fail|maint|unk|boot|futr|power|resume", re.I)
# sinfo state flags: * not responding, ~ powered off, # powering up,
# % powering down, $ maint reservation, @ reboot pending, ! / + misc.
STATE_FLAGS = "*~#%$@!+"

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

# --------------------------------------------------------------------------

log = logging.getLogger("exporter-autodeploy")

TARGET_LINE_RE = re.compile(r"^(\s*-\s*)['\"]?([^'\":]+):(\d+)['\"]?\s*$")
DEFAULT_INDENT = "     - "

# Exit code used on the remote side to signal "file does not exist"
# (distinguishable from cat/permission failures and from ssh's own 255).
ABSENT_RC = 42


def prom_ssh(host, command, input_text=None, timeout=60):
    """Run a shell command on the Prometheus server via ssh."""
    cmd = ["ssh", *PROM_SSH_OPTS, host, command]
    return subprocess.run(cmd, input=input_text, capture_output=True,
                          text=True, timeout=timeout)


def check_prom_host(host, targets_dir):
    """Verify the Prometheus server can be updated: ssh works and the
    targets directory exists and is writable. Returns an error string,
    or None if everything is fine.
    """
    qdir = shlex.quote(targets_dir)
    try:
        result = prom_ssh(host, "true", timeout=30)
    except subprocess.TimeoutExpired:
        return "ssh to %s timed out" % host
    if result.returncode != 0:
        return "cannot ssh to %s: %s" % (
            host, result.stderr.strip() or "exit code %d" % result.returncode)
    try:
        result = prom_ssh(
            host, "test -d %s && test -w %s" % (qdir, qdir), timeout=30)
    except subprocess.TimeoutExpired:
        return "checking %s on %s timed out" % (targets_dir, host)
    if result.returncode != 0:
        return "targets directory %s on %s is missing or not writable" % (
            targets_dir, host)
    return None


def get_slurm_nodes(exclude_re=None):
    """Return (all_nodes, gpu_nodes, states).

    all_nodes/gpu_nodes are sorted lists; states maps node -> set of
    sinfo state strings (a node may appear in several partitions).
    A node is a GPU node if any of its sinfo lines reports a gpu GRES
    (e.g. 'gpu:a100:4'); CPU-only nodes report '(null)' or no gpu entry.
    """
    out = subprocess.run(
        ["sinfo", "-h", "-N", "-o", "%N|%G|%t"],
        check=True, capture_output=True, text=True,
    ).stdout
    nodes, gpu_nodes, states = set(), set(), {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name, gres, state = (p.strip() for p in parts[:3])
        if not name:
            continue
        nodes.add(name)
        states.setdefault(name, set()).add(state)
        if re.search(r"(^|,)gpu:", gres):
            gpu_nodes.add(name)
    if exclude_re:
        pattern = re.compile(exclude_re)
        excluded = {n for n in nodes if pattern.search(n)}
        if excluded:
            log.info("excluding %d node(s) matching %r: %s",
                     len(excluded), exclude_re, ",".join(sorted(excluded)))
        nodes -= excluded
        gpu_nodes -= excluded
    return sorted(nodes), sorted(gpu_nodes), states


def slurm_state_ok(node_states):
    """True if none of the node's Slurm states indicate unavailability."""
    for state in node_states:
        if state.rstrip(STATE_FLAGS) != state:  # flagged: not responding etc.
            return False
        if UNAVAILABLE_STATE_RE.search(state):
            return False
    return True


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
      'ready'       - ok to add targets and deploy now
      'retry'       - temporarily unavailable, re-check next run
      'unsupported' - OS below RHEL/Rocky 8, exclude from targets/deploy
    """
    if not slurm_state_ok(node_states):
        return "retry", "slurm state %s" % ",".join(sorted(node_states))
    if dry_run:
        return "ready", "slurm state ok (ssh/os/load check skipped in dry-run)"
    cmd = ["clush", *CLUSH_OPTS, "-N", "-w", node,
           "cat /etc/os-release && echo ___ && cat /proc/loadavg && nproc"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "retry", "ssh check timed out"
    if result.returncode != 0:
        return "retry", "unreachable via ssh"
    os_part, sep, load_part = result.stdout.partition("___")
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


def read_targets_file(host, path):
    """Return the lines of a targets yml file (local, or on the
    Prometheus server when host is set), or None if the file does not
    exist. Raises OSError on any other failure.
    """
    if not host:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.readlines()
    qpath = shlex.quote(path)
    try:
        result = prom_ssh(
            host, "if [ -e %s ]; then cat %s; else exit %d; fi"
            % (qpath, qpath, ABSENT_RC))
    except subprocess.TimeoutExpired:
        raise OSError("reading %s on %s timed out" % (path, host))
    if result.returncode == ABSENT_RC:
        return None
    if result.returncode != 0:
        raise OSError("cannot read %s on %s: %s" % (
            path, host, result.stderr.strip() or "exit %d" % result.returncode))
    return result.stdout.splitlines(keepends=True)


def parse_target_hosts(lines):
    """Return the bare node names (suffix stripped) present in the lines."""
    hosts = set()
    for line in lines:
        m = TARGET_LINE_RE.match(line)
        if m:
            host = m.group(2)
            if NODE_SUFFIX and host.endswith(NODE_SUFFIX):
                host = host[: -len(NODE_SUFFIX)]
            hosts.add(host)
    return hosts


def write_targets_file_local(path, content):
    """Atomic local write, preserving mode/ownership of an existing file."""
    dirname = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dirname, prefix=os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        if os.path.exists(path):
            st = os.stat(path)
            os.chmod(tmp, st.st_mode)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_targets_file_remote(host, path, content):
    """Atomic write on the Prometheus server: upload to a temp file in
    the same directory, copy mode/ownership from the existing file, then
    mv into place so Prometheus file_sd never sees a partial file.
    """
    qpath = shlex.quote(path)
    qtemplate = shlex.quote(os.path.join(
        os.path.dirname(path) or ".",
        "." + os.path.basename(path) + ".XXXXXX"))
    remote_cmd = (
        'set -e; '
        'tmp=$(mktemp %(template)s); '
        'trap \'rm -f "$tmp"\' EXIT; '
        'cat > "$tmp"; '
        'if [ -e %(path)s ]; then '
        'chmod --reference=%(path)s "$tmp"; '
        'chown --reference=%(path)s "$tmp" 2>/dev/null || true; '
        'fi; '
        'mv -f "$tmp" %(path)s; '
        'trap - EXIT'
        % {"template": qtemplate, "path": qpath})
    try:
        result = prom_ssh(host, remote_cmd, input_text=content)
    except subprocess.TimeoutExpired:
        raise OSError("writing %s on %s timed out" % (path, host))
    if result.returncode != 0:
        raise OSError("cannot write %s on %s: %s" % (
            path, host, result.stderr.strip() or "exit %d" % result.returncode))


def append_targets(host, path, lines, new_nodes, port, dry_run):
    """Append new target entries after the last existing target line."""
    # Match indentation of existing entries if possible.
    indent = DEFAULT_INDENT
    last_target_idx = None
    for i, line in enumerate(lines):
        m = TARGET_LINE_RE.match(line)
        if m:
            indent = m.group(1)
            last_target_idx = i

    new_lines = [f"{indent}'{node}{NODE_SUFFIX}:{port}'\n" for node in new_nodes]
    if last_target_idx is not None:
        updated = lines[: last_target_idx + 1] + new_lines + lines[last_target_idx + 1:]
    else:
        updated = lines + new_lines

    where = "%s:%s" % (host, path) if host else path
    if dry_run:
        for nl in new_lines:
            log.info("[dry-run] %s: would append %s", where, nl.strip())
        return

    content = "".join(updated)
    if host:
        write_targets_file_remote(host, path, content)
    else:
        write_targets_file_local(path, content)
    for nl in new_lines:
        log.info("%s: appended %s", where, nl.strip())


def load_state(path):
    """Return (state, existed). state maps exporter name -> deployed nodes."""
    if not os.path.exists(path):
        return {name: [] for name in EXPORTERS}, False
    with open(path) as f:
        state = json.load(f)
    for name in EXPORTERS:
        state.setdefault(name, [])
    return state, True


def save_state(path, state, dry_run):
    if dry_run:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="state.")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def deploy_exporter(salt_state, nodes, dry_run):
    """Apply a salt state to nodes via clush. Returns True on success."""
    nodelist = ",".join(nodes)
    cmd = ["clush", *CLUSH_OPTS, "-w", nodelist,
           f"salt-call state.apply {salt_state}"]
    if dry_run:
        log.info("[dry-run] would run: %s", shlex.join(cmd))
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
        description="Auto-deploy jobstats exporters to new Slurm nodes.")
    parser.add_argument("--prom-host", default=PROM_HOST,
                        help="ssh destination (user@host) of the Prometheus "
                             "server holding the targets dir; '' = targets "
                             f"dir is local (default: {PROM_HOST})")
    parser.add_argument("--targets-dir", default=TARGETS_DIR,
                        help="Prometheus targets directory on --prom-host "
                             f"(default: {TARGETS_DIR})")
    parser.add_argument("--state-file", default=STATE_FILE,
                        help=f"deployment state file (default: {STATE_FILE})")
    parser.add_argument("--exclude", metavar="REGEX", default=None,
                        help="exclude nodes whose name matches this regex")
    parser.add_argument("--max-load-ratio", type=float, default=MAX_LOAD_RATIO,
                        help="skip deployment if load1 > CPUs * ratio "
                             f"(default: {MAX_LOAD_RATIO})")
    parser.add_argument("--no-deploy", action="store_true",
                        help="only update target files, skip salt deployment")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be done without changing anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)

    prom_host = args.prom_host.strip()

    # Preflight: make sure the Prometheus server can actually be updated
    # before discovering/deploying anything. Read-only, so it also runs
    # in dry-run mode (as a warning only there).
    if prom_host:
        error = check_prom_host(prom_host, args.targets_dir)
        if error:
            if args.dry_run:
                log.warning("[dry-run] Prometheus server check failed: %s "
                            "(a real run would abort here)", error)
            else:
                log.error("Prometheus server check failed: %s", error)
                return 1
        else:
            log.info("Prometheus server %s reachable, %s writable",
                     prom_host, args.targets_dir)
    else:
        log.info("--prom-host empty: using local targets dir %s",
                 args.targets_dir)

    try:
        slurm_nodes, gpu_nodes, node_states = get_slurm_nodes(args.exclude)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.error("failed to query Slurm nodes: %s", e)
        return 1
    if not slurm_nodes:
        log.error("Slurm returned no nodes; refusing to continue")
        return 1
    log.info("Slurm reports %d node(s), %d with GPUs",
             len(slurm_nodes), len(gpu_nodes))

    state, state_existed = load_state(args.state_file)
    exit_code = 0

    def eligible_nodes(gpu_only):
        return gpu_nodes if gpu_only else slurm_nodes

    # Parse the target files up front; also bootstrap deployment state
    # from them on the very first run.
    target_files = {}
    for name, (filename, _port, _salt, _gpu_only) in EXPORTERS.items():
        path = os.path.join(args.targets_dir, filename)
        try:
            lines = read_targets_file(prom_host, path)
        except OSError as e:
            log.error("cannot read %s: %s", path, e)
            return 1
        if lines is None:
            lines = ["- targets:\n"]
        existing = parse_target_hosts(lines)
        target_files[name] = (path, lines, existing)
        if not state_existed:
            # First run: nodes already present in the targets file are
            # assumed deployed, so we don't re-apply salt to the whole
            # cluster on bootstrap.
            state[name] = sorted(existing)
            log.info("%s exporter: bootstrapped state with %d already-known node(s)",
                     name, len(existing))

    # Step 1: readiness check for all nodes pending deployment
    # (available in Slurm, reachable via ssh, supported OS, load not
    # too high). Temporarily unavailable nodes are re-checked next run;
    # nodes on an unsupported OS (< RHEL/Rocky 8) are excluded from
    # both targets and deployment.
    pending_union = set()
    for name, (_f, _p, _s, gpu_only) in EXPORTERS.items():
        deployed = set(state[name])
        pending_union |= {n for n in eligible_nodes(gpu_only)
                          if n not in deployed}

    ready = set()
    for node in sorted(pending_union):
        status, reason = check_node_ready(
            node, node_states.get(node, set()),
            args.max_load_ratio, args.dry_run)
        if status == "ready":
            log.info("%s: ready for deployment - %s", node, reason)
            ready.add(node)
        elif status == "unsupported":
            log.warning("%s: excluded - %s", node, reason)
        else:
            log.warning("%s: skipping this run (%s); will retry next run",
                        node, reason)

    # Step 2: bring the Prometheus target files up to date. Only nodes
    # that are ready now (or were already deployed earlier) are added,
    # so unsupported-OS nodes never become dead scrape targets.
    for name, (filename, port, _salt, gpu_only) in EXPORTERS.items():
        path, lines, existing = target_files[name]
        deployed = set(state[name])
        missing = [n for n in eligible_nodes(gpu_only)
                   if n not in existing and (n in ready or n in deployed)]
        if not missing:
            log.info("%s: up to date (%d targets)", filename, len(existing))
            continue
        try:
            append_targets(prom_host, path, lines, missing, port, args.dry_run)
        except OSError as e:
            log.error("cannot update %s: %s", path, e)
            exit_code = 1

    # Step 3: deploy exporters to ready nodes not yet deployed.
    if not args.no_deploy:
        for name, (_filename, _port, salt_state, gpu_only) in EXPORTERS.items():
            deployed = set(state[name])
            pending = [n for n in eligible_nodes(gpu_only)
                       if n not in deployed and n in ready]
            if not pending:
                log.info("%s exporter: nothing to deploy", name)
                continue
            if deploy_exporter(salt_state, pending, args.dry_run):
                state[name] = sorted(deployed | set(pending))
            else:
                exit_code = 1

    try:
        save_state(args.state_file, state, args.dry_run)
    except OSError as e:
        log.error("cannot write state file %s: %s", args.state_file, e)
        exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
