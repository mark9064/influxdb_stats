#!/usr/bin/env python3
"""
Data:

CPU by core psutil.cpu_percent(percpu=True)
CPU by user, system, user, idle, nice, iowait, irq, softirq psutil.cpu_times_percent()
CPU frequency psutil.cpu_freq(percpu=True)
Nvidia GPUs by clocks, temperature, fanspeed, power, utilisations and memory usage
Memory by absolute usage and percentage psutil.virtual_memory() expressed in bytes
Disk usage per specified mountpoint; total, used, percent psutil.disk_usage()
Disk i/o in bytes and number of reads/writes psutil.disk_io_counters()
Network i/o in bytes and packets psutil.net_io_counters()
CPU temp psutil.sensors_temperatures()
System load (1, 5, 15) os.getloadavg()
Total processes psutil.pids()
System uptime psutil.boot_time()

Catergories:
    cpu
    nvidia
    memory
    disk
    diskio
    netio
    sensors
    misc

Timers:
target_time - targetted end time of the fetch - data saved to the db under this value
last_end_time - precise end time stored internally in each class for delta monitors

Control flow:

Initial cycle:
Call init for all delta monitors
Set target to next round second
Begin mainloop

Main loop:
Wait until target_time - save_rate or if already past this log warning
Continuous monitors start
Continuous monitors end 0.2 secs before target time
Point monitors sampled
Data pushed to database
target_time set
next iteration

TODO

cython version
per net interface stats
add photos to readme
auto install dashboard with http API
setting up grafana data source
use .error to still write point stats
"""
# pylint: disable=logging-format-interpolation
import argparse
import collections
import copy
import importlib
import logging
import math
import os
import signal
import statistics
import sys
import time
import warnings

import influxdb
import psutil
import yaml

from common_lib import BaseStat, InternalConfig


class GracefulKiller:
    """Class to deal with SIGTERM and SIGINT"""
    kill_now = False
    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, signum, frame):
        """Sets the kill_now var upon SIGTERM/SIGINT"""
        # pylint: disable=unused-argument
        self.kill_now = True


class CPUStats(BaseStat):
    """All CPU related stats"""
    name = "CPU"
    def __init__(self):
        self.cpu_time_fields = psutil.cpu_times_percent(interval=0.0)._fields
        self.cpu_stats_fields = psutil.cpu_stats()._fields
        self.poll_data = dict(util=[], freq=[], times=[])
        self.cpu_persistent = []
        self.out_data = {"measurement": "cpu"}
        self.last_end_time = 0

    def init_fetch(self):
        """Fetches stats for post-initialisation"""
        self.cpu_persistent = psutil.cpu_stats()
        self.last_end_time = time.time()

    async def poll_stats(self):
        """Fetches the polling stats"""
        self.poll_data = dict(util=[], freq=[], times=[])
        while time.time() < self.target_time - 0.2:
            self.poll_data["util"].append(psutil.cpu_percent(percpu=True))
            self.poll_data["freq"].append(psutil.cpu_freq(percpu=True))
            self.poll_data["times"].append(psutil.cpu_times_percent(interval=0.0))
            next_poll_time = time.time() + self.save_rate / 10
            if next_poll_time > self.target_time:
                break
            time.sleep(next_poll_time - time.time())

    async def get_stats(self):
        """Fetches the point stats and pushes to out_data"""
        current_stats = psutil.cpu_stats()
        time_delta = time.time() - self.last_end_time
        self.last_end_time = time.time()
        stats_delta = [round((current_stats[i] - self.cpu_persistent[i]) / time_delta)
                       for i in range(len(current_stats))]
        self.cpu_persistent = current_stats
        utilisation = [round(statistics.mean(x), 2) for x in zip(*self.poll_data["util"])]
        frequency = [round(statistics.mean([y.current * 1000000 for y in x]), 2)
                     for x in zip(*self.poll_data["freq"])]
        times = [round(statistics.mean(x), 2) for x in zip(*self.poll_data["times"])]
        self.out_data = {"measurement": "cpu"}
        for item in ["ctx_switches", "interrupts"]:
            self.out_data[item] = stats_delta[self.cpu_stats_fields.index(item)]
        for index, item in enumerate(utilisation):
            self.out_data["cpu{0}".format(index)] = item
        for index, item in enumerate(frequency):
            self.out_data["cpu{0}_freq".format(index)] = item
        for index, item in enumerate(times):
            field = self.cpu_time_fields[index]
            if field in ("user", "system", "iowait", "nice", "irq", "softirq"):
                self.out_data[field] = item


