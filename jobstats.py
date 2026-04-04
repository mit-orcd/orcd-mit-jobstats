import csv
import os
import subprocess
import sys
import time
import requests
import json
import base64
import gzip
import syslog
import config as c

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


# class that gets and holds per job prometheus statistics
class Jobstats:
    # initialize basic job stats, can be called either with those stats
    # provided and if not it will fetch them
    def __init__(self,
                 jobid=None,
                 jobidraw=None,
                 start=None,
                 end=None,
                 gpus=None,
                 cluster=None,
                 prom_server=None,
                 debug=False,
                 debug_syslog=False,
                 force_recalc=False,
                 debug_slurm_env=None):
        self.cluster = cluster
        self.prom_server = prom_server
        self.debug = debug
        self.debug_syslog = debug_syslog
        self.force_recalc = force_recalc
        # With debug enabled only: narrow which Slurm accounting env sacct uses (see CLI).
        self.debug_slurm_env = debug_slurm_env if debug else None
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
        self.debug_print("jobid=%s, jobidraw=%s, start=%s, end=%s, gpus=%s, diff=%s, cluster=%s, data=%s, timelimitraw=%s" % 
            (self.jobid,self.jobidraw,self.start,self.end,self.gpus,self.diff,self.cluster,self.data,self.timelimitraw))
        if self.data is not None and self.data.startswith('JS1:') and len(self.data) > 10:
            try:
                t = json.loads(gzip.decompress(base64.b64decode(self.data[4:])))
                self.sp_node = t["nodes"]
            except Exception as e:
                print("ERROR: %s" %e)
        if not self.sp_node:
            # call prometheus to get detailed statistics (if long enough)
            if self.diff >= 2 * SAMPLING_PERIOD:
                self.get_job_stats()
        self.parse_stats()

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
                    "jobidraw=%s, start=%s, end=%s, cluster=%s, tres=%s, data=%s, user=%s, account=%s, state=%s, timelimit=%s, nodes=%s, ncpus=%s, reqmem=%s, qos=%s, partition=%s, jobname=%s"
                    % (
                        self.jobidraw,
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
    #{'metric': {'__name__': 'cgroup_memory_total_bytes', 'cluster': 'stellar', 'instance': 'stellar-m02n30:9306', 'job': 'Stellar Nodes', 'jobid': '50783'}, 'values': [[1629592582, '536870912000']]}
    # or
    #{'metric': {'cluster': 'stellar', 'instance': 'stellar-m06n4:9306', 'job': 'Stellar Nodes', 'jobid': '50783'}, 'value': [1629592575, '190540828672']}
    def get_data_out(self, d, n):
        if 'data' in d:
            j = d['data']['result']
            for i in j:
                node=i['metric']['instance'].split(':')[0]
                minor = i['metric'].get('minor_number', None)
                if 'value' in i:
                    v=i['value'][1]
                if 'values' in i:
                    v=i['values'][0][0]
                # trim unneeded precision
                if '.' in v:
                    v = round(float(v), 1)
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

    def get_data(self, where, query):
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
            response = requests.get('{0}/api/v1/{1}'.format(self.prom_server, qstr), params)
            return response.json()
        
        expanded_query = query % (self.jobidraw, self.diff)
        self.debug_print("query=%s, time=%s" % (expanded_query,self.end))
        try:
            j = __run_query(expanded_query, time=self.end)
        except Exception as e:
            self.error("ERROR: Failed to query jobstats database, got error: %s:" % e)
        self.debug_print("query result=%s" % j)
        if j["status"] == 'success':
            self.get_data_out(j, where)
        elif j["status"] == 'error':
            self.error("ERROR: Failed to get run query %s with time %s, error: %s" % (expanded_query, self.end, j["error"]))
        else:
            self.error("ERROR: Unknown result when running query %s with time %s, full output: %s" %(expanded_query, self.end, j))

    def get_job_stats(self):
        # query CPU and Memory utilization data
        self.get_data('total_memory', "max_over_time(cgroup_memory_total_bytes{jobid='%s',step='',task=''}[%ds])")
        self.get_data('used_memory', "max_over_time(cgroup_memory_rss_bytes{jobid='%s',step='',task=''}[%ds])")
        self.get_data('total_time', "max_over_time(cgroup_cpu_total_seconds{jobid='%s',step='',task=''}[%ds])")
        self.get_data('cpus', "max_over_time(cgroup_cpus{jobid='%s',step='',task=''}[%ds])")

        # and now GPUs
        if self.gpus:
            self.get_data('gpu_total_memory', "max_over_time((nvidia_gpu_memory_total_bytes{} and nvidia_gpu_jobId == %s)[%ds:])")
            self.get_data('gpu_used_memory', "max_over_time((nvidia_gpu_memory_used_bytes{} and nvidia_gpu_jobId == %s)[%ds:])")
            self.get_data('gpu_utilization', "avg_over_time((nvidia_gpu_duty_cycle{} and nvidia_gpu_jobId == %s)[%ds:])")

    def parse_stats(self):
        sp_node = self.sp_node

        if len(sp_node) == 0:
            if self.diff < SAMPLING_PERIOD:
                cmd = ["seff", f"{self.jobid}"]
                try:
                    env = os.environ.copy()
                    if self._subprocess_slurm_env:
                        env.update(self._subprocess_slurm_env)
                    seff = subprocess.check_output(cmd, stderr=DEVNULL, env=env).decode("utf-8")
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

        # cpu utilization
        total = 0
        total_used = 0
        total_cores = 0
        self.cpu_util_error_code = 0
        self.cpu_util__node_used_alloc_cores = []
        cores_per_node = int(self.ncpus) / int(self.nnodes)
        for n in sp_node:
            d = sp_node[n]
            if 'total_time' in d and 'cpus' in d:
                used  = sp_node[n]['total_time']
                cores = cores_per_node
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
        for n in sp_node:
            d = sp_node[n]
            if 'used_memory' in d and 'total_memory' in d and 'cpus' in d:
                used  = sp_node[n]['used_memory']
                alloc = sp_node[n]['total_memory']
                cores = cores_per_node
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
            for n in sp_node:
                d = sp_node[n]
                if 'gpu_utilization' in d:
                    gpus = list(d['gpu_utilization'].keys())
                    gpus.sort()
                    for g in gpus:
                        util = d['gpu_utilization'][g]
                        overall += util
                        overall_gpu_count += 1
                        self.gpu_util__node_util_index.append((n, util, g))
                else:
                    self.gpu_util_error_code = 1
                    self.gpu_util__node_util_index.append((n, None, None))
                    break
            self.gpu_util_total__util_gpus = (overall, overall_gpu_count)

            # gpu memory
            overall = 0
            overall_total = 0
            self.gpu_mem_error_code = 0
            self.gpu_mem__node_used_total_index = []
            for n in sp_node:
                d = sp_node[n]
                if 'gpu_used_memory' in d and 'gpu_total_memory' in d:
                    gpus = list(d['gpu_total_memory'].keys())
                    gpus.sort()
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
                    self.gpu_mem_error_code == 2
                if overall_total == 0:
                    self.gpu_mem_error_code == 3
            self.gpu_mem_total__used_alloc = (overall, overall_total)

    def __str__(self, compact=False):
        js_data = {'nodes': self.sp_node, 'total_time': self.diff, 'gpus': self.gpus}
        if compact:
            return json.dumps(js_data, separators=(',', ':'))
        else:
            return json.dumps(js_data, sort_keys=True, indent=4)

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
