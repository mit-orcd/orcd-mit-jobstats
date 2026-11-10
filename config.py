##########################
## JOBSTATS CONFIG FILE ##
##########################

# prometheus server address and port
# PROM_SERVER = "http://admin001.inband:9090"
PROM_SERVER = "http://vcore001.inband:9090"

# Number of days that Prometheus keeps the data. It is only used by notes that
# call self.js.is_retained() (e.g., to show a URL only while the data exists).
PROM_RETENTION_DAYS = 365

# number of seconds between measurements
SAMPLING_PERIOD = 30

# How the GPU series of a job are selected in Prometheus.
#   False: join each metric with nvidia_gpu_jobId (works with every version of
#          the exporter but it is slow for long jobs)
#   True:  use the jobid label of the GPU metrics (much faster). This needs
#          version 0.2.2+ of https://github.com/plazonic/nvidia_gpu_prometheus_exporter
#          on ALL GPU nodes, otherwise GPU jobs on the older nodes show no GPU data.
# To check, compare the number of nodes that report each form for running jobs
# (the two Prometheus queries should give the same count):
#   count(count by (instance) (nvidia_gpu_jobId > 0))
#   count(count by (instance) (nvidia_gpu_duty_cycle{jobid=~".+",jobid!="0"}))
GPU_EXPORTER_JOBID = False

# Nodes with MIG GPUs, named as they appear in the report (e.g., "node1234.inband").
# The GPU utilization of jobs on these nodes is reported as unknown instead of
# as an error. Leave empty if not needed.
MIG_NODES_1 = set()
MIG_NODES_2 = set()

# threshold values for red versus black notes
GPU_UTIL_RED   = 15  # percentage
GPU_UTIL_BLACK = 25  # percentage
CPU_UTIL_RED   = 65  # percentage
CPU_UTIL_BLACK = 80  # percentage
TIME_EFFICIENCY_RED   = 40  # percentage
TIME_EFFICIENCY_BLACK = 70  # percentage
MIN_MEMORY_USAGE      = 70  # percentage
MIN_RUNTIME_SECONDS   = 10 * SAMPLING_PERIOD  # seconds

# translate cluster names in Slurm DB to informal names
CLUSTER_TRANS = {}  # if no translations then use an empty dictionary
CLUSTER_TRANS_INV = dict(zip(CLUSTER_TRANS.values(), CLUSTER_TRANS.keys()))

# maximum number of characters to display in jobname
MAX_JOBNAME_LEN = 64

# default CPU memory per core in bytes for each cluster
# if unsure then use memory per node divided by cores per node
DEFAULT_MEM_PER_CORE = {"":3355443200}

# number of CPU-cores per node for each cluster
# this will eventually be replaced with explicit values for each node
CORES_PER_NODE = {"":48}

# When sacct does not find the job in the default Slurm accounting DB (e.g. archived
# jobs), retry sacct and seff with SLURM_CONF pointing here. Use the path to slurm.conf,
# or a directory that contains slurm.conf. None disables this fallback. Prometheus data
# still comes only from PROM_SERVER.
SLURM_ARCHIVE_CONF = "/home/systems/slurm/archive/etc/slurm"


