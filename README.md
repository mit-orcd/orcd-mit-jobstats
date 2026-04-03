# orcd-mit-jobstats

Fork of [PrincetonUniversity/jobstats](https://github.com/PrincetonUniversity/jobstats), adapted for MIT ORCD Slurm and Prometheus.

## Purpose

**jobstats** prints a utilization report for a finished or running Slurm job: CPU use, CPU memory, and (when applicable) GPU memory and GPU duty cycle. It helps users and staff see whether a job used its allocation well and surfaces short advisory notes when metrics suggest waste or misconfiguration.

The tool combines **job metadata and accounting** from Slurm with **time-series metrics** from Prometheus (cgroup and NVIDIA exporters keyed by job ID). It does not replace `sacct` or the cluster monitoring stack; it interprets them for a single job in one view.

## How it works

| Source | Role |
|--------|------|
| **Slurm** (`sacct`) | Job ID, user, account, partition, QOS, state, time limits, TRES (including GPUs), start/end times, optional cached stats in admin comment. |
| **Prometheus** (`PROM_SERVER` in `config.py`) | Max/avg metrics over the job window: cgroup CPU and memory, GPU memory and utilization. |

Prometheus is the **only** metrics backend; all PromQL targets the server configured as `PROM_SERVER`.

If a job is missing from the **live** accounting database (common for very old jobs), **jobstats** can retry `sacct` (and `seff` for very short runs) using an alternate **`slurm.conf`** via the environment variable **`SLURM_CONF`**, controlled by site config—see below.

## Configuration (`config.py`)

- **`PROM_SERVER`** — Base URL of the Prometheus API (e.g. `http://host:9090`).
- **`SLURM_ARCHIVE_CONF`** — Path to an archived `slurm.conf`, **or** a directory that contains `slurm.conf`. When the default Slurm install does not return the job, jobstats retries with `SLURM_CONF` set to that file. Set to `None` to disable. Prometheus still uses only `PROM_SERVER`.
- Thresholds, cluster name translation, default cores/memory per node, and **advisory note** text live in the same file (`NOTES`).

## Usage

```text
jobstats [options] jobid [jobid ...]
```

| Option | Meaning |
|--------|---------|
| `-c`, `--cluster` | Slurm cluster name for `sacct -M` when not using the host default. |
| `-d`, `--debug` | Print debug lines (queries, Slurm fields, archive retry). |
| `-S`, `--syslog` | Log debug to syslog. |
| `-f`, `--force` | Ignore cached job payload in Slurm admin comment; re-fetch from Prometheus. |
| `-j`, `--json` | Emit per-node JSON instead of the text report. |
| `-b`, `--base64` | Same as JSON, gzip-compressed and base64-encoded (for storage). |
| `-n`, `--no-color` | Disable ANSI colors in the text report. |

Example:

```bash
jobstats 12345678
jobstats -c mycluster 12345678 -d
```

## Layout

- **`jobstats`** — CLI entrypoint.
- **`jobstats.py`** — Slurm lookup, Prometheus queries, aggregation.
- **`config.py`** — `PROM_SERVER`, `SLURM_ARCHIVE_CONF`, sampling period, notes.
- **`output_formatters.py`** — Human-readable report and note rendering.

## Requirements

Python 3 with `requests` (and optionally `blessed` for terminal features). Slurm client commands (`sacct`, and `seff` when runtime is shorter than two sampling periods) must be available where the tool runs.