class GPUStats(BaseStat):
    """All GPU related stats"""
    name = "GPU"
    def __init__(self):
        self.out_data = []
        self.nvidia_devices = {}
        try:
            import py3nvml.py3nvml as py3nvml
            self.py3nvml = py3nvml
            self.setup_nvidia()
        except ImportError:
            LOGGER.info("Py3nvml not found, disabling nvidia backend")

    def setup_nvidia(self):
        """Sets up nvidia backend"""
        self.py3nvml.nvmlInit()
        LOGGER.debug("Detected nvidia driver: {0}"
                     .format(self.py3nvml.nvmlSystemGetDriverVersion()))
        device_count = self.py3nvml.nvmlDeviceGetCount()
        if device_count == 0:
            LOGGER.warning("Nvidia driver loaded but no devices found")
        for item in range(device_count):
            handle = self.py3nvml.nvmlDeviceGetHandleByIndex(item)
            uuid = str(self.py3nvml.nvmlDeviceGetUUID(handle))
            if uuid not in CONFIG.main["nvidia_cards"]:
                LOGGER.warning("New nvidia card detected, please re-run install to set up"
                               " the card and grafana")
            else:
                self.nvidia_devices[uuid] = handle
        self.nvidia_metrics = dict(
            mem=[self.py3nvml.nvmlDeviceGetMemoryInfo],
            power_usage=[self.py3nvml.nvmlDeviceGetPowerUsage],
            power_limit=[self.py3nvml.nvmlDeviceGetPowerManagementLimit],
            util=[self.py3nvml.nvmlDeviceGetUtilizationRates],
            temp=[self.py3nvml.nvmlDeviceGetTemperature, self.py3nvml.NVML_TEMPERATURE_GPU],
            core_clock=[self.py3nvml.nvmlDeviceGetClockInfo, self.py3nvml.NVML_CLOCK_GRAPHICS],
            max_core_clock=[self.py3nvml.nvmlDeviceGetMaxClockInfo,
                            self.py3nvml.NVML_CLOCK_GRAPHICS],
            mem_clock=[self.py3nvml.nvmlDeviceGetClockInfo, self.py3nvml.NVML_CLOCK_MEM],
            max_mem_clock=[self.py3nvml.nvmlDeviceGetMaxClockInfo, self.py3nvml.NVML_CLOCK_MEM],
            fanspeed_percent=[self.py3nvml.nvmlDeviceGetFanSpeed],
        )
        self.device_support = {}
        for uuid, handle in self.nvidia_devices.items():
            self.device_support[uuid] = {}
            for test, args in self.nvidia_metrics.items():
                if self.test_metric(args[0], handle, *args[1:]):
                    self.device_support[uuid][test] = True
                else:
                    self.device_support[uuid][test] = False
            LOGGER.debug("GPU {0} supports {1}".format(CONFIG.main["nvidia_cards"][uuid],
                                                       self.device_support[uuid]))

    def test_metric(self, func, *args):
        """Tests a metric to see whether it is supported"""
        try:
            res = func(*args)
            if res is None:
                return False
        except Exception:
            return False
        return True

    async def get_stats(self):
        """Fetches the point stats and pushes to out_data"""
        self.out_data = []
        nvidia_results = {}
        for uuid, handle in self.nvidia_devices.items():
            nvidia_results[uuid] = {}
            for metric, enabled in self.device_support[uuid].items():
                if not enabled:
                    continue
                if metric == "mem":
                    res = self.py3nvml.nvmlDeviceGetMemoryInfo(handle)
                    nvidia_results[uuid]["mem_free"] = res.free
                    nvidia_results[uuid]["mem_used"] = res.used
                    nvidia_results[uuid]["mem_total"] = res.total
                    res = None
                elif metric == "power_usage":
                    res = self.py3nvml.nvmlDeviceGetPowerUsage(handle) / 1000
                elif metric == "power_limit":
                    res = self.py3nvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000
                elif metric == "util":
                    res = self.py3nvml.nvmlDeviceGetUtilizationRates(handle)
                    nvidia_results[uuid]["gpu_util"] = res.gpu
                    nvidia_results[uuid]["mem_util"] = res.memory
                    res = None
                elif metric == "temp":
                    res = self.py3nvml.nvmlDeviceGetTemperature(
                        handle, self.py3nvml.NVML_TEMPERATURE_GPU
                    )
                elif metric == "fanspeed_percent":
                    res = self.py3nvml.nvmlDeviceGetFanSpeed(handle)
                elif metric == "core_clock":
                    res = self.py3nvml.nvmlDeviceGetClockInfo(
                        handle, self.py3nvml.NVML_CLOCK_GRAPHICS
                    ) * 1000000
                elif metric == "max_core_clock":
                    res = self.py3nvml.nvmlDeviceGetMaxClockInfo(
                        handle, self.py3nvml.NVML_CLOCK_GRAPHICS
                    ) * 1000000
                elif metric == "mem_clock":
                    res = self.py3nvml.nvmlDeviceGetClockInfo(
                        handle, self.py3nvml.NVML_CLOCK_MEM
                    ) * 1000000
                elif metric == "max_mem_clock":
                    res = self.py3nvml.nvmlDeviceGetMaxClockInfo(
                        handle, self.py3nvml.NVML_CLOCK_MEM
                    ) * 1000000
                if res is not None:
                    nvidia_results[uuid][metric] = res
            self.out_data.append({"measurement": "nvidia", **nvidia_results[uuid],
                                  "tags": {"gpu": uuid}})


