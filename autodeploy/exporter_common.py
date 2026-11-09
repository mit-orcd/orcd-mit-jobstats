"""
Shared helpers for the jobstats exporter automation (ORCD MIT).

Used by:
  deploy_exporters.py         - admin node (admin001): deploys exporters
  sync_prometheus_targets.py  - Prometheus server (core025): keeps the
                                file_sd target files in sync with Slurm

Python 3.6+ stdlib only.
"""

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# exporter name -> (targets file, exporter port, salt state, gpu_only)
# gpu_only exporters are added/deployed only to nodes with gpu GRES in Slurm.
EXPORTERS = {
    "cgroup": ("cgroup_targets.yml", 9306, "engaging.role.cgroup-exporter", False),
    "gpu":    ("gpu_targets.yml",    9445, "engaging.role.nvidia-gpu-exporter", True),
    "node":   ("node_targets.yml",   9100, "engaging.role.node-exporter", False),
}

# Suffix appended to the Slurm node name to build the scrape address.
NODE_SUFFIX = ".inband"

# Only nodes with at least one of these Slurm node features (`sinfo -o %f`)
# get exporters deployed and appear in the Prometheus target files; nodes
# tagged e.g. centos7 are skipped by deployment and removed from targets.
# Both scripts accept --feature (repeatable) to override.
REQUIRED_FEATURES = ["rocky8"]

# Slurm node states (scontrol State=, '+'-separated flags) that mean
# "don't touch this node now".
UNAVAILABLE_STATE_RE = re.compile(
    r"down|drain|fail|maint|not_responding|power|reboot|future|unknown|"
    r"invalid|cloud|resume", re.I)

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

log = logging.getLogger("exporter-automation")

# scontrol -o output is "Key=value Key=value ..."; some values (OS=,
# Reason=) contain spaces, so a value runs until the next " Key=".
_SCONTROL_KV_RE = re.compile(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)")
_GPU_GRES_RE = re.compile(r"(^|,)gpu(:|\(|$)")


def run(cmd, input_text=None, timeout=None):
    """subprocess.run with captured text output (Python 3.6 compatible)."""
    return subprocess.run(cmd, input=input_text, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True,
                          timeout=timeout)


def quote_cmd(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


def get_slurm_nodes(exclude_re=None, ssh_host=None):
    """Return (all_nodes, gpu_nodes, states, features) for every node
    defined in Slurm.

    Uses `scontrol -o show node`, which lists all configured nodes,
    including nodes not in any partition (sinfo would miss those).
    all_nodes/gpu_nodes are sorted lists; states maps node -> set of
    state flags (e.g. {'IDLE', 'DRAIN'}); features maps node -> set of
    available node features (same as `sinfo -o %f`, e.g. {'rocky8'}).
    If ssh_host is set, scontrol runs there over ssh (for hosts without
    a Slurm client).

    Raises RuntimeError if Slurm cannot be queried.
    """
    cmd = ["scontrol", "-o", "show", "node"]
    if ssh_host:
        cmd = ["ssh", "-oConnectTimeout=10", "-oBatchMode=yes", ssh_host,
               quote_cmd(cmd)]
    try:
        result = run(cmd, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("cannot run %s: %s" % (quote_cmd(cmd), e))
    if result.returncode != 0:
        raise RuntimeError("%s failed: %s" % (
            quote_cmd(cmd), result.stderr.strip() or "exit %d" % result.returncode))

    nodes, gpu_nodes, states, features = set(), set(), {}, {}
    for line in result.stdout.splitlines():
        fields = dict(_SCONTROL_KV_RE.findall(line.strip()))
        name = fields.get("NodeName")
        if not name:
            continue
        nodes.add(name)
        states[name] = {s for s in fields.get("State", "").split("+") if s}
        features[name] = {f for f in fields.get("AvailableFeatures", "").split(",")
                          if f and f != "(null)"}
        if _GPU_GRES_RE.search(fields.get("Gres", "")):
            gpu_nodes.add(name)

    if exclude_re:
        pattern = re.compile(exclude_re)
        excluded = {n for n in nodes if pattern.search(n)}
        if excluded:
            log.info("excluding %d node(s) matching %r: %s",
                     len(excluded), exclude_re, ",".join(sorted(excluded)))
        nodes -= excluded
        gpu_nodes -= excluded
    return sorted(nodes), sorted(gpu_nodes), states, features


def split_by_features(nodes, node_features, required):
    """Return (with_feature, without_feature) lists, keeping node order.
    A node qualifies if it has any of the required features."""
    required = set(required)
    keep, skip = [], []
    for n in nodes:
        (keep if node_features.get(n, set()) & required else skip).append(n)
    return keep, skip


def slurm_state_ok(node_states):
    """True if none of the node's Slurm state flags indicate unavailability."""
    if not node_states:
        return False
    return not any(UNAVAILABLE_STATE_RE.search(s) or s.endswith("*")
                   for s in node_states)


def target_address(node, port):
    return "%s%s:%d" % (node, NODE_SUFFIX, port)


def exporter_responds(node, port, timeout=5):
    """True if http://<node><suffix>:<port>/metrics answers with HTTP 200."""
    url = "http://%s/metrics" % target_address(node, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def probe_exporters(nodes, port, workers=32, timeout=5):
    """Return the subset of nodes whose exporter on port responds."""
    nodes = list(nodes)
    if not nodes:
        return set()
    with ThreadPoolExecutor(max_workers=min(workers, len(nodes))) as pool:
        results = pool.map(lambda n: exporter_responds(n, port, timeout), nodes)
    return {n for n, ok in zip(nodes, results) if ok}


def load_json(path, default):
    if not os.path.exists(path):
        return default, False
    with open(path) as f:
        return json.load(f), True


def atomic_write(path, content, preserve_from=None):
    """Write content to path atomically (temp file in the same directory +
    rename), copying mode/ownership from preserve_from (default: path)
    if it exists, so readers like Prometheus file_sd never see a partial file.
    """
    preserve_from = preserve_from or path
    dirname = os.path.dirname(path) or "."
    os.makedirs(dirname, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirname, prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        if os.path.exists(preserve_from):
            st = os.stat(preserve_from)
            os.chmod(tmp, st.st_mode)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_json(path, data):
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