################################################################################
##                 D E T A I L E D    G P U    M E T R I C S                  ##
##                                                                            ##
##  Per-GPU metrics beyond utilization and memory. They are collected by      ##
##  version 0.2.3+ of the NVIDIA GPU exporter (NVML) and only exist for       ##
##  Hopper GPUs and later (e.g., H100, H200). A metric that is not found in   ##
##  Prometheus for a job is left out of the report. Each metric adds one      ##
##  Prometheus query per GPU job so remove the ones that are not wanted.      ##
##                                                                            ##
##  metric:       sm_util_percent, sm_occupancy_percent,                      ##
##                any_tensor_util_percent, integer_util, fp16_util_percent,   ##
##                fp32_util_percent, fp64_util_percent, dram_bw_util_percent, ##
##                duty_cycle, memory_used_bytes, power_usage_milliwatts,      ##
##                temperature_celsius, pcie_rx_per_sec, pcie_tx_per_sec,      ##
##                nvlink_total_rx_per_sec, nvlink_total_tx_per_sec            ##
##  operation:    avg_over_time, max_over_time, min_over_time,                ##
##                stddev_over_time                                            ##
##  show_overall: add a meter to "Overall Utilization" (percentages only)     ##
##  show_per_gpu: show the value of each GPU in "Detailed Utilization" (as    ##
##                tables when four or more metrics are shown, with the        ##
##                metrics named PCIe or NVLink in a second table)             ##
##  write_to_db:  include the metric in the output of "jobstats -j/-b"        ##
##  long_name:    heading used when the metrics are not shown as tables       ##
##                                                                            ##
##  To turn the feature off use GPU_METRICS = {} with no entries.             ##
################################################################################
GPU_METRICS_EXPORTER = "NVML"  # choices are "None" or "NVML" (or "DCGM")
# URL shown below the metrics to explain them (use "" for none)
GPU_METRICS_DOCS_URL = "https://orcd-docs.mit.edu/running-jobs/overview/#retrieving-job-stats/"
GPU_METRICS = {}
GPU_METRICS["SM"] = {"metric": "sm_util_percent",
                     "operation": "avg_over_time",
                     "show_overall": True,
                     "show_per_gpu": True,
                     "write_to_db": True,
                     "long_name": "Streaming Multiprocessor (SM) utilization"}
GPU_METRICS["OCC"] = {"metric": "sm_occupancy_percent",
                      "operation": "avg_over_time",
                      "show_overall": True,
                      "show_per_gpu": True,
                      "write_to_db": True,
                      "long_name": "Occupancy"}
GPU_METRICS["TC"] = {"metric": "any_tensor_util_percent",
                     "operation": "avg_over_time",
                     "show_overall": True,
                     "show_per_gpu": True,
                     "write_to_db": True,
                     "long_name": "Tensor Core (TC) utilization"}
GPU_METRICS["INT"] = {"metric": "integer_util",
                      "operation": "avg_over_time",
                      "show_overall": True,
                      "show_per_gpu": True,
                      "write_to_db": True,
                      "long_name": "INT utilization"}
GPU_METRICS["FP16 Max"] = {"metric": "fp16_util_percent",
                           "operation": "max_over_time",
                           "show_overall": True,
                           "show_per_gpu": True,
                           "write_to_db": True,
                           "long_name": "FP16 (max) utilization"}
GPU_METRICS["FP32 Avg"] = {"metric": "fp32_util_percent",
                           "operation": "avg_over_time",
                           "show_overall": True,
                           "show_per_gpu": True,
                           "write_to_db": True,
                           "long_name": "FP32 (avg) utilization"}
GPU_METRICS["FP64 Max"] = {"metric": "fp64_util_percent",
                           "operation": "max_over_time",
                           "show_overall": True,
                           "show_per_gpu": True,
                           "write_to_db": True,
                           "long_name": "FP64 (max) utilization"}
GPU_METRICS["DRAM BW"] = {"metric": "dram_bw_util_percent",
                          "operation": "avg_over_time",
                          "show_overall": True,
                          "show_per_gpu": True,
                          "write_to_db": True,
                          "long_name": "DRAM Bandwidth utilization"}
GPU_METRICS["Power"] = {"metric": "power_usage_milliwatts",
                        "operation": "avg_over_time",
                        "show_overall": False,
                        "show_per_gpu": True,
                        "write_to_db": True,
                        "long_name": "Power Usage"}
GPU_METRICS["Temp"] = {"metric": "temperature_celsius",
                       "operation": "avg_over_time",
                       "show_overall": False,
                       "show_per_gpu": True,
                       "write_to_db": True,
                       "long_name": "Temperature"}
GPU_METRICS["PCIe Recv"] = {"metric": "pcie_rx_per_sec",
                            "operation": "avg_over_time",
                            "show_overall": False,
                            "show_per_gpu": True,
                            "write_to_db": True,
                            "long_name": "Data Received by GPU over PCIe"}