class MemoryStats(BaseStat):
    """All memory related stats"""
    name = "Memory"
    def __init__(self):
        self.out_data = {"measurement": "memory"}

    async def get_stats(self):
        """Fetches the point stats and pushes to out_data"""
        mem_data = psutil.virtual_memory()
        self.out_data = {"measurement": "memory"}
        self.out_data["total"] = mem_data.total
        self.out_data["used"] = mem_data.total - mem_data.available
        self.out_data["percent"] = mem_data.percent


class DiskStorageStats(BaseStat):
    """All stats related to storage space on disks"""
    name = "Disk"
    def __init__(self, disk_paths):
        self.out_data = {"measurement": "disk"}
        self.disk_paths = disk_paths

    async def get_stats(self):
        """Fetches the point stats and pushes to out_data"""
        self.out_data = {"measurement": "disk"}
        for item in self.disk_paths:
            disk_data = psutil.disk_usage(item)
            self.out_data["{0}_total".format(item)] = disk_data.total
            self.out_data["{0}_used".format(item)] = disk_data.used
            self.out_data["{0}_percent".format(item)] = disk_data.percent


class DiskIOStats(BaseStat):
    """All stats related to IO on disks"""
    name = "DiskIO"
    def __init__(self):
        self.out_data = {"measurement": "diskio"}
        self.diskio_persistent = []
        self.last_end_time = 0
        self.diskio_fields = psutil.disk_io_counters()._fields
        self.remap = dict(read_bytes="read_bytes", read_count="disk_reads",
                          write_bytes="write_bytes", write_count="disk_writes")

    def init_fetch(self):
        """Fetches stats for post-initialisation"""
        self.diskio_persistent = psutil.disk_io_counters()
        self.last_end_time = time.time()

    async def get_stats(self):
        """Fetches the point stats and pushes to out_data"""
        current_stats = psutil.disk_io_counters()
        time_delta = time.time() - self.last_end_time
        self.last_end_time = time.time()
        stats_delta = [round((current_stats[i] - self.diskio_persistent[i]) / time_delta)
                       for i in range(len(current_stats))]
        self.diskio_persistent = current_stats
        self.out_data = {"measurement": "diskio"}
        for item in ("read_bytes", "read_count", "write_bytes", "write_count"):
            self.out_data[self.remap[item]] = stats_delta[self.diskio_fields.index(item)]


