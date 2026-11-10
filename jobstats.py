import base64
import csv
import datetime
import gzip
import json
import os
import subprocess
import sys
import syslog
import time
from typing import Dict
import requests
import config as c

# optional settings (older config.py files do not define them)
if not hasattr(c, "GPU_EXPORTER_JOBID"):
    c.GPU_EXPORTER_JOBID = False
if not hasattr(c, "GPU_METRICS"):
    c.GPU_METRICS = {}
if not hasattr(c, "GPU_METRICS_EXPORTER"):
    c.GPU_METRICS_EXPORTER = "None"


# upstream PrincetonUniversity/jobstats release this ORCD fork is merged with
__version__ = "1.1.0+orcd"

# number of seconds between measurements
SAMPLING_PERIOD = c.SAMPLING_PERIOD

# conversion factors
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600

# for convenience
DEVNULL = open(os.devnull, 'w')
# next line produces unix times
os.environ['SLURM_TIME_FORMAT'] = "%s"


def _resolve_slurm_conf_path(path):
    """Return absolute path to slurm.conf for SLURM_CONF, or None if not usable."""
    if not path:
        return None
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        return os.path.abspath(path)
    if os.path.isdir(path):
        candidate = os.path.join(path, "slurm.conf")
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


class DetailedGpuMetric:

    """Class for individual detailed GPU metrics (see GPU_METRICS in config.py)."""

    def __init__(self, name: str, settings: Dict[str, Dict], is_mig: bool) -> None:
        self.name = name
        self.metric       = settings["metric"]
        self.operation    = settings["operation"]
        self.show_overall = settings["show_overall"]
        self.show_per_gpu = settings["show_per_gpu"]
        self.write_to_db  = settings["write_to_db"]
        self.long_name    = settings.get("long_name")
        self.is_mig = is_mig
        # these metrics are stored as fractions (0 to 1) and shown as percentages
        ms = ("sm", "fp16", "fp32", "fp64", "tensor", "integer", "occupancy", "dram")
        self.is_percentage = any(m in self.metric for m in ms)
        if self.is_percentage:
            self.fac = 100
        elif "power" in self.metric:
            # milliwatts to watts
            self.fac = 0.001
        else:
            self.fac = 1
        # duty_cycle is already stored as a percentage
        if "duty" in self.metric:
            self.is_percentage = True
        # internal metric name for summary statistics
        self.name_ss = Jobstats.internal_detailed_metric_name(self.metric,
                                                              self.operation)

    def parse(self, sp_node: dict) -> None:
        """Loop over the data associated with each node. Variable n
           is node name while g is the GPU index. The value of
           total__value_gpus[1] is used later to determine whether
           or not a valid value was found. The error code is 1 when
           the metric is missing for a node (e.g., a job on nodes with
           different types of GPUs). The values of the other nodes are
           still collected so that they can be shown per GPU.
        """
        overall = 0
        overall_gpu_count = 0
        self.error_code = 0
        self.node_value_index = []
        for n, d in sp_node.items():
            if self.name_ss in d:
                gpus = sorted(d[self.name_ss].keys())
                for g in gpus:
                    value = self.fac * d[self.name_ss][g]
                    overall += value
                    overall_gpu_count += 1
                    self.node_value_index.append((n, value, g))
            else:
                if self.is_mig:
                    self.node_value_index.append((n, None, "#"))
                else:
                    self.error_code = 1
                    self.node_value_index.append((n, None, None))
        self.total__value_gpus = (overall, overall_gpu_count)

    def __str__(self) -> str:
        return (f"{self.name}, {self.metric}, {self.operation}, "
                f"{self.node_value_index}, {self.error_code}")


