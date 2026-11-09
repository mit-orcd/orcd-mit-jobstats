#!/usr/bin/env bash
#
# Install the jobstats exporter automation from admin001:
#   - admin001 (this host):  deploy_exporters.py + exporter-deploy.{service,timer}
#   - Prometheus server:     sync_prometheus_targets.py + prometheus-targets-sync.{service,timer}
#                            (installed remotely over ssh, default root@core025)
#
# Run as root on admin001 from a checkout of this repo:
#   ./autodeploy/deploy.sh [options]
#
# Each side runs a --dry-run of its script before the timer is enabled; a
# failing dry-run aborts that side unless --skip-check is given.

set -euo pipefail

PROM_HOST="root@core025"
INSTALL_DIR="/opt/exporter-autodeploy"
UNIT_DIR="/etc/systemd/system"
TARGETS_DIR="/db/prometheus/targets"
SLURM_SSH=""
DO_LOCAL=1
DO_REMOTE=1
ENABLE=1
CHECK=1
MIGRATE=1

SSH_OPTS=(-oConnectTimeout=10 -oBatchMode=yes)
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOCAL_FILES=(exporter_common.py deploy_exporters.py)
LOCAL_UNITS=(exporter-deploy.service exporter-deploy.timer)
REMOTE_FILES=(exporter_common.py sync_prometheus_targets.py)
REMOTE_UNITS=(prometheus-targets-sync.service prometheus-targets-sync.timer)