class NetIOStats(BaseStat):
    """All network related stats"""
    name = "NetIO"
    def __init__(self):
        self.out_data = {"measurement": "netio"}
        self.netio_persistent = []
        self.last_end_time = 0
        self.netio_fields = psutil.net_io_counters()._fields
        self.remap = dict(bytes_sent="tx_bytes", bytes_recv="rx_bytes",
                          packets_sent="tx_packets", packets_recv="rx_packets")

    def init_fetch(self):
        """Fetches stats for post-initialisation"""
        self.netio_persistent = psutil.net_io_counters()
        self.last_end_time = time.time()

    async def get_stats(self):
        """Fetches the point stats and pushes to out_data"""
        current_stats = psutil.net_io_counters()
        time_delta = time.time() - self.last_end_time
        self.last_end_time = time.time()
        stats_delta = [round((current_stats[i] - self.netio_persistent[i]) / time_delta)
                       for i in range(len(current_stats))]
        self.netio_persistent = current_stats
        self.out_data = {"measurement": "netio"}
        for item in ("bytes_sent", "bytes_recv", "packets_sent", "packets_recv"):
            self.out_data[self.remap[item]] = stats_delta[self.netio_fields.index(item)]


class SensorStats(BaseStat):
    """All sensor related stats"""
    name = "Sensors"
    def __init__(self):
        self.out_data = {"measurement": "sensors"}
        self.thermal_nosensor = False

    async def get_stats(self):
        """Fetches the point stats and pushes to out_data"""
        temperature_data = psutil.sensors_temperatures()
        cpu_temperature = None
        if "coretemp" in temperature_data:
            for item in temperature_data["coretemp"]:
                if item.label == "Package id 0":
                    cpu_temperature = item.current
                    break
        elif "armada_thermal" in temperature_data:
            cpu_temperature = temperature_data["armada_thermal"][0].current
        self.out_data = {"measurement": "sensors"}
        if cpu_temperature is not None:
            self.out_data["cpu_temp"] = cpu_temperature
        else:
            if not self.thermal_nosensor:
                LOGGER.info("CPU thermal sensor not found")
                self.thermal_nosensor = True


class MiscStats(BaseStat):
    """Any other miscellaneous stats"""
    name = "Misc"
    def __init__(self):
        self.out_data = {"measurement": "misc"}

    async def get_stats(self):
        """Fetches the point stats and pushes to out_data"""
        sys_load = os.getloadavg()
        processes = len(psutil.pids())
        uptime = self.target_time - int(psutil.boot_time())
        self.out_data = {"measurement": "misc"}
        for index, item in enumerate(("load_1", "load_5", "load_15")):
            self.out_data[item] = sys_load[index]
        self.out_data["processes"] = processes
        self.out_data["uptime"] = uptime


def critical_exit(exc, message=""):
    """Exits with a critical error"""
    LOGGER.critical(format_error(exc, message=message))
    sys.exit(1)


def format_error(exc_info, message=""):
    """Returns a string of formatted exception info"""
    if message:
        message = "- {0} ".format(message)
    if exc_info[1] is not None:
        trace = ": {0}".format(exc_info[1])
    else:
        trace = ""
    if exc_info[2] is not None:
        line = "(L{0})".format(exc_info[2].tb_lineno)
    else:
        line = ""
    return "{0} {1}{2}{3}".format(exc_info[0].__name__, message, line, trace)

