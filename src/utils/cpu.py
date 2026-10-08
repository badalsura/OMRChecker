"""
CPU use for batch reading: one OpenCV/ONNX thread per worker process, workers
sized to the physical cores, and (on Windows 10/11) no power throttling of
the background workers. Nothing here changes what a sheet reads as.
"""

import ctypes
import os
import sys

THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "OMR_ONNX_THREADS",
)


def prepare_worker_environment():
    """
    Set the thread limits in the parent before a pool starts: spawned workers
    copy the environment at start, before any initializer runs, and math
    libraries read these variables when they are first imported.
    """
    for name in THREAD_VARIABLES:
        os.environ.setdefault(name, "1")
    disable_power_throttling()


def disable_power_throttling():
    """
    Opt this process out of Windows "EcoQoS" execution-speed throttling, which
    parks a background process (a server window that isn't focused) on
    efficiency cores at low clocks. Windows 10 1709+; a no-op elsewhere.
    """
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        set_info = kernel32.SetProcessInformation
    except (AttributeError, OSError):  # Windows 7: no such API
        return False

    class PowerThrottlingState(ctypes.Structure):
        _fields_ = [
            ("Version", ctypes.c_ulong),
            ("ControlMask", ctypes.c_ulong),
            ("StateMask", ctypes.c_ulong),
        ]

    process_power_throttling = 4
    execution_speed = 0x1
    state = PowerThrottlingState(1, execution_speed, 0)
    try:
        return bool(
            set_info(
                kernel32.GetCurrentProcess(),
                process_power_throttling,
                ctypes.byref(state),
                ctypes.sizeof(state),
            )
        )
    except OSError:
        return False


def physical_cores():
    """Physical core count (hyper-threads share a core's maths units)."""
    count = None
    try:
        import psutil  # optional

        count = psutil.cpu_count(logical=False)
    except Exception:
        count = _windows_physical_cores() if sys.platform == "win32" else _linux_physical_cores()
    return count or os.cpu_count() or 1


def default_workers():
    """
    One worker per logical processor: with files read ahead the workers are
    CPU-bound, and hyper-threads still add throughput on OpenCV work.
    OMR_WORKERS (or the job's worker count) overrides it.
    """
    return max(1, os.cpu_count() or physical_cores())


def _linux_physical_cores():
    try:
        cores = set()
        physical, core = None, None
        with open("/proc/cpuinfo") as handle:
            for line in handle:
                if line.startswith("physical id"):
                    physical = line.split(":")[1].strip()
                elif line.startswith("core id"):
                    core = line.split(":")[1].strip()
                    cores.add((physical, core))
        return len(cores) or None
    except OSError:
        return None


def _windows_physical_cores():
    """GetLogicalProcessorInformation (Windows XP SP3+): one entry per core."""
    try:
        kernel32 = ctypes.windll.kernel32

        class Info(ctypes.Structure):
            _fields_ = [
                ("ProcessorMask", ctypes.c_size_t),
                ("Relationship", ctypes.c_int),
                ("Reserved", ctypes.c_ulonglong * 2),
            ]

        length = ctypes.c_ulong(0)
        kernel32.GetLogicalProcessorInformation(None, ctypes.byref(length))
        count = length.value // ctypes.sizeof(Info)
        if not count:
            return None
        buffer = (Info * count)()
        if not kernel32.GetLogicalProcessorInformation(buffer, ctypes.byref(length)):
            return None
        relation_processor_core = 0
        return sum(1 for item in buffer if item.Relationship == relation_processor_core) or None
    except (AttributeError, OSError):
        return None