usage() {
    cat <<EOF
Usage: $0 [options]

  --prom-host USER@HOST  Prometheus server to install the target sync on
                         (default: $PROM_HOST)
  --slurm-ssh USER@HOST  core025 has no Slurm client: have the sync run
                         scontrol on this host over ssh (written to a
                         systemd drop-in on the Prometheus server)
  --local-only           install only on this host (admin001)
  --remote-only          install only on the Prometheus server
  --no-enable            install files but don't enable/start the timers
  --skip-check           don't run the --dry-run checks before enabling
  --no-migrate           don't disable/migrate the old exporter-autodeploy
                         service on this host
  -h, --help             show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prom-host)   PROM_HOST="$2"; shift 2 ;;
        --slurm-ssh)   SLURM_SSH="$2"; shift 2 ;;
        --local-only)  DO_REMOTE=0; shift ;;
        --remote-only) DO_LOCAL=0; shift ;;
        --no-enable)   ENABLE=0; shift ;;
        --skip-check)  CHECK=0; shift ;;
        --no-migrate)  MIGRATE=0; shift ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log()  { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "must run as root"
for f in "${LOCAL_FILES[@]}" "${LOCAL_UNITS[@]}" "${REMOTE_FILES[@]}" "${REMOTE_UNITS[@]}"; do
    [[ -f "$SRC_DIR/$f" ]] || die "missing $SRC_DIR/$f"
done

# ---------------------------------------------------------------------------
# admin001 (local)
# ---------------------------------------------------------------------------
deploy_local() {
    log "[$(hostname -s)] installing exporter deployment service"
    for cmd in python3 scontrol clush; do
        command -v "$cmd" >/dev/null || die "$cmd not found on $(hostname -s)"
    done

    if [[ $MIGRATE -eq 1 && -f $UNIT_DIR/exporter-autodeploy.timer ]]; then
        log "disabling old exporter-autodeploy.timer"
        systemctl disable --now exporter-autodeploy.timer || true
        if [[ -f /var/lib/exporter-autodeploy/state.json && ! -f /var/lib/exporter-deploy/state.json ]]; then
            log "migrating deployment state to /var/lib/exporter-deploy/state.json"
            install -d -m 0755 /var/lib/exporter-deploy
            cp -p /var/lib/exporter-autodeploy/state.json /var/lib/exporter-deploy/state.json
        fi
        rm -f "$UNIT_DIR/exporter-autodeploy.service" "$UNIT_DIR/exporter-autodeploy.timer" \
              /usr/local/bin/autodeploy_exporters.py
    fi

    install -d -m 0755 "$INSTALL_DIR"
    install -m 0644 "$SRC_DIR/exporter_common.py" "$INSTALL_DIR/"
    install -m 0755 "$SRC_DIR/deploy_exporters.py" "$INSTALL_DIR/"
    for u in "${LOCAL_UNITS[@]}"; do
        install -m 0644 "$SRC_DIR/$u" "$UNIT_DIR/"
    done
    systemctl daemon-reload

    if [[ $CHECK -eq 1 ]]; then
        log "dry-run of deploy_exporters.py"
        install -d -m 0755 /var/lib/exporter-deploy
        "$INSTALL_DIR/deploy_exporters.py" --dry-run \
            || die "deploy_exporters.py --dry-run failed; fix it or re-run with --skip-check"
    fi

    if [[ $ENABLE -eq 1 ]]; then
        systemctl enable --now exporter-deploy.timer
        log "exporter-deploy.timer enabled:"
        systemctl list-timers exporter-deploy.timer --no-pager | sed -n '1,2p'
    fi
}

# ---------------------------------------------------------------------------
# Prometheus server (remote)
# ---------------------------------------------------------------------------
deploy_remote() {
    log "[$PROM_HOST] installing Prometheus target sync service"
    ssh "${SSH_OPTS[@]}" "$PROM_HOST" true \
        || die "cannot ssh to $PROM_HOST (passwordless root key required)"

    local stage
    stage=$(ssh "${SSH_OPTS[@]}" "$PROM_HOST" mktemp -d /tmp/exporter-autodeploy.XXXXXX)
    tar -C "$SRC_DIR" -cf - "${REMOTE_FILES[@]}" "${REMOTE_UNITS[@]}" \
        | ssh "${SSH_OPTS[@]}" "$PROM_HOST" tar -C "$stage" -xf -

    # Remote side: everything below runs on the Prometheus server. ssh joins
    # its arguments into one string for the remote shell, so each argument
    # is quoted here to survive that (an empty --slurm-ssh would vanish).
    local remote_args
    remote_args=$(printf '%q ' "$stage" "$INSTALL_DIR" "$UNIT_DIR" "$TARGETS_DIR" \
        "$SLURM_SSH" "$CHECK" "$ENABLE")
    ssh "${SSH_OPTS[@]}" "$PROM_HOST" "bash -s -- $remote_args" <<'REMOTE'
set -euo pipefail
stage=$1 install_dir=$2 unit_dir=$3 targets_dir=$4 slurm_ssh=$5 check=$6 enable=$7
trap 'rm -rf "$stage"' EXIT
host=$(hostname -s)
die() { echo "ERROR [$host]: $*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 not found"
[[ -d $targets_dir && -w $targets_dir ]] || die "$targets_dir missing or not writable"
for f in cgroup_targets.yml gpu_targets.yml node_targets.yml; do
    [[ -f $targets_dir/$f ]] || echo "WARNING [$host]: $targets_dir/$f does not exist yet; it will be created" >&2
done

if [[ -z $slurm_ssh ]]; then
    scontrol -o show node >/dev/null 2>&1 \
        || die "scontrol does not work here; install the Slurm client + munge, or re-run deploy.sh with --slurm-ssh USER@HOST"
else
    ssh -oConnectTimeout=10 -oBatchMode=yes "$slurm_ssh" scontrol -o show node >/dev/null \
        || die "cannot run scontrol via ssh $slurm_ssh (passwordless key required from $host)"
fi

install -d -m 0755 "$install_dir"
install -m 0644 "$stage/exporter_common.py" "$install_dir/"
install -m 0755 "$stage/sync_prometheus_targets.py" "$install_dir/"
install -m 0644 "$stage/prometheus-targets-sync.service" "$stage/prometheus-targets-sync.timer" "$unit_dir/"

dropin="$unit_dir/prometheus-targets-sync.service.d"
if [[ -n $slurm_ssh ]]; then
    install -d -m 0755 "$dropin"
    cat > "$dropin/slurm-ssh.conf" <<EOF
[Service]
ExecStart=
ExecStart=$install_dir/sync_prometheus_targets.py --slurm-ssh $slurm_ssh
EOF
else
    rm -f "$dropin/slurm-ssh.conf"
fi
systemctl daemon-reload

extra=()
[[ -n $slurm_ssh ]] && extra=(--slurm-ssh "$slurm_ssh")
if [[ $check -eq 1 ]]; then
    echo "==> [$host] dry-run of sync_prometheus_targets.py"
    "$install_dir/sync_prometheus_targets.py" --dry-run ${extra[@]+"${extra[@]}"} \
        || die "sync_prometheus_targets.py --dry-run failed; fix it or re-run with --skip-check"
fi

if [[ $enable -eq 1 ]]; then
    systemctl enable --now prometheus-targets-sync.timer
    echo "==> [$host] prometheus-targets-sync.timer enabled:"
    systemctl list-timers prometheus-targets-sync.timer --no-pager | sed -n '1,2p'
fi
REMOTE
}

if [[ $DO_LOCAL -eq 1 ]]; then deploy_local; fi
if [[ $DO_REMOTE -eq 1 ]]; then deploy_remote; fi
log "done"