def main(args):
    """Main function"""
    if CAUGHT_WARNINGS:
        LOGGER.info("Suppressed sys.excepthook warning")
    custom_dir = "custom_plugins"
    save_rate = args["save_rate"]
    error_limit = args["error_limit"]
    pidfile = args["pidfile"]
    interrupt = GracefulKiller()
    influx_args = {x: args[x]
                   for x in ["host", "port", "username", "password", "database"]}
    if not args["dry_run"]:
        client = influxdb.InfluxDBClient(**influx_args)
    try:
        stats_classes = [CPUStats(), MemoryStats(), DiskStorageStats(args["disk_paths"]),
                         DiskIOStats(), NetIOStats(), SensorStats(), MiscStats(), GPUStats()]
        modules = os.listdir(custom_dir)
        for item in modules:
            if not item.endswith(".py"):
                continue
            item = item[:-3]
            try:
                module = importlib.import_module("{0}.{1}".format(custom_dir, item))
                for stat_class in module.ACTIVATED_METRICS:
                    stats_classes.append(stat_class())
                    LOGGER.debug("Loaded class {0} from {1}".format(stat_class.name, item))
            except Exception:
                LOGGER.warning("Failed to import plugin {0}".format(item))
        stats_classes = {x.name: x for x in stats_classes}
        BaseStat.save_rate = save_rate
        for item in stats_classes.values():
            if callable(getattr(item, "init_fetch", None)):
                item.init_fetch()
            if callable(getattr(item, "poll_stats", None)):
                item.continuous = True
            else:
                item.continuous = False
    except Exception:
        exc = sys.exc_info()
        critical_exit(exc, message="Initialisation failed")
    cumulative_errors = 0
    target_time = math.ceil(time.time() + 1)
    BaseStat.set_time(target_time)
    LOGGER.info("Initialised successfully")
    while True:
        try:
            if interrupt.kill_now or (cumulative_errors > error_limit > 0):
                break
            if time.time() > target_time - save_rate:
                behind_secs = time.time() - target_time + save_rate
                level = logging.INFO
                if behind_secs >= save_rate / 2:
                    level = logging.WARNING
                LOGGER.log(level, "Running behind by {0:.2f}s".format(behind_secs))
                if behind_secs > 5:
                    LOGGER.critical("Running behind by more than 5 seconds, skipping data entry")
                    target_time = math.ceil(time.time() + 1)
                    BaseStat.set_time(target_time)
            while time.time() < target_time - save_rate:
                time.sleep(0.001)
            current_time = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(target_time))
            errors = trio.run(collect_stats, stats_classes)
            if any(errors.values()):
                cumulative_errors += 1
                for key, value in errors.items():
                    if value:
                        LOGGER.error("Error in stats collect for {0}: {1}"
                                     .format(key, format_error(value)))
            write_data = []
            for key, value in stats_classes.items():
                if not errors[key]:
                    out_data = copy.deepcopy(value.out_data)
                    if isinstance(value.out_data, dict):
                        format_dataset = format_measurements(out_data, current_time, key)
                        if format_dataset is not None:
                            write_data.append(format_dataset)
                    elif isinstance(value.out_data, list):
                        for dataset in out_data:
                            format_dataset = format_measurements(dataset, current_time, key)
                            if format_dataset is not None:
                                write_data.append(format_dataset)
            if not args["dry_run"]:
                client.write_points(write_data, database=influx_args["database"])
            else:
                print(write_data)
            cumulative_errors = 0
        except Exception:
            exc = sys.exc_info()
            LOGGER.error("Caught exception: {0}".format(format_error(exc)))
            cumulative_errors += 1
        finally:
            target_time += save_rate
            BaseStat.set_time(target_time)
    if pidfile is not None:
        LOGGER.debug("Removing pidfile")
        os.remove(pidfile)
    LOGGER.info("Exiting")


def format_measurements(dataset, current_time, name):
    """Takes a measurement dict and formats it for influxdb"""
    if "measurement" not in dataset:
        LOGGER.error("No measurement found for {0}".format(name))
        return None
    measurement = dataset.pop("measurement")
    if measurement is None:
        return None
    tags = {}
    if "tags" in dataset:
        tags = dataset.pop("tags")
    if not dataset:
        return None
    return dict(measurement=measurement, time=current_time,
                fields=dataset, tags=tags)


async def collect_stats(stats_classes):
    """Asynchronously fetches the stats"""
    errors = {k: False for k in stats_classes}
    for key, value in stats_classes.items():
        value.error = False
    async with trio.open_nursery() as nursery:
        for key, value in stats_classes.items():
            if value.continuous:
                nursery.start_soon(catch_fetch_errors, value.poll_stats, value)
    stats_classes, errors = stats_error_handler(stats_classes, errors)
    async with trio.open_nursery() as nursery:
        for key, value in stats_classes.items():
            if not errors[key]:
                nursery.start_soon(catch_fetch_errors, value.get_stats, value)
    stats_classes, errors = stats_error_handler(stats_classes, errors)
    return errors


async def catch_fetch_errors(async_fn, value):
    """Executes an async function and catches any raised errors"""
    try:
        await async_fn()
    except Exception:
        value.error = sys.exc_info()


