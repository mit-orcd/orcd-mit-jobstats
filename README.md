# orcd-mit-jobstats

Fork of [PrincetonUniversity/jobstats](https://github.com/PrincetonUniversity/jobstats), adapted for MIT ORCD Slurm and Prometheus.

## Purpose

**jobstats** prints a utilization report for a finished or running Slurm job: CPU use, CPU memory, and (when applicable) GPU memory, GPU duty cycle, and [detailed GPU metrics](#detailed-gpu-metrics) such as SM, Tensor Core and memory-bandwidth utilization, power, temperature, and PCIe/NVLink traffic. It helps users and staff see whether a job used its allocation well and surfaces short advisory notes when metrics suggest waste or misconfiguration.

The tool combines **job metadata and accounting** from Slurm with **time-series metrics** from Prometheus (cgroup and NVIDIA exporters keyed by job ID). It does not replace `sacct` or the cluster monitoring stack; it interprets them for a single job in one view.

## How it works

| Source | Role |
|--------|------|
| **Slurm** (`sacct`) | Job ID, user, account, partition, QOS, state, time limits, TRES (including GPUs), start/end times, optional cached stats in admin comment. |
| **Prometheus** (`PROM_SERVER` in `config.py`) | Max/avg metrics over the job window: cgroup CPU and memory, GPU memory and utilization, detailed GPU metrics. |

Prometheus is the **only** metrics backend; all PromQL targets the server configured as `PROM_SERVER`. The ORCD series have no `cluster` label, so, unlike upstream, the queries select by job ID only.

If a job is missing from the **live** accounting database (common for very old jobs), **jobstats** can retry `sacct` (and `seff` for very short runs) using an alternate **`slurm.conf`** via the environment variable **`SLURM_CONF`**, controlled by site config—see below.

## Configuration (`config.py`)

- **`PROM_SERVER`** — Base URL of the Prometheus API (e.g. `http://host:9090`).
- **`SLURM_ARCHIVE_CONF`** — Path to an archived `slurm.conf`, **or** a directory that contains `slurm.conf`. When the default Slurm install does not return the job, jobstats retries with `SLURM_CONF` set to that file. Set to `None` to disable. Prometheus still uses only `PROM_SERVER`.
- **`GPU_EXPORTER_JOBID`** — How the GPU series of a job are selected. `False` joins each metric with `nvidia_gpu_jobId` (works with any exporter version, slow for long jobs). `True` uses the `jobid` label of the GPU metrics (much faster) and needs version 0.2.2+ of the [NVIDIA GPU exporter](https://github.com/plazonic/nvidia_gpu_prometheus_exporter) on every GPU node. `config.py` has two queries to check this.
- **`GPU_METRICS_EXPORTER`**, **`GPU_METRICS`**, **`GPU_METRICS_DOCS_URL`** — The [detailed GPU metrics](#detailed-gpu-metrics).
- **`MIG_NODES_1`**, **`MIG_NODES_2`** — Nodes with MIG GPUs. GPU utilization of jobs on them is reported as unknown instead of as an error.
- **`PROM_RETENTION_DAYS`** — Used only by notes that call `self.js.is_retained()`.
- **`CPU_MEM_UTIL_RED`**, **`GPU_MEM_UTIL_RED`** — Optional. Memory meters turn red below these percentages.
- Thresholds, cluster name translation, default cores/memory per node, and **advisory note** text live in the same file (`NOTES`).

## Detailed GPU metrics

For GPU jobs the report can show per-GPU metrics beyond utilization and memory. Each entry of `GPU_METRICS` in `config.py` is one metric:

```python
GPU_METRICS["SM"] = {"metric": "sm_util_percent",
                     "operation": "avg_over_time",
                     "show_overall": True,    # meter in "Overall Utilization"
                     "show_per_gpu": True,    # value per GPU in "Detailed Utilization"
                     "write_to_db": True,     # included in "jobstats -j/-b"
                     "long_name": "Streaming Multiprocessor (SM) utilization"}
```

```text
  Detailed GPU Metrics
                             -----------------------------------------------------------------------------------
                               SM | OCC |    TC |  INT | FP16 Max | FP32 Avg | FP64 Max | DRAM BW | Power | Temp
                             -----+-----+-------+------+----------+----------+----------+---------+-------+-----
    node2906.inband (GPU 0)   64% | 10% | 15.1% | 6.1% |     0.9% |    20.0% |     0.0% |     30% |  346W | 46°C
    node2906.inband (GPU 1)   30% | 10% |  0.0% | 5.9% |     0.2% |    22.1% |     0.0% |     25% |  337W | 43°C
                             -----------------------------------------------------------------------------------

                             -------------------------------------------------
                             PCIe Recv | PCIe Sent | NVLink Recv | NVLink Sent
                             ----------+-----------+-------------+------------
    node2906.inband (GPU 0)    71 MB/s |   12 MB/s |     25 GB/s |     25 GB/s
    node2906.inband (GPU 1)    68 MB/s |   12 MB/s |     25 GB/s |     25 GB/s
                             -------------------------------------------------
```

- The metrics come from version 0.2.3+ of the NVIDIA GPU exporter and exist only for Hopper GPUs and later (H100, H200, ...). A metric that is not in Prometheus for a job is left out, so jobs on older GPUs or older exporters get the same report as before.
- Each metric is one extra Prometheus query per GPU job. If one of these queries fails, the remaining ones are skipped and the report is still printed (`-d` shows why).
- Tables are used when four or more metrics are shown, otherwise lines of text. Metrics named `PCIe...` or `NVLink...` go in a second table. The tables are wider than 80 columns; remove metrics or set `"show_per_gpu": False` to narrow them.
- Percentages are stored as fractions in the JSON (`"sm_util_percent_avg": 0.643` is 64.3%); only `gpu_utilization` is stored as a percentage.
- In notes, the overall value of a metric with `"show_overall": True` is `self.gm_overall["<NAME>-util"]` (see the commented example in `config.py`).
- The metrics are defined in the [upstream documentation](https://princetonuniversity.github.io/jobstats/setup/detailed_gpu_metrics/). Set `GPU_METRICS = {}` to turn the feature off.

## Usage

```text
jobstats [options] jobid [jobid ...]
```

| Option | Meaning |
|--------|---------|
| `-c`, `--cluster` | Slurm cluster name for `sacct -M` when not using the host default. |
| `-d`, `--debug` | Print debug lines (queries, Slurm fields, archive retry). |
| `--debug-slurm-env MODE` | **Requires `-d`.** Restricts which Slurm accounting environment `sacct` uses: `primary` (live config only, no archive retry), `archive` (archive `slurm.conf` only, as if the job were missing from the live DB), or `both` (default when this flag is omitted—try primary, then archive). |
| `-S`, `--syslog` | Log debug to syslog. |
| `-f`, `--force` | Ignore cached job payload in Slurm admin comment; re-fetch from Prometheus. |
| `-j`, `--json` | Emit per-node JSON instead of the text report. |
| `-b`, `--base64` | Same as JSON, gzip-compressed and base64-encoded (for storage). |
| `-n`, `--no-color` | Disable ANSI colors in the text report. |
| `-B`, `--batch-script` | Also show the Slurm batch script (`sacct -B`), if available. |

Examples:

```bash
jobstats 12345678
jobstats -c mycluster 12345678 -d

# Debug Slurm lookup in isolation (archive path comes from SLURM_ARCHIVE_CONF in config.py)
jobstats -d --debug-slurm-env primary 12345678    # live Slurm only
jobstats -d --debug-slurm-env archive 12345678    # archived accounting only
jobstats -d --debug-slurm-env both 12345678       # same as: jobstats -d 12345678
```

`--debug-slurm-env` is ignored unless `-d` is set; using it without `-d` is a CLI error.

## Relation to upstream

The code is merged with upstream release 1.1.0 (October 2026). Kept from this fork:

- Archive Slurm fallback (`SLURM_ARCHIVE_CONF`, `--debug-slurm-env`), also used for `seff` and `-B`.
- PromQL without the `cluster` label.
- Cores per node taken from the Slurm allocation (`NCPUS / NNodes`) instead of the `cgroup_cpus` metric.
- The ORCD notes and thresholds in `config.py`.

Taken from upstream: detailed GPU metrics, `GPU_EXPORTER_JOBID`, shared memory counted in used CPU memory (when the cgroup exporter reports `cgroup_memory_shmem_bytes`), SLUID lookup for Slurm 26.05+, MIG handling, `-B`, `UNLIMITED` time limits, and the computed `zero_gpu` / `zero_cpu` / `gpu_show` values used by note conditions.

Not taken: the external MariaDB/MySQL storage (`db_handler.py`, `store_jobstats.py`).

Intentional differences from upstream in the merged code:

- Values below 1 keep three decimals (upstream: only below 0.1), so fraction metrics are not limited to steps of 10%.
- A failed detailed GPU metric query does not abort the report.
- A metric found on only some nodes of a job is shown for those GPUs (upstream hides it).
- The CPU utilization meter is rounded, not truncated.
- The report falls back to ASCII for `─` and `°` when stdout cannot encode them.

## Layout

- **`jobstats`** — CLI entrypoint.
- **`jobstats.py`** — Slurm lookup, Prometheus queries, aggregation.
- **`config.py`** — `PROM_SERVER`, `SLURM_ARCHIVE_CONF`, sampling period, GPU metrics, notes.
- **`output_formatters.py`** — Human-readable report and note rendering.

## Requirements

Python 3 with `requests` (and optionally `blessed` for terminal features). Slurm client commands (`sacct`, and `seff` when runtime is shorter than two sampling periods) must be available where the tool runs.
