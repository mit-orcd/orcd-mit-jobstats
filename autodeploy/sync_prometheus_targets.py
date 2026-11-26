#!/usr/bin/env python3
"""
Keep the Prometheus file_sd target files in sync with Slurm (ORCD MIT) -
Prometheus server part.

Runs locally on the Prometheus server (core025) via a systemd timer and
edits the target files in TARGETS_DIR in place:
    cgroup_targets.yml  (port 9306) - all nodes
    gpu_targets.yml     (port 9445) - GPU nodes only
    node_targets.yml    (port 9100) - all nodes

Exporters are deployed separately from the admin node (deploy_exporters.py
on admin001); this script never needs clush/salt.

On each run:
  1. Lists all nodes defined in Slurm (`scontrol -o show node`, locally or
     over ssh with --slurm-ssh), which have GPUs (gpu GRES), and their
     features. Nodes with an excluded feature (default centos7, see
     --exclude-feature) are not eligible to be targets; every other node
     is, including nodes with no features.
  2. ADD: for every eligible node missing from a targets file, probes
     http://<node>.inband:<port>/metrics and appends the node only if the
     exporter answers. Nodes where the exporter was never deployed
     therefore never become dead scrape targets.
  3. REMOVE: drops entries for nodes that are no longer defined in Slurm
     or that have an excluded feature (e.g. centos7 nodes).
     Safety rails:
       - only "managed" entries are ever removed: nodes this script has
         seen in Slurm at least once, or whose name matches --node-pattern
         (default ^node<digit>, catches nodes decommissioned before the
         first run). Other targets (e.g. infrastructure servers in
         node_targets.yml) are left alone;
       - a node must be ineligible in --remove-after consecutive runs
         (default 2) before it is removed;
       - if more than --max-remove nodes would be removed in a single run,
         no removals are done at all and the run fails, unless --force
         is given;
       - an empty or failed Slurm query aborts the run.
  Nodes that are down/drained but still defined in Slurm are kept.

Files are rewritten atomically (temp file + rename, mode/ownership kept),
so Prometheus file_sd picks up the change without a reload.

Run with --dry-run to see what would change without touching anything.
"""

import argparse
import logging
import os
import re
import sys

import exporter_common as ec

TARGETS_DIR = "/db/prometheus/targets"
STATE_FILE = "/var/lib/prometheus-targets-sync/state.json"
REMOVE_AFTER = 2
MAX_REMOVE = 20

# Target names matching this are compute nodes owned by this script even if
# it never saw them in Slurm (e.g. decommissioned before the first run), so
# they are removed when not in Slurm. Other names (infra servers) are kept.
NODE_NAME_PATTERN = r"^node\d"

TARGET_LINE_RE = re.compile(r"^(\s*-\s*)['\"]?([^'\":\s]+):(\d+)['\"]?\s*$")
DEFAULT_INDENT = "     - "

log = ec.log


def read_targets_file(path):
    """Return the file's lines, or a minimal skeleton if it does not exist."""
    if not os.path.exists(path):
        return ["- targets:\n"]
    with open(path) as f:
        return f.readlines()


def target_entries(lines):
    """Return {node: [line indices]} for target lines using NODE_SUFFIX."""
    entries = {}
    for i, line in enumerate(lines):
        m = TARGET_LINE_RE.match(line)
        if not m:
            continue
        host = m.group(2)
        if ec.NODE_SUFFIX and host.endswith(ec.NODE_SUFFIX):
            host = host[: -len(ec.NODE_SUFFIX)]
        entries.setdefault(host, []).append(i)
    return entries


def update_lines(lines, add_nodes, remove_nodes, entries, port):
    """Return new lines with remove_nodes' entries dropped and add_nodes
    appended after the last remaining target line (matching its indent)."""
    drop = set()
    for node in remove_nodes:
        drop.update(entries.get(node, []))
    kept = [l for i, l in enumerate(lines) if i not in drop]

    indent, last_idx = DEFAULT_INDENT, None
    for i, line in enumerate(kept):
        m = TARGET_LINE_RE.match(line)
        if m:
            indent, last_idx = m.group(1), i
    new_lines = ["%s'%s'\n" % (indent, ec.target_address(n, port)) for n in add_nodes]
    if last_idx is None:
        if kept and not kept[-1].endswith("\n"):
            kept[-1] += "\n"
        return kept + new_lines
    return kept[: last_idx + 1] + new_lines + kept[last_idx + 1:]