def stats_error_handler(stats_classes, errors):
    """Handles any errors raised during stats fetches"""
    for key, value in stats_classes.items():
        if value.error:
            errors[key] = value.error
        value.error = False
    return stats_classes, errors


def initial_argparse():
    """Parses command line args"""
    log_levels = dict(debug=logging.DEBUG, info=logging.INFO, warning=logging.WARNING,
                      error=logging.ERROR, critical=logging.CRITICAL)
    cmd_args = collections.OrderedDict([
        ["config_file", dict(cmd_name="config-file", default=None, type=[None, str],
                             help="Specify path to config file. The command line options override "
                             "the config file. Example config file in example_config.yaml")],
        ["username", dict(cmd_name="username", default="root", type=str,
                          help="Username for influxdb. Default is root")],
        ["password", dict(cmd_name="password", default="root", type=str,
                          help="Password for influxdb. Default is root")],
        ["host", dict(cmd_name="host", default="localhost", type=str,
                      help="Host for influxdb. Default is localhost")],
        ["port", dict(cmd_name="port", default=8086, type=int,
                      help="Port for influxdb. Default is 8086")],
        ["database", dict(cmd_name="database", default="system_stats", type=str,
                          help="Database name for influxdb. Default is system_stats")],
        ["save_rate", dict(cmd_name="save-rate", default=1, type=int,
                           help="Sets how often the stats are saved to influx, in seconds. "
                           "Default is 1, must be a non zero integer")],
        ["disk_paths", dict(cmd_name="disk-paths", default=["/"], nargs="*", type=str,
                            help="Sets the mountpoints used for disk monitoring (space used only, "
                            "io is global). Default is /, multiple args should be seperated "
                            "with a space e.g '--disk-paths / /boot/efi'. Trailing slash on "
                            "mountpoint is optional. No args disables disk monitoring.")],
        ["error_limit", dict(cmd_name="max-consecutive-errors", default=0, type=int,
                             help="Sets the max limit for consecutive errors, which the the  "
                             "program will exit at if reached. An error can occur once per save "
                             "cycle. Default is 0 (never exit)")],
        ["dry_run", dict(cmd_name="dry-run", default=False, type=bool, action="store_true",
                         help="Skips writing any data to influx and instead prints it "
                         "to stdout. Useful only for testing. A valid influx database "
                         "is not required when running in this mode.")],
        ["logfile_path", dict(cmd_name="logfile-path", default=None, type=[None, str],
                              help="Sets the path to the desired logfile. By default a logfile "
                              "is not created.")],
        ["log_stdout", dict(cmd_name="log-stdout", default=False, type=bool, action="store_true",
                            help="Enables logging non critical events to stdout")],
        ["log_level", dict(cmd_name="log-level", default="info", type=str,
                           help="Set the loglevel for all logging. Default is info. "
                           "Available levels are {0}".format(", ".join(log_levels.keys())))],
        ["quiet", dict(cmd_name="quiet", default=False, type=bool, action="store_true",
                       help="Disables logging critical exits to stdout; complete silence")],
        ["pidfile", dict(cmd_name="pidfile", default=None, type=[None, str],
                         help="Enables writing a pidfile to the specified location. "
                         "File is removed when the program exits. "
                         "Any existing file will be overwritten.")]])
    parser = argparse.ArgumentParser()
    format_dict = copy.deepcopy(cmd_args)
    for key, value in format_dict.items():
        if isinstance(value["type"], list):
            value["type"] = value["type"][1]
        elif "action" in value:
            if value["action"] == "store_true":
                del value["type"]
        name = "--{0}".format(value["cmd_name"])
        del value["cmd_name"]
        del value["default"]
        parser.add_argument(name, dest=key, **value)

    args = vars(parser.parse_args())
    specified = {}
    for key, value in args.items():
        if value is None or (cmd_args[key]["type"] == bool and value == cmd_args[key]["default"]):
            args[key] = cmd_args[key]["default"]
            specified[key] = False
        else:
            specified[key] = True
    if args["config_file"] is not None:
        args["config_file"] = os.path.expanduser(args["config_file"])
        args = parse_config_file(args, cmd_args, specified)
    if args["log_level"] not in log_levels.keys():
        critical_exit((TypeError, None, None), message="Invalid loglevel specified")
    ROOT_LOGGER.setLevel(log_levels[args["log_level"]])
    if args["log_stdout"] and args["quiet"]:
        critical_exit((TypeError, None, None),
                      message="Log stdout and quiet cannot be specified together")
    if args["quiet"]:
        ROOT_LOGGER.handlers = []
    if args["log_stdout"]:
        ROOT_LOGGER.handlers[0].level = logging.DEBUG
    if args["logfile_path"] is not None:
        args["logfile_path"] = os.path.expanduser(args["logfile_path"])
        ROOT_LOGGER.addHandler(create_sublogger(logging.DEBUG, args["logfile_path"]))
    if ROOT_LOGGER.handlers == []:
        ROOT_LOGGER.disabled = True
    if args["save_rate"] <= 0:
        critical_exit((TypeError, None, None),
                      message="Save rate must be a non zero positive integer")
    if args["pidfile"] is not None:
        args["pidfile"] = os.path.expanduser(args["pidfile"])
        open(args["pidfile"], "w").write(str(os.getpid()))
    mountpoints = [x.mountpoint for x in psutil.disk_partitions()]
    for item in args["disk_paths"]:
        if item.endswith("/") and item != "/":
            item = item[:-1]
        if item not in mountpoints:
            critical_exit((FileNotFoundError, None, None), message="Invalid mountpoint specified")
    return args


