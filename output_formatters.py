import datetime
import math
import sys
from abc import ABC, abstractmethod
from textwrap import TextWrapper
from typing import Optional, Tuple

import config as c
from jobstats import Jobstats
try:
    from blessed import Terminal
    blessed_is_available = True
except ModuleNotFoundError:
    blessed_is_available = False


# conversion factors
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


def _if_encodable(text: str, fallback: str) -> str:
    """Return text if stdout can encode it, else the ASCII fallback. This
       keeps the report from failing with UnicodeEncodeError when stdout is
       ASCII (e.g., Python 3.6 with LANG=C or run from a systemd unit)."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeError, LookupError):
        return fallback
    return text


class BaseFormatter(ABC):
    """Abstract base class for custom output formatters. An output
       formatter is used to print the job report."""

    def __init__(self, js: Jobstats) -> None:
        self.js = js
        # overall value of each detailed GPU metric shown in the "Overall
        # Utilization" section, keyed by "<NAME>-util" for use in the notes
        self.gm_overall = {}

    @abstractmethod
    def output(self, no_color: bool=True) -> str:
        pass

    @abstractmethod
    def output_metadata(self) -> str:
        pass

    @abstractmethod
    def output_overall_cpu_util(self) -> str:
        pass

    @abstractmethod
    def output_overall_cpu_memory_usage(self) -> str:
        pass

    @abstractmethod
    def output_overall_gpu_util(self) -> str:
        pass

    @abstractmethod
    def output_overall_gpu_memory_usage(self) -> str:
        pass

    @staticmethod
    def human_bytes(size: int, decimal_places: int = 1, spaces: str = "") -> str:
        if size is None:
            return "0B"
        size = float(size)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
            if size < 1024:
                break
            size /= 1024
        return f"{size:.{decimal_places}f}{spaces}{unit}"

    @staticmethod
    def human_seconds(seconds: int) -> str:
        hour = seconds // 3600
        if hour >= 24:
            days = "%d-" % (hour // 24)
            hour %= 24
            hour = days + ("%02d:" % hour)
        else:
            if hour > 0:
                hour = "%02d:" % hour
            else:
                hour = '00:'
        seconds = seconds % (24 * 3600)
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60
        return "%s%02d:%02d" % (hour, minutes, seconds)

    @staticmethod
    def human_datetime(seconds_since_epoch: int) -> str:
       fmt = "%a %b %-d, %Y at %-I:%M %p"
       return datetime.datetime.fromtimestamp(seconds_since_epoch).strftime(fmt)

    def cpu_memory_formatted(self, with_label=True) -> str:
        """Return the total CPU memory with formatting."""
        total = self.js.reqmem
        if total.endswith("K"):
            bytes_ = float(total.replace("K", "")) * 1e3
        elif total.endswith("M"):
            bytes_ = float(total.replace("M", "")) * 1e6
        elif total.endswith("G"):
            bytes_ = float(total.replace("G", "")) * 1e9
        elif total.endswith("T"):
            bytes_ = float(total.replace("T", "")) * 1e12
        else:
            if with_label:
                return f'     CPU Memory: {total}'
            else:
                return total
        per_core = bytes_ / int(self.js.ncpus)
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if per_core < 1000:
                break
            per_core /= 1000
        pc = f"{per_core:.1f}"
        pc = pc.replace(".0", "")
        total = total.replace("000K", "M")
        total = total.replace("000M", "G")
        total = total.replace("000G", "T")
        total = total.replace(".50G", ".5G")
        total = total.replace(".50T", ".5T")
        total = f"{total}B"
        if with_label:
            if self.js.ncpus == "1":
                return f'     CPU Memory: {total}'
            return f'     CPU Memory: {total} ({pc}{unit} per CPU-core)'
        else:
            return total
 
    @staticmethod
    def rounded_memory_with_safety(mem_used: float) -> int:
        """Return the suggested memory including 20% safety. The input
           value (mem_used) has units of GB."""
        mem_with_safety = math.ceil(1.2 * mem_used)
        if mem_with_safety > 1000:
            mem_suggested = round(mem_with_safety, -2)
            if mem_suggested - mem_with_safety < 0:
                mem_suggested += 100
        elif mem_with_safety > 100:
            mem_suggested = round(mem_with_safety, -1)
            if mem_suggested - mem_with_safety < 0:
                mem_suggested += 10
        elif mem_with_safety > 30:
            mem_suggested = round(mem_with_safety, -1)
            if mem_suggested - mem_with_safety < 0:
                mem_suggested += 5
        else:
            return max(1, mem_with_safety)
        return mem_suggested

    def time_limit_formatted(self) -> str:
        self.js.time_eff_violation = False
        clr = self.txt_normal
        if self.js.timelimitraw == "UNLIMITED":
            return f"     Time Limit: {clr}UNLIMITED{self.txt_normal}"
        if self.js.state == "COMPLETED" and self.js.timelimitraw > 0:
            self.js.time_efficiency = round(100 * self.js.diff / (SECONDS_PER_MINUTE * self.js.timelimitraw))
            self.js.time_efficiency = min(self.js.time_efficiency, 100)
            if self.js.time_efficiency < c.TIME_EFFICIENCY_BLACK and self.js.diff > 3 * c.MIN_RUNTIME_SECONDS:
                self.js.time_eff_violation = True
            if self.js.time_efficiency < c.TIME_EFFICIENCY_RED and self.js.time_eff_violation:
                clr = f"{self.txt_bold}{self.txt_red}"
        hs = self.human_seconds(SECONDS_PER_MINUTE * self.js.timelimitraw)
        return f"     Time Limit: {clr}{hs}{self.txt_normal}"

    def draw_meter(self, efficiency: int, hardware: str, util: bool=False) -> str:
        bars = efficiency // 2
        if bars < 0:
            bars = 0
        elif bars > 50:
            bars = 50
        text = f"{efficiency}%"
        spaces = 50 - bars - len(text)
        if bars + len(text) > 50:
            bars = 50 - len(text)
            spaces = 0
        clr1 = ""
        clr2 = ""
        # optional settings: memory meters turn red below these percentages
        CPU_MEM_UTIL_RED = c.CPU_MEM_UTIL_RED if hasattr(c, "CPU_MEM_UTIL_RED") else 0
        GPU_MEM_UTIL_RED = c.GPU_MEM_UTIL_RED if hasattr(c, "GPU_MEM_UTIL_RED") else 0
        if (efficiency < c.CPU_UTIL_RED and hardware == "cpu" and util and (not self.js.gpus)) or \
           (efficiency < CPU_MEM_UTIL_RED and hardware == "cpumem" and util) or \
           (efficiency < c.GPU_UTIL_RED and hardware == "gpu" and util) or \
           (efficiency < GPU_MEM_UTIL_RED and hardware == "gpumem" and util):
            clr1 = f"{self.txt_red}"
            clr2 = f"{self.txt_bold}{self.txt_red}"
        return f"{self.txt_bold}[{self.txt_normal}" + clr1 + bars * "|" + spaces * " " + clr2 + \
               text + f"{self.txt_normal}{self.txt_bold}]{self.txt_normal}"

    def format_note(self, *items, style="normal", indent_width=4, bullet="*") -> str:
        """Combine the pieces of the note and apply formatting."""
        indent = " " * indent_width
        first_indent = [" " for _ in range(indent_width)]
        if len(first_indent) >= 2:
            first_indent[-2] = bullet
        first_indent = "".join(first_indent)
        wrapper = TextWrapper(width=78,
                              subsequent_indent=indent,
                              break_on_hyphens=False)
        # combine the parts of the note
        note = ""
        starts = ("http", "ftp", "$ ", "#SBATCH")
        for i, item in enumerate(items):
            if i == 0:
                wrapper.initial_indent = first_indent
                note += wrapper.fill(item)
            elif any(item.startswith(start) for start in starts):
                note += f"\n{indent}  {item}\n"
            elif item == "\n":
                note += item
            else:
                wrapper.initial_indent = indent
                note += wrapper.fill(item)
        # apply styling
        if style == "bold":
            styling = self.txt_bold
        elif style == "bold-red":
            styling = f"{self.txt_bold}{self.txt_red}"
        else:
            styling = ""
        # add newline(s) to the end of the note
        if any(items[-1].startswith(start) for start in starts):
            newlines = "\n"
        else:
            newlines = "\n\n"
        return f"{styling}{note}{self.txt_normal}{newlines}"

    def job_notes(self):
        """Process the notes in config.py."""
        s = ""
        # compute several quantities which can be referenced in the notes
        total_used, total, total_cores = self.js.cpu_mem_total__used_alloc_cores
        cores_per_node = int(self.js.ncpus) / int(self.js.nnodes)
        gb_per_core_used = total_used / total_cores / 1024**3 if total_cores != 0 else 0
        gb_per_core_total = total / total_cores / 1024**3 if total_cores != 0 else 0
        gb_per_core = gb_per_core_total  # name used by the ORCD notes
        gb_per_node_used = total_used / int(self.js.nnodes) / 1024**3 if int(self.js.nnodes) != 0 else 0
        # zero GPU utilization
        zero_gpu = False
        if self.js.gpus:
            num_unused_gpus = sum([util == 0 for _, util, _ in self.js.gpu_util__node_util_index])
            if num_unused_gpus:
                zero_gpu = True
        else:
            num_unused_gpus = 0
        cores_per_gpu = int(self.js.ncpus) / self.js.gpus if self.js.gpus else 0
        cpu_mem_per_gpu = total / 1024**3 / self.js.gpus if self.js.gpus else 0
        if self.js.gpus:
            gpu_mem_used, gpu_mem_alloc = self.js.gpu_mem_total__used_alloc
            gpu_mem_used = gpu_mem_used / 1024**3
        multi = "" if self.js.gpus - num_unused_gpus > 1 else "Is the code capable of using multiple GPUs? "
        gpu_show = False if self.js.gpus and cpu_mem_per_gpu < 50 else True
        # zero CPU utilization
        num_unused_nodes = sum([used == 0 for _, used, _, _ in self.js.cpu_util__node_used_alloc_cores])
        zero_cpu = True if num_unused_nodes else False
        multi_cpu = "" if int(self.js.nnodes) - num_unused_nodes > 1 else "Is the code capable of using multiple nodes? "
        # interactive
        interactive_job = (self.js.jobname == "interactive") or self.js.jobname.startswith("sys/dashboard/sys/")
        # low cpu utilization
        somewhat = " " if self.js.cpu_efficiency < c.CPU_UTIL_RED else " somewhat "
        ceff = self.js.cpu_efficiency if self.js.cpu_efficiency > 0 else "less than 1"
        # next three lines needed for serial code using multiple CPU-cores note
        eff_if_serial = 100 / int(self.js.ncpus) if self.js.ncpus != "0" else -1
        serial_ratio = self.js.cpu_efficiency / eff_if_serial
        approx = " approximately " if self.js.cpu_efficiency != round(eff_if_serial, 1) else " "
        # next four lines needed for excess CPU memory note
        if self.js.cpu_memory_efficiency >= 1:
            opening = f"only used {self.js.cpu_memory_efficiency}%"
        else:
            opening = "used less than 1%"
        if hasattr(c, "CORES_PER_NODE") and self.js.cluster in c.CORES_PER_NODE:
            cpn = c.CORES_PER_NODE[self.js.cluster]
        else:
            cpn = 0
        if hasattr(c, "DEFAULT_MEM_PER_CORE") and self.js.cluster in c.DEFAULT_MEM_PER_CORE:
            mpc = c.DEFAULT_MEM_PER_CORE[self.js.cluster]
        else:
            mpc = 0
        # loop over notes
        for condition, note, style in c.NOTES:
            if eval(condition):
                if isinstance(note, str):
                    note = (note,)
                note_eval = []
                for item in note:
                    # evaluate f-strings if found
                    if ('f"' in item or "f'" in item) and "{" in item and "}" in item:
                        note_eval.append(eval(item))
                    else:
                        note_eval.append(item)
                s += self.format_note(*note_eval, style=style)
        return s


class ClassicOutput(BaseFormatter):
    """Classic output formatter for the job report."""

    def __init__(self, js: Jobstats, width: int=80) -> None:
        super().__init__(js)
        self.txt_bold   = ""
        self.txt_red    = ""
        self.txt_normal = ""
        self.width = width

    def output_metadata(self) -> str:
        """Return the job metadata."""
        meta = f"         Job ID: {self.txt_bold}{self.js.jobid}{self.txt_normal}\n"
        meta += f"   User/Account: {self.js.user}/{self.js.account}\n"
        meta += f"       Job Name: {self.js.jobname}\n"
        if self.js.state in ("OUT_OF_MEMORY", "TIMEOUT"):
            meta += f"          State: {self.txt_bold}{self.txt_red}{self.js.state}{self.txt_normal}\n"
        else:
            meta += f"          State: {self.js.state}\n"
        meta += f"          Nodes: {self.js.nnodes}\n"
        meta += f"      CPU Cores: {self.js.ncpus}\n"
        meta += self.cpu_memory_formatted() + "\n"
        if self.js.gpus:
            meta += f"           GPUs: {self.js.gpus}\n"
        meta += f"  QOS/Partition: {self.js.qos}/{self.js.partition}\n"
        meta += f"        Cluster: {self.js.cluster}\n"
        meta += f"     Start Time: {self.human_datetime(self.js.start)}\n"
        in_progress = " (in progress)" if self.js.state == "RUNNING" else ""
        meta += f"       Run Time: {self.human_seconds(self.js.diff)}{in_progress}\n"
        meta += self.time_limit_formatted() + "\n"
        return meta

    def output_overall_cpu_util(self) -> str:
        """Return the overall CPU utilization."""
        if self.js.cpu_util_error_code == 0:
            total_used, total, _ = self.js.cpu_util_total__used_alloc_cores
            self.js.cpu_efficiency = round(100 * total_used / total, 1)
            # the meter is rounded as before (upstream truncates: 48.9% is shown as 48%)
            meter = self.draw_meter(round(self.js.cpu_efficiency), "cpu", util=True)
            cpu_util = f"  CPU utilization  {meter}\n"
        elif self.js.cpu_util_error_code == 1:
            cpu_util = "  CPU utilization  (JSON is malformed)\n"
        elif self.js.cpu_util_error_code == 2:
            cpu_util = "  CPU utilization  (Value was erroneously found to be >100%)\n"
        elif self.js.cpu_util_error_code == 3:
            cpu_util = "  CPU utilization  (Total CPU time was found to be zero)\n"
        else:
            cpu_util = "  CPU utilization  (Something went wrong)\n"
        return cpu_util

    def output_overall_cpu_memory_usage(self) -> str:
        """Return the overall CPU memory usage."""
        if self.js.cpu_mem_error_code == 0:
            total_used, total, _ = self.js.cpu_mem_total__used_alloc_cores
            self.js.cpu_memory_efficiency = round(100 * total_used / total)
            meter = self.draw_meter(self.js.cpu_memory_efficiency, "cpumem", util=True)
            cpu_mem = f"  CPU memory usage {meter}\n"
        elif self.js.cpu_mem_error_code == 1:
            cpu_mem = "  CPU memory usage (JSON is malformed)\n"
        elif self.js.cpu_mem_error_code == 2:
            cpu_mem = "  CPU memory usage (Value was erroneously found to be >100%)\n"
        elif self.js.cpu_mem_error_code == 3:
            cpu_mem = "  CPU memory usage (Allocated memory was found to be zero)\n"
        else:
            cpu_mem = "  CPU memory usage (Something went wrong)\n"
        return cpu_mem

    def unknown_meter(self, text: str) -> str:
        """Return a meter that shows a centered message instead of a value."""
        return f"{self.txt_bold}[{self.txt_normal}{text.center(50)}" \
               f"{self.txt_normal}{self.txt_bold}]{self.txt_normal}"

    def output_overall_gpu_util(self) -> str:
        """Return the overall GPU utilization."""
        if self.js.gpu_util_error_code == 0:
            if self.js.is_mig_job():
                # set utilization to 100 to avoid triggering low utilization notes
                self.js.gpu_utilization = 100
                meter = self.unknown_meter("GPU utilization is unknown for MIG jobs")
                gpu_util = f"  GPU utilization  {meter}\n"
            else:
                overall, overall_gpu_count = self.js.gpu_util_total__util_gpus
                if overall_gpu_count:
                    self.js.gpu_utilization = overall / overall_gpu_count
                    meter = self.draw_meter(round(self.js.gpu_utilization), "gpu", util=True)
                    gpu_util = f"  GPU utilization  {meter}\n"
                else:
                    # set utilization to 100 to avoid triggering low utilization notes
                    self.js.gpu_utilization = 100
                    meter = self.unknown_meter("GPU utilization is unknown")
                    gpu_util = f"  GPU utilization  {meter}\n"
        elif self.js.gpu_util_error_code == 1:
            gpu_util = "  GPU utilization  (Value is unknown)\n"
        else:
            gpu_util = "  GPU utilization  (Something went wrong)\n"
        return gpu_util

    @staticmethod
    def show_overall_detailed_gpu_metric(gm) -> bool:
        """Return True if the metric gets a meter in the overall section. The
           metric must have been found for every node of the job since a mean
           over only some of the GPUs would be misleading."""
        metric_found_in_prom = bool(gm.total__value_gpus[1])
        return metric_found_in_prom and gm.show_overall and gm.error_code == 0

    def output_overall_detailed_gpu_metric(self, gm, max_len) -> str:
        """Return the overall utilization of the detailed GPU metric. The
           value is stored in self.gm_overall for use in the notes."""
        label = f"    {gm.name.rjust(max_len)} util. "
        overall, overall_gpu_count = gm.total__value_gpus
        if gm.error_code == 0:
            if self.js.is_mig_job():
                return label + self.unknown_meter(f"{gm.name} utilization is unknown for MIG jobs") + "\n"
            if overall_gpu_count:
                value = overall / overall_gpu_count
                self.gm_overall[f"{gm.name}-util"] = value
                return label + self.draw_meter(round(value), hardware="other", util=True) + "\n"
            return label + self.unknown_meter(f"{gm.name} utilization is unknown") + "\n"
        elif gm.error_code == 1:
            return label + "(Value is unknown)\n"
        return label + "(Something went wrong)\n"

    def output_overall_gpu_memory_usage(self) -> str:
        """Return the overall GPU memory usage."""
        if self.js.gpu_mem_error_code == 0:
            overall, overall_total = self.js.gpu_mem_total__used_alloc
            gpu_memory_usage = round(100 * overall / overall_total)
            meter = self.draw_meter(gpu_memory_usage, "gpumem", util=True)
            gpu_mem = f"  GPU memory usage {meter}\n"
        elif self.js.gpu_mem_error_code == 1:
            gpu_mem = "  GPU memory usage (JSON is malformed)\n"
        elif self.js.gpu_mem_error_code == 2:
            gpu_mem = "  GPU memory usage (Value was erroneously found to be >100%)\n"
        elif self.js.gpu_mem_error_code == 3:
            gpu_mem = "  GPU memory usage (Allocated memory was found to be zero)\n"
        else:
            gpu_mem = "  GPU memory usage (Something went wrong)\n"
        return gpu_mem

    def get_max_name_length(self) -> int:
        """Return the number of characters of the longest metric name."""
        lengths = [0]
        for gm in self.js.detailed_gpu_metrics:
            if self.show_overall_detailed_gpu_metric(gm):
                lengths.append(len(gm.name))
        return max(lengths)

    def detailed_gpu_value(self, gm, value, compact: bool) -> str:
        """Return the value of a detailed GPU metric with its units. The
           compact form is used in tables."""
        if value is None:
            return "N/A"
        if "power" in gm.metric:
            return f"{round(value)}W" if compact else f"{round(value)} W"
        if "temperature" in gm.metric:
            return f"{round(value)}" + _if_encodable("\u00B0C", "C")
        if "rx" in gm.metric or "tx" in gm.metric:
            if compact:
                return self.human_bytes(round(value), 0, spaces=" ") + "/s"
            return self.human_bytes(value, spaces=" ") + "/s"
        if "bytes" in gm.metric:
            return self.human_bytes(value)
        if compact:
            if any(x in gm.metric for x in ("sm", "occup", "dram")):
                return f"{round(value)}%"
            if any(x in gm.metric for x in ("duty", "tensor", "fp", "integer")):
                return f"{round(value, 1)}%"
            return str(value)
        if gm.is_percentage:
            return f"{value:.1f}%"
        return str(round(value))

    def grid_detailed_gpu_metrics(self,
                                  include_names: Optional[Tuple[str, ...]] = None,
                                  exclude_names: Tuple[str, ...] = ()) -> str:
        """Return the table of metrics as a string with one row per GPU and
           one column per metric. A cell is left empty when the metric was not
           found for that GPU (e.g., a job on nodes with different types of
           GPUs). If include_names is not specified then all metrics will be
           included. Metrics in exclude_names will be excluded."""
        headers = []
        columns = []
        gpus = []  # (node, gpu index) of each row
        for gm in self.js.detailed_gpu_metrics:
            # apply excludes before dealing with includes
            if gm.name in exclude_names:
                continue
            if include_names is not None and gm.name not in include_names:
                continue
            metric_found_in_prom = bool(gm.total__value_gpus[1])
            if metric_found_in_prom and gm.show_per_gpu:
                cells = {}
                for node, value, gpu_index in gm.node_value_index:
                    if gpu_index is None:
                        # the metric was not found for this node
                        continue
                    if (node, gpu_index) not in gpus:
                        gpus.append((node, gpu_index))
                    cells[(node, gpu_index)] = self.detailed_gpu_value(gm, value, compact=True)
                headers.append(gm.name)
                columns.append(cells)
        if not headers:
            return ""
        col_widths = [max(len(header), max(len(val) for val in cells.values()))
                      for header, cells in zip(headers, columns)]
        row_labels = [f"    {node} (GPU {gpu_index})  " for node, gpu_index in gpus]
        indent = " " * max(map(len, row_labels))
        header_line = " | ".join(h.rjust(w) for h, w in zip(headers, col_widths))
        separator = "-+-".join("-" * w for w in col_widths)
        rule = indent + "-" * len(separator)
        lines = [rule, indent + header_line, indent + separator]
        for label, gpu in zip(row_labels, gpus):
            row = " | ".join(cells.get(gpu, "").rjust(w) for cells, w in zip(columns, col_widths))
            lines.append(label.ljust(len(indent)) + row)
        lines.append(rule)
        return "\n".join(lines)

    def output_detailed_gpu_metrics(self, gutter: str) -> str:
        """Return the per-GPU values of the detailed GPU metrics. Tables are
           used when there are four or more metrics, otherwise lines of text."""
        shown = [gm for gm in self.js.detailed_gpu_metrics
                 if gm.show_per_gpu and gm.total__value_gpus[1]]
        if not shown:
            return ""
        docs_url = getattr(c, "GPU_METRICS_DOCS_URL", "")
        report = ""
        if len(shown) >= 4:
            # display metrics as table(s) with the data transfer rates in their own table
            traffic_names = tuple(gm.name for gm in shown
                                  if "PCIe" in gm.name or "NVLink" in gm.name)
            tables = [self.grid_detailed_gpu_metrics(exclude_names=traffic_names)]
            if traffic_names:
                tables.append(self.grid_detailed_gpu_metrics(include_names=traffic_names))
            tables = [table for table in tables if table]
            if not tables:
                return ""
            report += f"\n{gutter}Detailed GPU Metrics\n"
            report += "\n\n".join(tables) + "\n"
            if docs_url:
                # align the URL with the left edge of the table
                rule = tables[0].split("\n")[0]
                report += " " * (len(rule) - len(rule.lstrip())) + docs_url + "\n"
        else:
            # display metrics as lines of text
            for gm in shown:
                name = gm.long_name if gm.long_name else gm.name
                report += f"\n{gutter}GPU {name} per node\n"
                for node, value, gpu_index in gm.node_value_index:
                    if gpu_index is None:
                        # the metric was not found for this node
                        continue
                    value_str = self.detailed_gpu_value(gm, value, compact=False)
                    report += f"{gutter}    {node} (GPU {gpu_index}): {value_str}\n"
            if docs_url:
                report += f"\n{gutter}{docs_url}\n"
        return report

    def output(self, no_color: bool=True) -> str:
        if blessed_is_available and not no_color:
            term = Terminal()
            self.txt_bold   = f"{term.bold}"
            self.txt_red    = f"{term.red}"
            self.txt_normal = f"{term.normal}"
        ########################################################################
        #                               JOB METADATA                           #
        ########################################################################
        report = "\n"
        report += self.width * "=" + "\n"
        report += "Slurm Job Statistics".center(self.width) + "\n"
        report += self.width * "=" + "\n"
        report += self.output_metadata()
        report += "\n"
        ########################################################################
        #                           OVERALL UTILIZATION                        #
        ########################################################################
        heading = f"{self.txt_bold}Overall Utilization{self.txt_normal}"
        report += heading.center(self.width) + "\n"
        report += self.width * "=" + "\n"
        report += self.output_overall_cpu_util()
        report += self.output_overall_cpu_memory_usage()
        if self.js.gpus:
            report += self.output_overall_gpu_util()
            report += self.output_overall_gpu_memory_usage()
            # detailed GPU metrics
            divider = False
            max_len = self.get_max_name_length()
            for gm in self.js.detailed_gpu_metrics:
                if self.show_overall_detailed_gpu_metric(gm):
                    if not divider:
                        report += "  " + 76 * _if_encodable("\u2500", "-") + "\n"
                        divider = True
                    report += self.output_overall_detailed_gpu_metric(gm, max_len)
        report += "\n"
        ########################################################################
        #                          DETAILED UTILIZATION                        #
        ########################################################################
        heading = f"{self.txt_bold}Detailed Utilization{self.txt_normal}"
        report += heading.center(self.width) + "\n"
        report += self.width * "=" + "\n"
        gutter = "  "
        # CPU time utilization
        report += f"{gutter}CPU utilization per node (CPU time used/run time)\n"
        if self.js.cpu_util_error_code == 0:
            for node, used, alloc, cores in self.js.cpu_util__node_used_alloc_cores:
                msg = ""
                if used == 0:
                    msg = f" {self.txt_bold}{self.txt_red}<-- CPU node was not used{self.txt_normal}"
                hs_used = self.human_seconds(used)
                hs_alloc = self.human_seconds(alloc)
                eff = 100 * used / alloc
                report += f"{gutter}    {node}: {hs_used}/{hs_alloc} (efficiency={eff:.1f}%){msg}\n"
            if self.js.nnodes != "1":
                used, alloc, _ = self.js.cpu_util_total__used_alloc_cores
                hs_used = self.human_seconds(used)
                hs_alloc = self.human_seconds(alloc)
                eff = 100 * used / alloc
                report += f"{gutter}Total used/runtime: {hs_used}/{hs_alloc}, efficiency={eff:.1f}%\n"
        else:
            report += f"{gutter}    An error was encountered ({self.js.cpu_util_error_code})\n"
        # CPU memory usage
        report += f"\n{gutter}CPU memory usage per node - used/allocated\n"
        for node, used, alloc, cores in self.js.cpu_mem__node_used_alloc_cores:
            hb_alloc = self.human_bytes(alloc).replace(".0GB", "GB")
            report += f"{gutter}    {node}: {self.human_bytes(used)}/{hb_alloc} "
            if self.js.ncpus == "1":
                report += "\n"
            elif alloc is not None and cores is not None and cores != 0:
                hb_alloc_per_core = self.human_bytes(alloc / cores).replace(".0GB", "GB")
                hb_used_per_core = self.human_bytes(used / cores).replace(".0GB", "GB")
                report += f"({hb_used_per_core}/{hb_alloc_per_core} per core of {cores})\n"
            else:
                report += f"(N/A per core, {cores if cores is not None else 'N/A'} cores)\n"
        total_used, total, total_cores = self.js.cpu_mem_total__used_alloc_cores
        if self.js.nnodes != "1":
            report += f"{gutter}Total used/allocated: {self.human_bytes(total_used)}/{self.human_bytes(total)} "
            if total is not None and total_cores is not None and total_cores != 0:
                hb_total = self.human_bytes(total / total_cores).replace(".0GB", "GB")
                hb_total_used_per_core = self.human_bytes(total_used / total_cores).replace(".0GB", "GB")
                report += f"({hb_total_used_per_core}/{hb_total} per core of {total_cores})\n"
            else:
                report += f"(N/A per core, {total_cores if total_cores is not None else 'N/A'} cores)\n"
        # GPUs
        if self.js.gpus:
            # GPU utilization
            report += f"\n{gutter}GPU utilization per node\n"
            if self.js.gpu_util_error_code == 0:
                for node, util, gpu_index in self.js.gpu_util__node_util_index:
                    msg = ""
                    if util == 0:
                        util = "0%"
                        msg = f" {self.txt_bold}{self.txt_red}<-- GPU was not used{self.txt_normal}"
                    elif util is None:
                        util = ""
                        if self.js.is_mig_job():
                            msg = "GPU utilization is unknown for MIG jobs"
                        else:
                            msg = "GPU utilization is unknown"
                    else:
                        util = f"{util}%"
                    report += f"{gutter}    {node} (GPU {gpu_index}): {util}{msg}\n"
            else:
                 report += f"{gutter}    An error was encountered ({self.js.gpu_util_error_code})\n"
            # GPU memory usage
            report += f"\n{gutter}GPU memory usage per node - maximum used/total\n"
            if self.js.gpu_mem_error_code == 0:
                for node, used, total, gpu_index in self.js.gpu_mem__node_used_total_index:
                    hs_used = self.human_bytes(used)
                    hs_total = self.human_bytes(total).replace(".0GB", "GB")
                    eff = 100 * used / total
                    report += f"{gutter}    {node} (GPU {gpu_index}): {hs_used}/{hs_total} ({eff:.1f}%)\n"
            else:
                report += f"{gutter}    An error was encountered ({self.js.gpu_mem_error_code})\n"
            # detailed GPU metrics
            report += self.output_detailed_gpu_metrics(gutter)
        ########################################################################
        #                             BATCH SCRIPT                             #
        ########################################################################
        if self.js.batch_script:
            heading = f"{self.txt_bold}Batch Script{self.txt_normal}"
            report += "\n" + heading.center(self.width) + "\n"
            report += self.width * "=" + "\n"
            lines = self.js.job_script.split("\n")
            if not self.js.job_script.strip():
                report += f"{gutter}The batch script is not available.\n"
            elif lines[0].startswith("Batch Script for"):
                report += "\n".join(lines[2:])
            else:
                report += self.js.job_script
        ########################################################################
        #                              JOB NOTES                               #
        ########################################################################
        gpu_errors = False
        if self.js.gpus:
            gpu_errors = bool(self.js.gpu_util_error_code > 0 or self.js.gpu_mem_error_code > 0)
        heading = f"{self.txt_bold}Notes{self.txt_normal}"
        if self.js.cpu_util_error_code == 0 and self.js.cpu_mem_error_code == 0 and not gpu_errors:
            report += "\n"
            notes = self.job_notes()
            if notes:
                report += heading.center(self.width) + "\n"
                report += self.width * "=" + "\n"
                report += notes
            return report
        else:
            report += "\n"
            report += heading.center(self.width) + "\n"
            report += self.width * "=" + "\n"
            if self.js.cpu_util_error_code:
                report += f"{gutter}* The CPU utilization could not be determined.\n"
            if self.js.cpu_mem_error_code:
                report += f"{gutter}* The CPU memory usage could not be determined. Try the grafana dashboard.\n"
            if self.js.gpus:
                if self.js.gpu_util_error_code:
                    report += f"{gutter}* The GPU utilization could not be determined.\n"
                if self.js.gpu_mem_error_code:
                    report += f"{gutter}* The GPU memory usage could not be determined.\n"
            report += f"{gutter}* No other notes will be shown.\n"
            report += "\n"
            return report
