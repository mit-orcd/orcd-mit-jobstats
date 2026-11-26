# Plan: AMD GPU exporter (MI355X and other AMD GPU nodes)

Status: not started. Written 2026-11-25 as a note for later work.

## Goal

AMD GPU nodes (e.g. `gpu:mi355x` GRES) get an AMD GPU exporter instead of
the NVIDIA one, are scraped by Prometheus, and their GPU jobs show GPU
utilization and memory in `jobstats`.

## Where things stand today

- `autodeploy/exporter_common.py` knows one GPU exporter. Any node whose
  Slurm `Gres` has a `gpu` entry is a "GPU node": it gets the salt state
  `engaging.role.nvidia-gpu-exporter` and is probed/added on port 9445 in
  `gpu_targets.yml`. The GPU vendor is not looked at.
- So an AMD node is sent the NVIDIA salt state on every deploy run (4x
  daily), since its port 9445 never answers. Since the switch to
  exclusion-based eligibility (only `centos7` is left out), this also
  reaches AMD nodes that have no `rocky8` feature.
- `jobstats.py` only queries `nvidia_gpu_*` metrics and reads the GPU index
  from the `minor_number` label. An AMD GPU job shows "GPU utilization
  could not be determined".

## Decisions to make first

1. **Which exporter.** Candidate: AMD Device Metrics Exporter
   (<https://instinct.docs.amd.com/projects/device-metrics-exporter/>).
   Facts from its 1.5.4 documentation:
   - Listens on port 5000 (`/metrics`).
   - Runs as a container (`rocm/device-metrics-exporter`) or as host
     services (`gpuagent.service`, `amd-metrics-exporter.service`). The
     docs only mention an apt package for the host install, so the path
     for Rocky needs checking.
   - Slurm integration is by prolog/epilog scripts
     (`example/slurm/exporter-prolog.sh`, `exporter-epilog.sh`) that
     record the job of each GPU under `/var/run/exporter/`. It needs
     `PrologFlags=Alloc`.
   - Job labels are off by default. `JOB_ID`, `JOB_USER`, `GPU_ID` (and
     others) must be listed under `GPUConfig.Labels` in `config.json`.
   - Metrics (fields): `GPU_GFX_ACTIVITY` (percent, 0-100),
     `GPU_USED_VRAM` and `GPU_TOTAL_VRAM` (MB), `GPU_PACKAGE_POWER` /
     `GPU_POWER_USAGE` (W), `GPU_JUNCTION_TEMPERATURE` (C).
   - Not checked: the exact metric names as exposed (expected lower case,
     e.g. `gpu_gfx_activity`) and the label names (`job_id`, `gpu_id`).
     Confirm with `curl` on a node once it runs.
2. **How to tell an AMD node from an NVIDIA node.** Proposed: by GRES
   type, with a pattern such as `^mi\d` (`gpu:mi355x:8`). Alternative: a
   Slurm node feature. Needs the real GRES names of the AMD nodes.
3. **Targets file.** Proposed: a new `amd_gpu_targets.yml` and a new
   Prometheus scrape job, rather than mixing ports in `gpu_targets.yml`.
4. **Where jobstats adapts** (see step 4): in the jobstats queries
   (proposed) or with Prometheus recording rules that republish the AMD
   series under the `nvidia_gpu_*` names.
5. **Salt state name** for the AMD exporter (e.g.
   `engaging.role.amd-gpu-exporter`), and who writes it.

## Work

### 1. Exporter on the nodes (salt, outside this repo)

- Salt state that installs and starts the exporter with a `config.json`
  that enables the job labels.
- Prolog/epilog scripts on the AMD nodes. Check they coexist with the
  existing prolog/epilog scripts (the NVIDIA exporter uses its own).
- Verify on one node: `curl -s localhost:5000/metrics | grep job_id`
  while a GPU job runs.

### 2. autodeploy (this repo)

- `get_slurm_nodes()`: also return the GRES types of each node, and split
  GPU nodes into NVIDIA and AMD.
- `EXPORTERS`: replace the `gpu_only` flag with a node selector (`all`,
  `nvidia_gpu`, `amd_gpu`). Add an `amd_gpu` entry (targets file, port,
  salt state). The existing `gpu` entry becomes NVIDIA-only.
- `deploy_exporters.py`: stop sending the NVIDIA state to AMD nodes;
  deploy the AMD state instead. The state file gets an `amd_gpu` list.
- `sync_prometheus_targets.py`: add AMD nodes to the new file. Remove AMD
  nodes that are listed in `gpu_targets.yml` (none expected, since 9445
  never answered there, but check).
- Decide what a node with both vendors gets (probably both exporters).
- `deploy.sh`: know about the new targets file. Update `README.md`.
- Test with a fake `scontrol` as was done for the feature filter.

### 3. Prometheus (core025)

- New scrape job with `file_sd_configs` on `amd_gpu_targets.yml`.
- Check the scrape interval matches `SAMPLING_PERIOD` (30 s) in
  `config.py`.

### 4. jobstats (this repo)

- `get_job_stats()`: GPU utilization and memory for AMD jobs. The
  conversion can be done in the query, e.g. used memory as
  `label_replace(gpu_used_vram{job_id='<id>'} * 1048576, "minor_number", "$1", "gpu_id", "(.*)")`
  so that `get_data_out()` and the report code stay as they are.
- Pick the vendor per job from `AllocTRES` (`gres/gpu:mi355x=...`), or
  query both and join with `or`. A job has only one vendor.
- Detailed GPU metrics (`GPU_METRICS`) are NVML-only. They are already
  left out when not found, so AMD jobs simply will not show them. Power
  and temperature could be added for AMD later.
- Check the notes in `config.py` read correctly for AMD jobs.
- Alternative (decision 4): recording rules on Prometheus that publish
  `nvidia_gpu_duty_cycle` etc. for AMD GPUs. No jobstats change, but the
  rules must be maintained and the data lags by the rule interval.

### 5. Rollout

1. One AMD node by hand: exporter, prolog/epilog, `curl` check.
2. Add its target by hand, confirm series and job labels in Prometheus.
3. jobstats change, tested against a real AMD job with `jobstats -d`.
4. autodeploy change, `--dry-run` on admin001 and core025, then
   `deploy.sh`.

## Until this is done

Keep the NVIDIA state away from AMD nodes with the existing option, e.g.
in the `exporter-deploy.service` command line:

```
deploy_exporters.py --exclude '^(node6000|node6001)$'
```

Note that `--exclude` skips all three exporters on those nodes, so the
cgroup and node exporters would have to be deployed there by hand.