def main():
    parser = argparse.ArgumentParser(
        description="Sync Prometheus file_sd target files with Slurm nodes "
                    "(runs on the Prometheus server).")
    parser.add_argument("--targets-dir", default=TARGETS_DIR,
                        help="Prometheus targets directory (default: %s)" % TARGETS_DIR)
    parser.add_argument("--state-file", default=STATE_FILE,
                        help="sync state file (default: %s)" % STATE_FILE)
    parser.add_argument("--slurm-ssh", metavar="USER@HOST", default=None,
                        help="run scontrol on this host over ssh instead of "
                             "locally (if this server has no Slurm client)")
    parser.add_argument("--exclude", metavar="REGEX", default=None,
                        help="ignore Slurm nodes whose name matches this regex "
                             "(never added; existing entries are left alone)")
    parser.add_argument("--exclude-feature", dest="exclude_features",
                        action="append", metavar="FEATURE", default=None,
                        help="keep nodes with this Slurm feature out of the "
                             "targets; repeat to exclude several, '' to "
                             "exclude none (default: %s)"
                             % ",".join(ec.EXCLUDED_FEATURES))
    parser.add_argument("--node-pattern", metavar="REGEX", default=NODE_NAME_PATTERN,
                        help="target names matching this are treated as Slurm "
                             "compute nodes and removed when not in Slurm, even "
                             "if never seen there; '' to only remove nodes seen "
                             "in Slurm (default: %s)" % NODE_NAME_PATTERN)
    parser.add_argument("--no-probe", action="store_true",
                        help="add every Slurm node without checking that its "
                             "exporter answers")
    parser.add_argument("--no-remove", action="store_true",
                        help="only add new nodes, never remove entries")
    parser.add_argument("--remove-after", type=int, default=REMOVE_AFTER,
                        help="remove a node after it has been missing from "
                             "Slurm in this many consecutive runs "
                             "(default: %d)" % REMOVE_AFTER)
    parser.add_argument("--max-remove", type=int, default=MAX_REMOVE,
                        help="refuse to remove more than this many nodes "
                             "per run (default: %d)" % MAX_REMOVE)
    parser.add_argument("--force", action="store_true",
                        help="ignore --max-remove")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be done without changing anything")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format=ec.LOG_FORMAT, stream=sys.stdout)

    if not os.path.isdir(args.targets_dir):
        log.error("targets directory %s does not exist", args.targets_dir)
        return 1

    try:
        slurm_nodes, gpu_nodes, _states, node_features = ec.get_slurm_nodes(
            args.exclude, ssh_host=args.slurm_ssh)
    except RuntimeError as e:
        log.error("failed to query Slurm nodes: %s", e)
        return 1
    if not slurm_nodes:
        log.error("Slurm returned no nodes; refusing to continue")
        return 1
    log.info("Slurm reports %d node(s), %d with GPUs", len(slurm_nodes), len(gpu_nodes))
    slurm_set = set(slurm_nodes)
    excluded_re = re.compile(args.exclude) if args.exclude else None

    excluded_features = ec.excluded_features(args.exclude_features)
    feature_label = "/".join(sorted(excluded_features))
    slurm_nodes, by_feature = ec.split_by_features(slurm_nodes, node_features,
                                                   excluded_features)
    gpu_nodes, _ = ec.split_by_features(gpu_nodes, node_features, excluded_features)
    if not slurm_nodes:
        log.error("every Slurm node has feature %s (check `sinfo -o \"%%N %%f\"` "
                  "or --exclude-feature); refusing to continue", feature_label)
        return 1
    if by_feature:
        log.info("%d node(s) with feature %s are not kept as targets: %s",
                 len(by_feature), feature_label, ",".join(by_feature))
    eligible_set = set(slurm_nodes)

    try:
        state, _existed = ec.load_json(args.state_file, {})
    except (OSError, ValueError) as e:
        log.error("cannot read state file %s: %s", args.state_file, e)
        return 1
    managed = set(state.get("managed", []))
    missing_runs = dict(state.get("missing_runs", {}))

    node_name_re = re.compile(args.node_pattern) if args.node_pattern else None

    files = {}
    for name, (filename, port, _salt, gpu_only) in ec.EXPORTERS.items():
        path = os.path.join(args.targets_dir, filename)
        try:
            lines = read_targets_file(path)
        except OSError as e:
            log.error("cannot read %s: %s", path, e)
            return 1
        entries = target_entries(lines)
        files[name] = (path, port, gpu_only, lines, entries)
        managed |= set(entries) & slurm_set
        if node_name_re:
            managed |= {e for e in entries if node_name_re.search(e)}
        unknown = sorted(set(entries) - slurm_set - managed)
        if unknown:
            log.info("%s: leaving %d entr%s untouched (not a Slurm node, not "
                     "matching --node-pattern %r): %s", filename,
                     len(unknown), "y" if len(unknown) == 1 else "ies",
                     args.node_pattern, ",".join(unknown[:20]) +
                     (",..." if len(unknown) > 20 else ""))

    present_anywhere = set()
    for (_p, _port, _g, _l, entries) in files.values():
        present_anywhere |= set(entries)

    def why_gone(node):
        if node in slurm_set:
            return "has Slurm feature %s" % feature_label
        return "no longer in Slurm"

    # Count consecutive runs each managed, still-listed node has been
    # ineligible (left Slurm, or has an excluded feature).
    gone = (managed - eligible_set) & present_anywhere
    if excluded_re:
        gone = {n for n in gone if not excluded_re.search(n)}
    for node in list(missing_runs):
        if node not in gone:
            del missing_runs[node]
    for node in gone:
        missing_runs[node] = missing_runs.get(node, 0) + 1
    removable = set() if args.no_remove else {
        n for n in gone if missing_runs[n] >= args.remove_after}
    for node in sorted(gone - removable):
        log.warning("%s: %s (%d/%d runs) - %s", node, why_gone(node),
                    missing_runs[node], args.remove_after,
                    "removal disabled" if args.no_remove else "will remove if still so")

    exit_code = 0
    if len(removable) > args.max_remove and not args.force:
        log.error("%d nodes would be removed (limit %d), skipping all removals; "
                  "re-run with --force if this is intended: %s",
                  len(removable), args.max_remove, ",".join(sorted(removable)))
        removable = set()
        exit_code = 1

    for name, (path, port, gpu_only, lines, entries) in files.items():
        filename = os.path.basename(path)
        eligible = gpu_nodes if gpu_only else slurm_nodes
        candidates = [n for n in eligible if n not in entries]
        if candidates and not args.no_probe:
            answering = ec.probe_exporters(candidates, port)
            silent = [n for n in candidates if n not in answering]
            if silent:
                log.info("%s: not adding %d node(s), %s exporter not answering on "
                         "port %d: %s", filename, len(silent), name, port,
                         ",".join(silent))
            to_add = [n for n in candidates if n in answering]
        else:
            to_add = candidates

        to_remove = sorted(n for n in removable if n in entries)
        if not to_add and not to_remove:
            log.info("%s: up to date (%d targets)", filename, len(entries))
            continue

        for n in to_add:
            log.info("%s%s: add %s", "[dry-run] " if args.dry_run else "",
                     filename, ec.target_address(n, port))
        for n in to_remove:
            log.info("%s%s: remove %s (%s)", "[dry-run] " if args.dry_run else "",
                     filename, n, why_gone(n))
        if args.dry_run:
            continue
        try:
            ec.atomic_write(path, "".join(update_lines(lines, to_add, to_remove, entries, port)))
        except OSError as e:
            log.error("cannot write %s: %s", path, e)
            exit_code = 1
            continue
        entries = target_entries(read_targets_file(path))
        files[name] = (path, port, gpu_only, lines, entries)
        managed |= set(to_add)

    if args.dry_run:
        return exit_code

    still_listed = set()
    for (_p, _port, _g, _l, entries) in files.values():
        still_listed |= set(entries)
    managed = {n for n in managed if n in slurm_set or n in still_listed}
    missing_runs = {n: c for n, c in missing_runs.items() if n in still_listed}
    try:
        ec.save_json(args.state_file, {"managed": sorted(managed),
                                       "missing_runs": missing_runs})
    except OSError as e:
        log.error("cannot write state file %s: %s", args.state_file, e)
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