def parse_config_file(args, cmd_args, specifed):
    """Parses the config file and type checks it"""
    with open(args["config_file"], "r") as stream:
        args_new = yaml.safe_load(stream)
    args_new_formatted = {}
    lookup = {v["cmd_name"]: k for k, v in cmd_args.items()}
    for key, value in args_new.items():
        args_new_formatted[lookup[key]] = value
    args_new = args_new_formatted
    for key, value in args_new.items():
        allowed_type = cmd_args[key]["type"]
        error = ""
        if isinstance(allowed_type, list):
            if (value is not None) and (not isinstance(value, allowed_type[1])):
                error = "{0} or None".format(allowed_type[1].__name__)
        elif not isinstance(value, allowed_type):
            error = allowed_type.__name__
        if "nargs" in cmd_args[key]:
            if cmd_args[key]["nargs"] == "*":
                error = ""
                if not isinstance(value, list):
                    error = "list"
                else:
                    for item in value:
                        if not isinstance(item, allowed_type):
                            error = "{0}s inside a list".format(allowed_type.__name__)
        if error:
            critical_exit((TypeError, None, None),
                          message="TypeError: Option {0} in config file is not type {1}"
                          .format(cmd_args[key]["cmd_name"], error))
        if not specifed[key]:
            args[key] = value
    return args


def create_sublogger(level, path=None):
    """Sets up a sublogger"""
    formatter = logging.Formatter("%(asctime)s %(name)s %(process)d %(levelname)s %(message)s")
    if path is None:
        logger_handler = logging.StreamHandler(sys.stdout)
    else:
        logger_handler = logging.FileHandler(path)
    logger_handler.setLevel(level)
    logger_handler.setFormatter(formatter)
    return logger_handler


def handle_warnings():
    """Handles the sys.excepthook warning trio raises on ubuntu"""
    if not CAUGHT_WARNINGS:
        return None
    if len(CAUGHT_WARNINGS) == 1:
        if "sys.excepthook" in CAUGHT_WARNINGS[0].message.args[0]:
            return True
    for item in CAUGHT_WARNINGS:
        print(item)
    return False


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.exists("configured"):
        os.mkdir("configured")
    CONFIG = InternalConfig()
    with warnings.catch_warnings(record=True) as CAUGHT_WARNINGS:
        warnings.simplefilter("always")
        import trio
        CAUGHT_WARNINGS = handle_warnings()
    logging.Formatter.converter = time.gmtime
    ROOT_LOGGER = logging.getLogger()
    ROOT_LOGGER.setLevel(logging.INFO)
    ROOT_LOGGER.addHandler(create_sublogger(logging.CRITICAL))
    LOGGER = logging.getLogger("system_metrics_influx")
    main(initial_argparse())
    CONFIG.write_config()