GPU_METRICS["PCIe Sent"] = {"metric": "pcie_tx_per_sec",
                            "operation": "avg_over_time",
                            "show_overall": False,
                            "show_per_gpu": True,
                            "write_to_db": True,
                            "long_name": "Data Transmitted from GPU over PCIe"}
GPU_METRICS["NVLink Recv"] = {"metric": "nvlink_total_rx_per_sec",
                              "operation": "avg_over_time",
                              "show_overall": False,
                              "show_per_gpu": True,
                              "write_to_db": True,
                              "long_name": "Data Received by GPU over NVLink"}
GPU_METRICS["NVLink Sent"] = {"metric": "nvlink_total_tx_per_sec",
                              "operation": "avg_over_time",
                              "show_overall": False,
                              "show_per_gpu": True,
                              "write_to_db": True,
                              "long_name": "Data Transmitted from GPU over NVLink"}


#########################################################################################
##                               C U S T O M    N O T E S                              ##
##                                                                                     ##
## Be sure to work from the examples. Pay attention to the different quote characters  ##
## when f-strings are involved (each note fragment is passed to eval(); use f'...' if ##
## you need literal double quotes inside the fragment).                                ##
#########################################################################################
NOTES = []

# zero GPU utilization (single GPU jobs)
condition = 'self.js.gpus and (self.js.diff > c.MIN_RUNTIME_SECONDS) and num_unused_gpus > 0 ' \
            'and self.js.gpus == 1'