# class that gets and holds per job prometheus statistics
class Jobstats:
    slurm_version = None
    sluid_available = False

    # initialize basic job stats, can be called either with those stats
    # provided and if not it will fetch them
    def __init__(self,
                 jobid=None,
                 jobidraw=None,
                 sluid=None,
                 start=None,
                 end=None,
                 gpus=None,
                 cluster=None,
                 prom_server=None,
                 debug=False,
                 debug_syslog=False,
                 force_recalc=False,
                 debug_slurm_env=None,
                 batch_script=False,
                 json_or_base64=False):
        self.__detect_slurm_version()
        self.cluster = cluster
        self.prom_server = prom_server
        self.debug = debug
        self.debug_syslog = debug_syslog
        self.force_recalc = force_recalc
        # With debug enabled only: narrow which Slurm accounting env sacct uses (see CLI).
        self.debug_slurm_env = debug_slurm_env if debug else None
        self.batch_script = batch_script
        self.json_or_base64 = json_or_base64
        self.sluid = sluid
        self.sp_node = {}
        self._subprocess_slurm_env = None  # set when archive SLURM_CONF is used for sacct
        # translate cluster name
        if self.cluster in c.CLUSTER_TRANS:
            self.cluster = c.CLUSTER_TRANS[self.cluster]
        if self.debug_syslog:
            syslog.openlog('jobstat[%s]' % jobid)
        if jobidraw is None:
            self.jobid = jobid
            if not self.__get_job_info():
                if self.state == "PENDING":
                    self.error("Failed to get details for job %s since it is a PENDING job." % jobid)
                else:
                    self.error("Failed to get details for job %s." % jobid)
        else:
            if jobid is None:
                jobid = jobidraw
            self.jobid = jobid
            self.jobidraw = jobidraw
            self.start = start
            self.end = end
            self.gpus = gpus
            self.data = None
            self.timelimitraw = None
        self.diff = self.end - self.start
        # translate cluster name
        if self.cluster in c.CLUSTER_TRANS_INV:
            self.cluster = c.CLUSTER_TRANS_INV[self.cluster]
        self.debug_print("jobid=%s, " \
                         "jobidraw=%s, " \
                         "sluid=%s, " \
                         "start=%s, " \
                         "end=%s, " \
                         "gpus=%s, " \
                         "diff=%s, " \
                         "cluster=%s, " \
                         "data=%s, " \
                         "timelimitraw=%s" % (self.jobid,
                                              self.jobidraw,
                                              self.sluid,
                                              self.start,
                                              self.end,
                                              self.gpus,
                                              self.diff,
                                              self.cluster,
                                              self.data,
                                              self.timelimitraw))
        if self.data is not None and self.data.startswith('JS1:') and len(self.data) > 10:
            try:
                t = json.loads(gzip.decompress(base64.b64decode(self.data[4:])))
                self.sp_node = t["nodes"]
            except Exception as e:
                print("ERROR: %s" %e)
        if not self.sp_node and (self.diff >= 2 * SAMPLING_PERIOD):
            # call prometheus to get detailed statistics (if long enough)
            self.get_job_stats()
        if len(self.sp_node) == 0 and self.json_or_base64:
            return
        self.parse_stats()
        if self.batch_script:
            cmd = ["sacct", "-j", f"{self.jobid}", "-B"]
            try:
                self.job_script = subprocess.check_output(cmd,
                                                          stderr=DEVNULL,
                                                          env=self.__slurm_env()).decode("utf-8")
            except Exception as e:
                self.debug_print("sacct -B failed: %s" % e)
                self.job_script = ""

    def nodes(self):
        return self.sp_node

    def jobid(self):
        return self.jobidraw

    def diff(self):
        return self.diff

    def gpus(self):
        return self.gpus

    # report an error on stderr and fail
    def error(self, msg):
        sys.stderr.write("%s\n" % msg)
        if self.debug_syslog:
            syslog.syslog(msg)
        sys.exit(1)

    def debug_print(self, msg):
        if self.debug:
            print('DEBUG: %s' % msg)
        if self.debug_syslog:
            syslog.syslog(msg)

    @staticmethod
    def __detect_slurm_version():
        """As of Slurm 26.05 the cgroup of a job may be named after its SLUID
           instead of its jobid, so both must be queried. The version is only
           looked up once. If it cannot be determined then SLUID is not used
           (a broken sacct is reported later by the job lookup)."""
        if Jobstats.slurm_version is not None:
            return
        try:
            out = subprocess.check_output(["sacct", "-V"], stderr=DEVNULL).decode("utf-8")
            Jobstats.slurm_version = out.split()[1]
            Jobstats.sluid_available = int(Jobstats.slurm_version.split(".")[0]) > 25
        except Exception:
            Jobstats.slurm_version = "unknown"
            Jobstats.sluid_available = False

    def __slurm_env(self):
        """Environment for Slurm commands. It points SLURM_CONF at the archive
           Slurm when that is where sacct found the job."""
        env = os.environ.copy()
        if self._subprocess_slurm_env:
            env.update(self._subprocess_slurm_env)
        return env

    # Get basic info from sacct and set instance variables
    def __get_job_info(self):
        fields = ["jobidraw",
                  "start",
                  "end",
                  "cluster",
                  "alloctres",
                  "admincomment",
                  "user",
                  "account",
                  "state",
                  "nnodes",
                  "ncpus",
                  "reqmem",
                  "qos",
                  "partition",
                  "timelimitraw",
                  "jobname"]
        if self.sluid_available:
            fields.insert(-1, "sluid")
        # jobname must be the last field to handle "|" chars later on
        assert fields[-1] == "jobname"
        fields = ",".join(fields)
        cluster_sacct_arg = self.cluster
        cmd = ["sacct", "-P", "-X", "-o", fields, "-j", self.jobid]
        if cluster_sacct_arg:
            cmd += ["-M", cluster_sacct_arg]

        archive_raw = getattr(c, "SLURM_ARCHIVE_CONF", None)
        archive_conf = _resolve_slurm_conf_path(archive_raw)
        if archive_raw and archive_conf is None:
            self.debug_print(
                "SLURM_ARCHIVE_CONF is set (%s) but no readable slurm.conf found; archive retry disabled"
                % archive_raw
            )
        self._subprocess_slurm_env = None
        slurm_mode = self.debug_slurm_env or "both"
        if slurm_mode not in ("primary", "archive", "both"):
            slurm_mode = "both"

        def fetch_sacct(extra_env=None):
            """Run sacct; return True if JobIDRaw was set from a row."""
            self.start = None
            self.end = None
            self.jobidraw = None
            env = os.environ.copy()
            if extra_env:
                env.update(extra_env)
            try:
                sacct_output = subprocess.check_output(cmd, stderr=DEVNULL, env=env).decode("utf-8").split("\n")
            except Exception:
                return False
            for i in csv.DictReader(sacct_output, delimiter="|"):
                self.jobidraw = i.get("JobIDRaw", None)
                self.sluid = i.get("SLUID", None)
                self.start = i.get("Start", None)
                self.end = i.get("End", None)
                self.cluster = i.get("Cluster", None)
                self.tres = i.get("AllocTRES", None)
                if self.force_recalc:
                    self.data = None
                else:
                    self.data = i.get("AdminComment", None)
                self.user = i.get("User", None)
                self.account = i.get("Account", None)
                self.state = i.get("State", None)
                self.timelimitraw = i.get("TimelimitRaw", None)
                self.nnodes = i.get("NNodes", None)
                self.ncpus = i.get("NCPUS", None)
                self.reqmem = i.get("ReqMem", None)
                self.qos = i.get("QOS", None)
                self.partition = i.get("Partition", None)
                self.jobname = i.get("JobName", None)
                self.debug_print(
                    "jobidraw=%s, sluid=%s, start=%s, end=%s, cluster=%s, tres=%s, data=%s, user=%s, account=%s, state=%s, timelimit=%s, nodes=%s, ncpus=%s, reqmem=%s, qos=%s, partition=%s, jobname=%s"
                    % (
                        self.jobidraw,
                        self.sluid,
                        self.start,
                        self.end,
                        self.cluster,
                        self.tres,
                        self.data,
                        self.user,
                        self.account,
                        self.state,
                        self.timelimitraw,
                        self.nnodes,
                        self.ncpus,
                        self.reqmem,
                        self.qos,
                        self.partition,
                        self.jobname,
                    )
                )
            return self.jobidraw is not None

        if slurm_mode == "archive":
            if not archive_conf:
                self.error(
                    "\n--debug-slurm-env archive requires a usable SLURM_ARCHIVE_CONF "
                    "(path to slurm.conf or a directory containing slurm.conf).\n"
                )
            self.debug_print(
                "sacct: archive Slurm only (--debug-slurm-env archive); skipping primary env"
            )
            got = fetch_sacct({"SLURM_CONF": archive_conf})
            if got:
                self._subprocess_slurm_env = {"SLURM_CONF": archive_conf}
        else:
            got = fetch_sacct()
            if not got and archive_conf and slurm_mode != "primary":
                self.debug_print("sacct: retrying with SLURM_CONF=%s (archive Slurm)" % archive_conf)
                got = fetch_sacct({"SLURM_CONF": archive_conf})
                if got:
                    self._subprocess_slurm_env = {"SLURM_CONF": archive_conf}
            elif not got and archive_conf and slurm_mode == "primary":
                self.debug_print("sacct: skipping archive retry (--debug-slurm-env primary)")

        if not got:
            msg = (f"\nFailed to lookup job {self.jobid}. Make sure the cluster is correct by\n"
                   "specifying the -c option (e.g., $ jobstats 1234567 -c frontier).\n")
            if slurm_mode == "primary":
                msg += "(Archive Slurm was not tried: --debug-slurm-env primary.)\n"
            elif slurm_mode == "archive":
                msg += (
                    f"(Only archive Slurm was tried, with SLURM_CONF={archive_conf}.)\n"
                )
            if archive_conf is None and not archive_raw and slurm_mode != "archive":
                msg += (
                    "If this is an old job, set SLURM_ARCHIVE_CONF in the jobstats config "
                    "so sacct can use the archived accounting database.\n"
                )
            elif archive_conf is None and archive_raw:
                msg += (
                    f"SLURM_ARCHIVE_CONF is {archive_raw!r} but that path is not a usable "
                    "slurm.conf (or slurm.conf under that directory).\n"
                )
            elif slurm_mode == "both" and archive_conf is not None:
                msg += (
                    f"The job was not found with the default Slurm config or with archive config.\n"
                    #f"SLURM_CONF={archive_conf}.\n"
                )
            if cluster_sacct_arg:
                clstr = (
                    c.CLUSTER_TRANS[cluster_sacct_arg]
                    if cluster_sacct_arg in c.CLUSTER_TRANS
                    else cluster_sacct_arg
                )
                msg += f"(Cluster filter in use: {clstr}.)\n"
            self.error(msg)

        self.gpus = 0
        if self.tres is not None and 'gres/gpu=' in self.tres and 'gres/gpu=0,' not in self.tres:
            for part in self.tres.split(","):
                if "gres/gpu=" in part:
                    self.gpus = int(part.split("=")[-1])
 
        if self.timelimitraw.isnumeric():
            self.timelimitraw = int(self.timelimitraw)
        if "CANCEL" in self.state:
            self.state = "CANCELLED"
        if len(self.jobname) > c.MAX_JOBNAME_LEN:
            self.jobname = self.jobname[:c.MAX_JOBNAME_LEN] + "..."

        # currently running jobs will have Unknown as time
        if self.end == 'Unknown':
            self.end = time.time()
        else:
            if self.end.isnumeric():
                self.end = int(self.end)
            else:
                return False
        if self.start.isnumeric():
            self.start = int(self.start)
            return True
        else:
            return False

    # extract info out of what was returned
    # sp = hash indexed by node
    # d  = data returned from prometheus
    # n  = what name to give this data
    # {'metric': {'__name__': 'cgroup_memory_total_bytes',
    #             'instance': 'node2906.inband:9306',
    #             'job': 'cgroup_exporter',
    #             'jobid': '10845322'},
    #             'values': [[1629592582, '536870912000']]}
    # or
    # {'metric': {'instance': 'node2906.inband:9306',
    #             'job': 'cgroup_exporter',
    #             'jobid': '10845322'},
    #             'value': [1629592575, '190540828672']}
    def get_data_out(self, d, n):
        if 'data' in d:
            j = d['data']['result']
            for i in j:
                node = i['metric']['instance'].split(':')[0]
                minor = i['metric'].get('minor_number', None)
                if 'value' in i:
                    v = i['value'][1]
                if 'values' in i:
                    v = i['values'][0][0]
                # trim unneeded precision. Values below 1 keep three decimals
                # since the detailed GPU metrics are fractions (0.643 is 64.3%).
                # Upstream only does this below 0.1 which limits those metrics
                # to steps of 10%.
                if '.' in v:
                    x = float(v)
                    if x >= 1:
                        v = round(x, 1)
                    else:
                        v = round(x, 3)
                else:
                    v = int(v)
                if node not in self.sp_node:
                    self.sp_node[node] = {}
                if minor is not None:
                    if n not in self.sp_node[node]:
                        self.sp_node[node][n] = {}
                    self.sp_node[node][n][minor] = v
                else:
                    self.sp_node[node][n] = v

    # Run a query and store the result under the name "where". The query is a
    # format string with the fields {jobid} and {diff}. Note that at ORCD the
    # Prometheus series do not carry a "cluster" label so, unlike upstream, the
    # queries do not filter on it. If optional is True then a failed query is
    # not fatal and False is returned.
    def get_data(self, where, query, query_sluid=False, optional=False):
        # run a query against prometheus
        def __run_query(q, start=None, end=None, time=None, step=2*SAMPLING_PERIOD):
            params = { 'query': q, }
            if start:
                params['start'] = start
                params['end'] = end
                params['step'] = step
                qstr = 'query_range'
            else:
                qstr = 'query'
                if time:
                    params['time'] = time
            response = requests.get(f'{self.prom_server}/api/v1/{qstr}', params)
            return response.json()

        def fail(msg):
            if optional:
                self.debug_print(msg)
                return False
            self.error(msg)

        expanded_query = query.format(jobid=self.jobidraw, diff=int(self.diff))
        if query_sluid and self.sluid:
            expanded_query += " or " + query.format(jobid=self.sluid, diff=int(self.diff))
        self.debug_print("query=%s, time=%s" % (expanded_query, self.end))
        try:
            j = __run_query(expanded_query, time=self.end)
        except Exception as e:
            return fail("ERROR: Failed to query jobstats database, got error: %s:" % e)
        self.debug_print("query result=%s" % j)
        if j["status"] == 'success':
            self.get_data_out(j, where)
            return True
        elif j["status"] == 'error':
            return fail("ERROR: Failed to get run query %s with time %s, error: %s" % (expanded_query,
                                                                                       self.end,
                                                                                       j["error"]))
        else:
            return fail("ERROR: Unknown result when running query %s with time %s. Full output: %s" % (expanded_query,
                                                                                                       self.end,
                                                                                                       j))

    @staticmethod
    def internal_detailed_metric_name(metric: str, operation: str) -> str:
        return f"{metric}_{operation[:3]}"

    @staticmethod
    def gpu_query_string(op: str, expr: str) -> str:
        """Return the format string of a query that applies op to the GPU
           series of the job over its run time. In expr each metric name must
           be followed by the placeholder <sel>. The series of the job are
           selected by the jobid label (GPU_EXPORTER_JOBID = True, fast) or
           else by joining with nvidia_gpu_jobId (slow for long jobs)."""
        if c.GPU_EXPORTER_JOBID:
            return op + "((" + expr.replace("<sel>", "{{jobid='{jobid}'}}") + ")[{diff}s:])"
        return op + "(((" + expr.replace("<sel>", "") + ") and nvidia_gpu_jobId == {jobid})[{diff}s:])"

    def gpu_metric_query_string(self, metric: str, op: str) -> str:
        """Generate the Prometheus query string for the given metric and operation.
           The first three letters of 'operation' is appended to the metric name
           when a DetailedGpuMetric object is created."""
        if op not in ("avg_over_time", "max_over_time", "min_over_time", "stddev_over_time"):
            self.error(f"Operation {op} is not supported.")
        metrics = {}
        if c.GPU_METRICS_EXPORTER == "NVML":
            metrics["duty_cycle"]              = "nvidia_gpu_duty_cycle"
            metrics["memory_used_bytes"]       = "nvidia_gpu_memory_used_bytes"
            metrics["memory_total_bytes"]      = "nvidia_gpu_memory_total_bytes"
            metrics["sm_util_percent"]         = "nvidia_gpu_sm_util_percent"
            metrics["sm_occupancy_percent"]    = "nvidia_gpu_sm_occupancy_percent"
            metrics["any_tensor_util_percent"] = "nvidia_gpu_any_tensor_util_percent"
            metrics["fp16_util_percent"]       = "nvidia_gpu_fp16_util_percent"
            metrics["fp32_util_percent"]       = "nvidia_gpu_fp32_util_percent"
            metrics["fp64_util_percent"]       = "nvidia_gpu_fp64_util_percent"
            metrics["integer_util"]            = "nvidia_gpu_integer_util"
            metrics["dram_bw_util_percent"]    = "nvidia_gpu_dram_bw_util_percent"
            metrics["pcie_rx_per_sec"]         = "nvidia_gpu_pcie_rx_per_sec"
            metrics["pcie_tx_per_sec"]         = "nvidia_gpu_pcie_tx_per_sec"
            metrics["nvlink_total_rx_per_sec"] = "nvidia_gpu_nvlink_total_rx_per_sec"
            metrics["nvlink_total_tx_per_sec"] = "nvidia_gpu_nvlink_total_tx_per_sec"
            metrics["temperature_celsius"]     = "nvidia_gpu_temperature_celsius"
            metrics["power_usage_milliwatts"]  = "nvidia_gpu_power_usage_milliwatts"
        elif c.GPU_METRICS_EXPORTER == "DCGM":
            # not fully supported upstream; kept for parity
            metrics["duty_cycle"]              = "DCGM_FI_DEV_GPU_UTIL"
            metrics["memory_used_MiB"]         = "DCGM_FI_DEV_FB_USED"
            metrics["memory_total_MiB"]        = "DCGM_FI_DEV_FB_TOTAL"
            metrics["sm_util_percent"]         = "DCGM_FI_PROF_SM_ACTIVE"
            metrics["sm_occupancy_percent"]    = "DCGM_FI_PROF_SM_OCCUPANCY"
            metrics["any_tensor_util_percent"] = "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE"
            metrics["fp16_util_percent"]       = "DCGM_FI_PROF_PIPE_FP16_ACTIVE"
            metrics["fp32_util_percent"]       = "DCGM_FI_PROF_PIPE_FP32_ACTIVE"
            metrics["fp64_util_percent"]       = "DCGM_FI_PROF_PIPE_FP64_ACTIVE"
            metrics["integer_util"]            = "DCGM_FI_PROF_PIPE_INT_ACTIVE"
            metrics["dram_active"]             = "DCGM_FI_PROF_DRAM_ACTIVE"
            metrics["pcie_rx_per_sec"]         = "DCGM_FI_PROF_PCIE_RX_BYTES"
            metrics["pcie_tx_per_sec"]         = "DCGM_FI_PROF_PCIE_TX_BYTES"
            metrics["nvlink_total_rx_per_sec"] = "DCGM_FI_PROF_NVLINK_RX_BYTES"
            metrics["nvlink_total_tx_per_sec"] = "DCGM_FI_PROF_NVLINK_TX_BYTES"
            metrics["temperature_celsius"]     = "DCGM_FI_DEV_GPU_TEMP"
            metrics["power_usage_milliwatts"]  = "DCGM_FI_DEV_POWER_USAGE"
        if metric not in metrics:
            self.error(f"{metric} is not valid for exporter {c.GPU_METRICS_EXPORTER}.")
        return Jobstats.gpu_query_string(op, metrics[metric] + "<sel>")

    # Query Prometheus for the job. With no arguments all of the metrics are
    # fetched, otherwise only the named ones (e.g., "used_memory", "cpus",
    # "gpu_utilization", "sm_util_percent_avg").
    def get_job_stats(self, *args):
        # query CPU and Memory utilization data
        cgroup = "{{jobid='{jobid}',step='',task=''}}"
        if not args or "total_memory" in args:
            self.get_data('total_memory', "max_over_time(cgroup_memory_total_bytes" + cgroup + "[{diff}s])", True)
        if not args or "used_memory" in args:
            # shared memory (e.g., /dev/shm) is included when the cgroup exporter provides it
            self.get_data('used_memory', "max_over_time(((cgroup_memory_rss_bytes" + cgroup
                                         + "+cgroup_memory_shmem_bytes" + cgroup
                                         + ") or 1*cgroup_memory_rss_bytes" + cgroup + ")[{diff}s:])", True)
        if not args or "total_time" in args:
            self.get_data('total_time', "max_over_time(cgroup_cpu_total_seconds" + cgroup + "[{diff}s])", True)
        if not args or "cpus" in args:
            self.get_data('cpus', "max_over_time(cgroup_cpus" + cgroup + "[{diff}s])", True)

        # and now GPUs
        if self.gpus:
            if not args or "gpu_total_memory" in args:
                self.get_data('gpu_total_memory',
                              Jobstats.gpu_query_string("max_over_time", "nvidia_gpu_memory_total_bytes<sel>"))
            if not args or "gpu_used_memory" in args:
                self.get_data('gpu_used_memory',
                              Jobstats.gpu_query_string("max_over_time", "nvidia_gpu_memory_used_bytes<sel>"))
            if not args or "gpu_utilization" in args:
                # MIG slices do not report a duty cycle so fall back to the graphics utilization
                self.get_data('gpu_utilization',
                              Jobstats.gpu_query_string("avg_over_time",
                                                        "nvidia_gpu_duty_cycle<sel> or "
                                                        "(nvidia_gpu_graphics_util_percent<sel> * 100)"))

            # detailed GPU metrics
            if c.GPU_METRICS and c.GPU_METRICS_EXPORTER == "None":
                self.error('Must set GPU_METRICS_EXPORTER to "NVML" or "DCGM" when GPU_METRICS is not empty.')
            for settings in c.GPU_METRICS.values():
                metric = settings["metric"]
                operation = settings["operation"]
                name_ss = Jobstats.internal_detailed_metric_name(metric, operation)
                if args and name_ss not in args:
                    continue
                if self.json_or_base64 and not settings["write_to_db"]:
                    continue
                prom_query_str = self.gpu_metric_query_string(metric, operation)
                # These metrics are extras. If a query fails (e.g., it times
                # out) then give up on the remaining ones and still produce
                # the report.
                if not self.get_data(name_ss, prom_query_str, optional=True):
                    self.debug_print("skipping the remaining detailed GPU metrics")
                    break

    def parse_stats(self):
        sp_node = self.sp_node
        if len(sp_node) == 0:
            if self.diff < SAMPLING_PERIOD:
                cmd = ["seff", f"{self.jobid}"]
                try:
                    seff = subprocess.check_output(cmd, stderr=DEVNULL, env=self.__slurm_env()).decode("utf-8")
                except Exception as e:
                    self.error(f"No job statistics are available ({e}).")
                else:
                    print("\nRun time is very short so only providing seff output:\n")
                    print(seff)
                    self.error("")
            else:
                self.error(f"No data was found for job {self.jobid}. This is probably because it is too old\n"
                          + "or it expired from Jobstats database. If you are not running this command on the\n"
                          + "cluster where the job was run then use the -c option to specify the cluster.\n"
                          +f'If the run time was very short then try running "seff {self.jobid}".')

        # ORCD: the number of cores per node comes from the Slurm allocation
        # (ncpus / nnodes) instead of the cgroup_cpus metric. When the object
        # was created without sacct (jobidraw given) fall back to the metric.
        try:
            cores_per_node = int(self.ncpus) / int(self.nnodes)
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            cores_per_node = None

        # cpu utilization
        total = 0
        total_used = 0
        total_cores = 0
        self.cpu_util_error_code = 0
        self.cpu_util__node_used_alloc_cores = []
        for n, d in sp_node.items():
            if 'total_time' in d and 'cpus' in d:
                used  = d['total_time']
                cores = d['cpus'] if cores_per_node is None else cores_per_node
                alloc = self.diff * cores
                total += alloc
                total_used += used
                total_cores += cores
                self.cpu_util__node_used_alloc_cores.append((n, used, alloc, cores))
            else:
                self.cpu_util_error_code = 1
                self.cpu_util__node_used_alloc_cores.append((n, None, None, None))
                break
        if self.cpu_util_error_code == 0:
            if total_used > total:
                self.cpu_util_error_code = 2
            if total == 0:
                self.cpu_util_error_code = 3
        self.cpu_util_total__used_alloc_cores = (total_used, total, total_cores)

        # cpu memory
        total = 0
        total_used = 0
        total_cores = 0
        self.cpu_mem_error_code = 0
        self.cpu_mem__node_used_alloc_cores = []
        for n, d in sp_node.items():
            if 'used_memory' in d and 'total_memory' in d and 'cpus' in d:
                used  = d['used_memory']
                alloc = d['total_memory']
                cores = d['cpus'] if cores_per_node is None else cores_per_node
                total += alloc
                total_used += used
                total_cores += cores
                self.cpu_mem__node_used_alloc_cores.append((n, used, alloc, cores))
            else:
                self.cpu_mem_error_code = 1
                self.cpu_mem__node_used_alloc_cores.append((n, None, None, None))
                break
        if self.cpu_mem_error_code == 0:
            if total_used > total:
                self.cpu_mem_error_code = 2
            if total == 0:
                self.cpu_mem_error_code = 3
        self.cpu_mem_total__used_alloc_cores = (total_used, total, total_cores)

        if self.gpus:
            # gpu utilization
            overall = 0
            overall_gpu_count = 0
            self.gpu_util_error_code = 0
            self.gpu_util__node_util_index = []
            for n, d in sp_node.items():
                if 'gpu_utilization' in d:
                    gpus = sorted(d['gpu_utilization'].keys())
                    for g in gpus:
                        util = d['gpu_utilization'][g]
                        overall += util
                        overall_gpu_count += 1
                        self.gpu_util__node_util_index.append((n, util, g))
                else:
                    if self.is_mig_job():
                        self.gpu_util__node_util_index.append((n, None, "#"))
                    else:
                        self.gpu_util_error_code = 1
                        self.gpu_util__node_util_index.append((n, None, None))
                        break
            self.gpu_util_total__util_gpus = (overall, overall_gpu_count)

            # detailed gpu metrics
            self.detailed_gpu_metrics = []
            for name, settings in c.GPU_METRICS.items():
                gm = DetailedGpuMetric(name, settings, self.is_mig_job())
                gm.parse(sp_node)
                self.detailed_gpu_metrics.append(gm)

            # gpu memory
            overall = 0
            overall_total = 0
            self.gpu_mem_error_code = 0
            self.gpu_mem__node_used_total_index = []
            for n, d in sp_node.items():
                if 'gpu_used_memory' in d and 'gpu_total_memory' in d:
                    gpus = sorted(d['gpu_total_memory'].keys())
                    for g in gpus:
                        used  = d['gpu_used_memory'][g]
                        total = d['gpu_total_memory'][g]
                        overall += used
                        overall_total += total
                        self.gpu_mem__node_used_total_index.append((n, used, total, g))
                else:
                    self.gpu_mem_error_code = 1
                    self.gpu_mem__node_used_total_index.append((n, None, None, None))
                    break
            if self.gpu_mem_error_code == 0:
                if overall > overall_total:
                    self.gpu_mem_error_code = 2
                if overall_total == 0:
                    self.gpu_mem_error_code = 3
            self.gpu_mem_total__used_alloc = (overall, overall_total)

    def __str__(self, compact=False):
        js_data = {'nodes': self.sp_node, 'total_time': self.diff, 'gpus': self.gpus}
        if compact:
            return json.dumps(js_data, separators=(',', ':'))
        else:
            return json.dumps(js_data, sort_keys=True, indent=4)

    def is_retained(self) -> bool:
        """Returns true if the job is expected to be found in the Prometheus
           database. Job data is typically purged after N days."""
        if self.end and isinstance(self.end, (float, int)) and self.end > 1_000_000_000:
            job_end = datetime.datetime.fromtimestamp(self.end)
            now = datetime.datetime.now()
            days = getattr(c, "PROM_RETENTION_DAYS", 365)
            return job_end > now - datetime.timedelta(days=days)
        return False

    def is_mig_job(self) -> bool:
        """Returns true if the job ran on a MIG node."""
        mig_nodes_1 = getattr(c, "MIG_NODES_1", [])
        mig_nodes_2 = getattr(c, "MIG_NODES_2", [])
        return any(node in mig_nodes_1 or node in mig_nodes_2
                   for node in self.sp_node)

    def report_job_json(self, encode):
        data = self.__str__(encode)
        if encode:
            if self.diff < 2 * SAMPLING_PERIOD:
                return 'Short'
            elif len(self.sp_node) == 0:
                return 'None'
            else:
                return base64.b64encode(gzip.compress(data.encode('ascii'))).decode('ascii')
        else:
            return data