note = ("This job did not use the GPU. Please resolve this " \
        "before running additional jobs. Wasting " \
        "resources prevents other users from getting their work done " \
        "and it causes your subsequent jobs to have a lower priority. " \
        "Is the code GPU-enabled? " \
        "Please consult the documentation for the software. For more info:",
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "bold-red"
NOTES.append((condition, note, style))

# zero GPU utilization (multi-GPU jobs)
condition = 'self.js.gpus and (self.js.diff > c.MIN_RUNTIME_SECONDS) and num_unused_gpus > 0 ' \
            'and self.js.gpus > 1'
note = ('f"This job did not use {num_unused_gpus} of the {self.js.gpus} allocated GPUs. "' \
        '"Please resolve this before running additional jobs. "' \
        '"Wasting resources prevents other users from getting their work done "' \
        '"and it causes your subsequent jobs to have a lower priority. Is the "' \
        '"code capable of using multiple GPUs? Please consult the documentation for "' \
        '"the software. For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "bold-red"
NOTES.append((condition, note, style))

# low GPU utilization (ondemand and salloc)
condition = '(not zero_gpu) and self.js.gpus and (self.js.gpu_utilization <= c.GPU_UTIL_RED) ' \
            'and interactive_job and (self.js.diff / SECONDS_PER_HOUR > 12)'
note = ('f"The overall GPU utilization of this job is only {round(self.js.gpu_utilization)}%. "' \
        'f"This value is low compared to the cluster mean value of 50%. Please "' \
        "f'do not create \"salloc\" or OnDemand sessions for more than 12 hours unless you '" \
        'f"plan to work intensively during the entire period. For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "bold-red"
NOTES.append((condition, note, style))

# low GPU utilization (batch jobs)
condition = '(not zero_gpu) and self.js.gpus and (self.js.gpu_utilization <= c.GPU_UTIL_RED) ' \
            'and (not interactive_job)'
note = ('f"The overall GPU utilization of this job is only {round(self.js.gpu_utilization)}%. "' \
        '"This value is low compared to the cluster mean value of 50%. Please "' \
        '"investigate the reason for the low utilization. For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "bold-red"
NOTES.append((condition, note, style))

# low CPU utilization (black, more than one core)
condition = '(not zero_cpu) and (not self.js.gpus) and (self.js.cpu_efficiency <= c.CPU_UTIL_BLACK) ' \
            'and (self.js.cpu_efficiency > c.CPU_UTIL_RED) and int(self.js.ncpus) > 1'
note = ('f"The overall CPU utilization of this job is {ceff}%. This value "' \
        'f"is{somewhat}low compared to the target range of "' \
        'f"90% and above. Please investigate the reason for the low efficiency. "' \
        '"For instance, have you conducted a scaling analysis? For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "normal"
NOTES.append((condition, note, style))

# low CPU utilization (red, more than one core)
condition = '(not zero_cpu) and (not self.js.gpus) and (self.js.cpu_efficiency < c.CPU_UTIL_RED) ' \
            'and (int(self.js.ncpus) > 1)'
note = ('f"The overall CPU utilization of this job is {ceff}%. This value "' \
        'f"is{somewhat}low compared to the target range of "' \
        'f"90% and above. Please investigate the reason for the low efficiency. "' \
        '"For instance, have you conducted a scaling analysis? For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "bold-red"
NOTES.append((condition, note, style))

# low CPU utilization (black, serial job)
condition = '(not zero_cpu) and (not self.js.gpus) and (self.js.cpu_efficiency <= c.CPU_UTIL_BLACK) ' \
            'and (self.js.cpu_efficiency > c.CPU_UTIL_RED) and int(self.js.ncpus) == 1'
note = ('f"The overall CPU utilization of this job is {ceff}%. This value "' \
        'f"is{somewhat}low compared to the target range of "' \
        'f"90% and above. Please investigate the reason for the low efficiency. "' \
        '"For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "normal"
NOTES.append((condition, note, style))

# low CPU utilization (red, serial job)
condition = '(not zero_cpu) and (not self.js.gpus) and (self.js.cpu_efficiency < c.CPU_UTIL_RED) ' \
            'and (int(self.js.ncpus) == 1)'
note = ('f"The overall CPU utilization of this job is {ceff}%. This value "' \
        'f"is{somewhat}low compared to the target range of "' \
        'f"90% and above. Please investigate the reason for the low efficiency. "' \
        '"For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "bold-red"
NOTES.append((condition, note, style))

# out of memory
condition = 'self.js.state == "OUT_OF_MEMORY"'
note = ("This job failed because it needed more CPU memory than the amount that " \
        "was requested. The solution is to resubmit the job while " \
        "requesting more CPU memory by " \
        "modifying the --mem-per-cpu or --mem Slurm directive. For more info: ",
        "https://orcd-docs.mit.edu/running-jobs/requesting-resources/#memory")
style = "bold-red"
NOTES.append((condition, note, style))

# timeout
condition = 'self.js.state == "TIMEOUT"'
note = ("This job failed because it exceeded the time limit. If there are no " \
        "other problems then the solution is to increase the value of the " \
        "--time Slurm directive and resubmit the job. For more info:",
        "https://orcd-docs.mit.edu/running-jobs/overview")
style = "bold-red"
NOTES.append((condition, note, style))

# excessive run time limit (red)
condition = 'self.js.time_eff_violation and self.js.time_efficiency <= c.TIME_EFFICIENCY_RED'
note = ('f"This job only needed {self.js.time_efficiency}% of the requested time "' \
        'f"which was {self.human_seconds(SECONDS_PER_MINUTE * self.js.timelimitraw)}. "' \
        '"For future jobs, please request less time by modifying "' \
        '"the --time Slurm directive. This will "' \
        '"lower your queue times and allow the Slurm job scheduler to work more "' \
        '"effectively for all users. For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/overview")
style = "bold-red"
NOTES.append((condition, note, style))

# excessive run time limit (black)
condition = 'self.js.time_eff_violation and self.js.time_efficiency > c.TIME_EFFICIENCY_RED'
note = ('f"This job only needed {self.js.time_efficiency}% of the requested time "' \
        'f"which was {self.human_seconds(SECONDS_PER_MINUTE * self.js.timelimitraw)}. "' \
        '"For future jobs, please request less time by modifying "' \
        '"the --time Slurm directive. This will "' \
        '"lower your queue times and allow the Slurm job scheduler to work more "' \
        '"effectively for all users. For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/overview")
style = "normal"
NOTES.append((condition, note, style))

# somewhat low GPU utilization
condition = '(not zero_gpu) and self.js.gpus and (self.js.gpu_utilization < c.GPU_UTIL_BLACK) and ' \
            '(self.js.gpu_utilization > c.GPU_UTIL_RED) and (self.js.diff > c.MIN_RUNTIME_SECONDS)'
note = ('f"The overall GPU utilization of this job is {round(self.js.gpu_utilization)}%. "' \
        '"This value is somewhat low compared to the cluster mean value of 50%. For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "normal"
NOTES.append((condition, note, style))

# excess CPU memory
condition = '(not zero_gpu) and (not zero_cpu) and (self.js.cpu_memory_efficiency < c.MIN_MEMORY_USAGE) ' \
            'and (gb_per_core > (mpc / 1024**3) - 2) and (total > mpc) and gpu_show and ' \
            '(not self.js.partition == "datascience") and (not self.js.partition == "mig") and ' \
            '(self.js.state != "OUT_OF_MEMORY") and (cores_per_node < cpn) and ' \
            '(self.js.diff > c.MIN_RUNTIME_SECONDS)'
note = ('f"This job {opening} of the {self.cpu_memory_formatted(with_label=False)} "' \
        '"of total allocated CPU memory. "' \
        '"For future jobs, please allocate less memory by using a Slurm directive such "' \
        'f"as --mem-per-cpu={self.rounded_memory_with_safety(gb_per_core_used)}G or "' \
        'f"--mem={self.rounded_memory_with_safety(gb_per_node_used)}G. "' \
        '"This will reduce your queue times and make the resources available to "' \
        '"other users. For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "normal"
NOTES.append((condition, note, style))

# serial jobs wasting multiple cpu-cores
condition = '(self.js.nnodes == "1") and (int(self.js.ncpus) > 1) and (not self.js.gpus) and (serial_ratio > 0.85 ' \
            'and serial_ratio < 1.1)'
note = ('f"The CPU utilization of this job ({self.js.cpu_efficiency}%) is{approx}equal "' \
        '"to 1 divided by the number of allocated CPU-cores "' \
        'f"(1/{self.js.ncpus}={round(eff_if_serial, 1)}%). This suggests that you may be "' \
        '"running a code that can only use 1 CPU-core. If this is true then "' \
        '"allocating more than 1 CPU-core is wasteful. Please consult the "' \
        '"documentation for the software to see if it is parallelized. For more info:"',
        "https://orcd-docs.mit.edu/running-jobs/application-analysis/")
style = "normal"
NOTES.append((condition, note, style))

# job ran in the test queue
condition = '"test" in self.js.qos or "debug" in self.js.qos'
note = ('f"This job ran in the {self.js.qos} QOS. Each user can only run a small number of "' \
        '"jobs simultaneously in this QOS. For more info:"',
        "https://orcd-docs.mit.edu")
style = "normal"
NOTES.append((condition, note, style))

# detailed GPU metrics: the overall value of a metric that has "show_overall": True
# is available in the notes as self.gm_overall["<NAME>-util"], for example:
# condition = '("TC-util" in self.gm_overall) and (self.gm_overall["TC-util"] == 0) ' \
#             'and (self.js.diff > c.MIN_RUNTIME_SECONDS)'
# note = ("The Tensor Core utilization of the job was 0%. Usually AI codes use the Tensor Cores.")
# style = "normal"
# NOTES.append((condition, note, style))

# more details for della
# condition = '(self.js.cluster == "della")'
# note = ("For additional job metrics including metrics plotted against time:",
#         "https://orcd.mit.edu")
# style = "normal"
# NOTES.append((condition, note, style))

# more details for adroit
# condition = '(self.js.cluster == "adroit")'
# note = ("For additional job metrics including metrics plotted against time:",
#         "https://orcd.mit.edu")
# style = "normal"
# NOTES.append((condition, note, style))

# example of a simple note that is always displayed
condition = 'True'
note = "Have a nice day!"
style = "normal"
NOTES.append((condition, note, style))
