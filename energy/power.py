import glob
import os
import platform
import re
import subprocess
import time
import uuid
import threading
import logging
from collections import deque
from dataclasses import dataclass

from config import settings

logger = logging.getLogger("carbon-proxy.power")

# Known TDP values (watts) for auto-detection
# Apple Silicon: combined CPU+GPU SoC TDP (GPU is integrated)
# Sources: Apple specs, AnandTech, Notebookcheck
_APPLE_SILICON_TDP = {
    "M1": 15, "M1 Pro": 30, "M1 Max": 60, "M1 Ultra": 120,
    "M2": 15, "M2 Pro": 30, "M2 Max": 35, "M2 Ultra": 70,
    "M3": 22, "M3 Pro": 30, "M3 Max": 40, "M3 Ultra": 80,
    "M4": 22, "M4 Pro": 30, "M4 Max": 40,
}

# x86 CPU TDP lookup table
# Source: CodeCarbon (https://github.com/mlco2/codecarbon), Apache 2.0 license
# File: data/cpu_power.csv
# Stores entries as {lowercased_name: tdp, ...} and {frozenset(tokens): (name, tdp), ...}
_CPU_TDP_BY_NAME: dict[str, float] = {}
_CPU_TDP_BY_TOKENS: dict[frozenset[str], tuple[str, float]] = {}

# Fallback: 4W per thread (CodeCarbon's default)
_DEFAULT_WATTS_PER_THREAD = 4.0


def _normalize_cpu_name(raw: str) -> str:
    """Normalize CPU name like CodeCarbon: strip (R), (TM), CPU, clock speed."""
    name = raw.replace("(R)", "").replace("(TM)", "").replace("(tm)", "")
    name = re.sub(r"\s*CPU\s*@\s*\d+\.\d+\s*GHz", "", name)
    name = name.replace(" CPU", "")
    name = re.sub(r"\s*@\s*\d+\.\d+\s*GHz", "", name)
    return name.strip()


def _tokenize(name: str) -> frozenset[str]:
    """Extract alphanumeric tokens from a CPU name."""
    return frozenset(re.findall(r"[a-z0-9]+", name.lower()))


def _load_cpu_tdp_table():
    """Load CPU TDP lookup table from csv."""
    csv_path = os.path.join(os.path.dirname(__file__), "data", "cpu_power.csv")
    if not os.path.isfile(csv_path):
        return
    try:
        with open(csv_path) as f:
            next(f)  # skip header
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.rsplit(",", 1)
                if len(parts) == 2:
                    name, tdp_str = parts[0].strip(), parts[1].strip()
                    try:
                        tdp = float(tdp_str)
                    except ValueError:
                        continue
                    _CPU_TDP_BY_NAME[name.lower()] = tdp
                    tokens = _tokenize(name)
                    if tokens:
                        _CPU_TDP_BY_TOKENS[tokens] = (name, tdp)
        logger.info("Loaded %d CPU TDP entries", len(_CPU_TDP_BY_NAME))
    except Exception as e:
        logger.warning("Failed to load CPU TDP table: %s", e)


def _lookup_cpu_tdp(cpu_name: str) -> tuple[float | None, str | None]:
    """Match a CPU name against the TDP table (CodeCarbon-style 3-stage matching).

    Returns (tdp_watts, matched_name) or (None, None).
    """
    raw_lower = cpu_name.lower().strip()

    # Stage 1: exact match on raw string
    if raw_lower in _CPU_TDP_BY_NAME:
        return _CPU_TDP_BY_NAME[raw_lower], cpu_name

    # Stage 2: normalize, then exact match
    normalized = _normalize_cpu_name(cpu_name).lower()
    if normalized in _CPU_TDP_BY_NAME:
        return _CPU_TDP_BY_NAME[normalized], cpu_name

    # Stage 3: token-set match (all tokens in CSV entry appear in detected name)
    query_tokens = _tokenize(normalized)
    matches = []
    for entry_tokens, (entry_name, tdp) in _CPU_TDP_BY_TOKENS.items():
        if entry_tokens.issubset(query_tokens):
            matches.append((len(entry_tokens), entry_name, tdp))

    if matches:
        # Pick the match with the most tokens (most specific)
        matches.sort(key=lambda x: x[0], reverse=True)
        if len(matches) == 1 or matches[0][0] > matches[1][0]:
            return matches[0][2], matches[0][1]
        # Ambiguous -- multiple equally specific matches, skip
        logger.debug("Ambiguous CPU TDP match for '%s': %s", cpu_name,
                     [m[1] for m in matches[:3]])

    return None, None


_load_cpu_tdp_table()


def _detect_chip_tdp() -> tuple[float, float, str]:
    """Auto-detect CPU/GPU TDP from chip model. Returns (cpu_tdp, gpu_tdp, chip_name).

    Detection order:
    1. CHIP_MODEL env var (for Docker where host info is unavailable)
    2. macOS sysctl (native macOS only)
    3. Linux /proc/cpuinfo
    """
    system = platform.system()

    # 2. macOS: Apple Silicon (integrated GPU, single TDP)
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            )
            brand = result.stdout.strip()

            if brand.startswith("Apple "):
                chip = brand[6:]  # strip "Apple " prefix
                for name in sorted(_APPLE_SILICON_TDP, key=len, reverse=True):
                    if chip.startswith(name):
                        tdp = _APPLE_SILICON_TDP[name]
                        return tdp, 0.0, f"Apple {name}"
        except Exception:
            pass

    # 3. Linux: read /proc/cpuinfo model name and look up TDP
    if system == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        chip = line.split(":", 1)[1].strip()
                        tdp, matched = _lookup_cpu_tdp(chip)
                        if tdp:
                            logger.info("CPU TDP matched '%s' -> %s (%.0fW)", chip, matched, tdp)
                            return tdp, 0.0, chip
                        return 0.0, 0.0, chip
        except OSError:
            pass

    # 4. Windows: read CPU name from registry and look up TDP
    if system == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            chip, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            winreg.CloseKey(key)
            chip = chip.strip()
            tdp, matched = _lookup_cpu_tdp(chip)
            if tdp:
                logger.info("CPU TDP matched '%s' -> %s (%.0fW)", chip, matched, tdp)
                return tdp, 0.0, chip
            return 0.0, 0.0, chip
        except Exception:
            pass

    return 0.0, 0.0, ""


# Optional NVIDIA support
try:
    import pynvml
    HAS_PYNVML = True
except ImportError:
    HAS_PYNVML = False


@dataclass
class PowerSample:
    timestamp: float  # monotonic
    cpu_watts: float
    gpu_watts: float

    @property
    def total_watts(self) -> float:
        return self.cpu_watts + self.gpu_watts


@dataclass
class ActiveRequest:
    start_time: float  # monotonic
    tokens_out: int = 0


@dataclass
class EnergyResult:
    energy_joules: float
    power_source: str  # "measured", "estimated", "none"


class PowerMonitor:
    def __init__(self):
        self._samples: deque[PowerSample] = deque(maxlen=settings.power_sample_hz * 60)
        self._active_requests: dict[str, ActiveRequest] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # CPU sensor state
        self._cpu_method = None  # "rapl", "powermetrics", "tdp", None
        self._rapl_path: str | None = None
        self._rapl_last_energy_uj: int = 0
        self._rapl_last_time: float = 0
        self._rapl_max_energy: int = 0

        # GPU sensor state
        self._gpu_method = None  # "nvml", "hwmon", "rapl_gt", "powermetrics", "tdp", None
        self._gpu_hwmon_path: str | None = None
        self._nvml_handle = None
        self._rapl_gt_path: str | None = None
        self._rapl_gt_last_energy_uj: int = 0
        self._rapl_gt_last_time: float = 0
        self._rapl_gt_max_energy: int = 0

        # macOS powermetrics state
        self._is_macos = platform.system() == "Darwin"
        self._powermetrics_available = False
        self._last_powermetrics_cpu_w: float = 0.0
        self._last_powermetrics_gpu_w: float = 0.0
        self._powermetrics_lock = threading.Lock()

        # Auto-detected chip info
        self._auto_tdp_cpu: float = 0.0
        self._auto_tdp_gpu: float = 0.0
        self._chip_name: str = ""

        # Detect available power sources
        self._power_source = "none"
        self._detect_sensors()

    def _detect_sensors(self):
        # Auto-detect chip TDP before sensor detection (used as fallback)
        self._auto_tdp_cpu, self._auto_tdp_gpu, self._chip_name = _detect_chip_tdp()
        if self._chip_name:
            logger.info("Detected chip: %s", self._chip_name)

        self._detect_cpu_sensor()
        self._detect_gpu_sensor()

        if self._cpu_method in ("rapl", "powermetrics") or self._gpu_method in ("nvml", "hwmon", "rapl_gt", "powermetrics"):
            self._power_source = "measured"
        elif self._cpu_method == "tdp" or self._gpu_method == "tdp":
            self._power_source = "estimated"
        else:
            logger.warning(
                "No power sensors or TDP values configured. "
                "Set TDP_CPU_WATTS and TDP_GPU_WATTS for estimation, "
                "or mount /sys/class/powercap for RAPL."
            )

        logger.info(
            "Power detection complete: cpu=%s, gpu=%s, source=%s",
            self._cpu_method or "none", self._gpu_method or "none", self._power_source,
        )

    def _detect_cpu_sensor(self):
        # 1. Try RAPL (Linux - Intel and AMD)
        rapl_path = self._find_rapl_cpu()
        if rapl_path:
            try:
                self._rapl_last_energy_uj = self._read_file_int(rapl_path)
                self._rapl_last_time = time.monotonic()
                max_path = os.path.join(os.path.dirname(rapl_path), "max_energy_range_uj")
                if os.path.isfile(max_path):
                    self._rapl_max_energy = self._read_file_int(max_path)
                self._rapl_path = rapl_path
                self._cpu_method = "rapl"
                logger.info("CPU power: RAPL at %s", rapl_path)
                return
            except PermissionError:
                logger.warning(
                    "RAPL found but permission denied. Fix with: "
                    "sudo chmod a+r /sys/class/powercap/intel-rapl:*/energy_uj"
                )
            except OSError as e:
                logger.warning("RAPL found but unreadable: %s", e)

        # 2. Try macOS powermetrics
        if self._is_macos:
            if self._check_powermetrics():
                self._cpu_method = "powermetrics"
                logger.info("CPU power: macOS powermetrics")
                return

        # 3. TDP fallback (env var > auto-detected > thread-count estimate > none)
        tdp = settings.tdp_cpu_watts or self._auto_tdp_cpu
        if not tdp:
            # Last resort: 4W per thread (CodeCarbon's default)
            try:
                threads = os.cpu_count() or 0
                if threads > 0:
                    tdp = threads * _DEFAULT_WATTS_PER_THREAD
                    logger.info("CPU power: estimating %.0fW from thread count (%d threads x %.0fW)",
                                tdp, threads, _DEFAULT_WATTS_PER_THREAD)
            except Exception:
                pass
        if tdp > 0:
            self._cpu_method = "tdp"
            self._effective_tdp_cpu = tdp
            if not self._chip_name or settings.tdp_cpu_watts:
                logger.info("CPU power: TDP estimation (%.0fW)", tdp)
            else:
                logger.info("CPU power: TDP estimation (%.0fW - %s)", tdp, self._chip_name)
        else:
            self._effective_tdp_cpu = 0.0

    def _detect_gpu_sensor(self):
        # 1. Try NVIDIA (pynvml)
        if HAS_PYNVML:
            try:
                pynvml.nvmlInit()
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                pynvml.nvmlDeviceGetPowerUsage(self._nvml_handle)  # test read
                name = pynvml.nvmlDeviceGetName(self._nvml_handle)
                if isinstance(name, bytes):
                    name = name.decode()
                self._gpu_method = "nvml"
                logger.info("GPU power: NVIDIA NVML (%s)", name)
                return
            except Exception as e:
                logger.debug("NVIDIA NVML not available: %s", e)
                try:
                    pynvml.nvmlShutdown()
                except Exception:
                    pass

        # 2. Try hwmon (AMD / Intel Arc) - auto-detect path
        hwmon_path = self._find_gpu_hwmon()
        if hwmon_path:
            try:
                self._read_file_int(hwmon_path)
                self._gpu_hwmon_path = hwmon_path
                self._gpu_method = "hwmon"
                logger.info("GPU power: hwmon at %s", hwmon_path)
                return
            except PermissionError:
                logger.warning("GPU hwmon found but permission denied: %s", hwmon_path)
            except OSError as e:
                logger.warning("GPU hwmon found but unreadable: %s", e)

        # 3. Try Intel iGPU via RAPL GT domain
        gt_path = self._find_rapl_gt()
        if gt_path:
            try:
                self._rapl_gt_last_energy_uj = self._read_file_int(gt_path)
                self._rapl_gt_last_time = time.monotonic()
                max_path = os.path.join(os.path.dirname(gt_path), "max_energy_range_uj")
                if os.path.isfile(max_path):
                    self._rapl_gt_max_energy = self._read_file_int(max_path)
                self._rapl_gt_path = gt_path
                self._gpu_method = "rapl_gt"
                logger.info("GPU power: Intel iGPU RAPL GT at %s", gt_path)
                return
            except (PermissionError, OSError) as e:
                logger.warning("RAPL GT domain found but unreadable: %s", e)

        # 4. macOS powermetrics (covers GPU too)
        if self._is_macos and self._powermetrics_available:
            self._gpu_method = "powermetrics"
            logger.info("GPU power: macOS powermetrics")
            return

        # 5. TDP fallback (env var > auto-detected > none)
        tdp = settings.tdp_gpu_watts or self._auto_tdp_gpu
        if tdp > 0:
            self._gpu_method = "tdp"
            self._effective_tdp_gpu = tdp
            logger.info("GPU power: TDP estimation (%.0fW)", tdp)
        else:
            self._effective_tdp_gpu = 0.0

    # --- Sensor discovery helpers ---

    @staticmethod
    def _find_rapl_cpu() -> str | None:
        """Find RAPL CPU package energy counter."""
        # Check configured path first
        if os.path.isfile(settings.rapl_path):
            return settings.rapl_path
        # Scan for it
        for path in sorted(glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj")):
            name_path = os.path.join(os.path.dirname(path), "name")
            try:
                with open(name_path) as f:
                    name = f.read().strip()
                if name == "package-0":
                    return path
            except OSError:
                continue
        # Fallback: first one found
        paths = sorted(glob.glob("/sys/class/powercap/intel-rapl:*/energy_uj"))
        return paths[0] if paths else None

    @staticmethod
    def _find_gpu_hwmon() -> str | None:
        """Find GPU power sensor via hwmon (AMD amdgpu / Intel Arc xe/i915)."""
        # Check configured path first
        if settings.gpu_power_path and os.path.isfile(settings.gpu_power_path):
            return settings.gpu_power_path
        # Auto-scan all DRM cards
        for path in sorted(glob.glob("/sys/class/drm/card*/device/hwmon/*/power1_average")):
            return path
        # Also check power1_input as fallback
        for path in sorted(glob.glob("/sys/class/drm/card*/device/hwmon/*/power1_input")):
            return path
        return None

    @staticmethod
    def _find_rapl_gt() -> str | None:
        """Find Intel iGPU RAPL GT/uncore domain."""
        for path in sorted(glob.glob("/sys/class/powercap/intel-rapl:*:*/name")):
            try:
                with open(path) as f:
                    name = f.read().strip()
                if name in ("uncore", "gt"):
                    energy_path = os.path.join(os.path.dirname(path), "energy_uj")
                    if os.path.isfile(energy_path):
                        return energy_path
            except OSError:
                continue
        return None

    def _check_powermetrics(self) -> bool:
        """Check if macOS powermetrics is available with sudo."""
        try:
            result = subprocess.run(
                ["sudo", "-n", "powermetrics", "--samplers", "cpu_power,gpu_power",
                 "-n", "1", "-i", "100", "-f", "plist"],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                self._powermetrics_available = True
                return True
            logger.info(
                "macOS powermetrics requires passwordless sudo. "
                "Add to sudoers: %s ALL=(root) NOPASSWD: /usr/bin/powermetrics",
                os.environ.get("USER", "username"),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return False

    # --- Power reading methods ---

    @staticmethod
    def _read_file_int(path: str) -> int:
        with open(path, "r") as f:
            return int(f.read().strip())

    def _read_rapl_watts(self, path: str, last_uj: int, last_time: float, max_energy: int) -> tuple[float, int, float]:
        """Read RAPL energy counter and return (watts, new_uj, new_time)."""
        now = time.monotonic()
        energy_uj = self._read_file_int(path)
        dt = now - last_time
        if dt <= 0:
            return 0.0, energy_uj, now

        delta = energy_uj - last_uj
        if delta < 0:
            delta = delta + max_energy if max_energy > 0 else 0

        watts = (delta / 1_000_000) / dt
        return watts, energy_uj, now

    def _read_cpu_watts(self) -> float:
        if self._cpu_method == "rapl":
            try:
                watts, self._rapl_last_energy_uj, self._rapl_last_time = self._read_rapl_watts(
                    self._rapl_path, self._rapl_last_energy_uj,
                    self._rapl_last_time, self._rapl_max_energy,
                )
                return watts
            except (OSError, ValueError):
                return 0.0

        if self._cpu_method == "powermetrics":
            with self._powermetrics_lock:
                return self._last_powermetrics_cpu_w

        if self._cpu_method == "tdp":
            load_factor = 0.8 if self._active_requests else 0.1
            return self._effective_tdp_cpu * load_factor

        return 0.0

    def _read_gpu_watts(self) -> float:
        if self._gpu_method == "nvml":
            try:
                mw = pynvml.nvmlDeviceGetPowerUsage(self._nvml_handle)
                return mw / 1000.0
            except Exception:
                return 0.0

        if self._gpu_method == "hwmon":
            try:
                microwatts = self._read_file_int(self._gpu_hwmon_path)
                return microwatts / 1_000_000
            except (OSError, ValueError):
                return 0.0

        if self._gpu_method == "rapl_gt":
            try:
                watts, self._rapl_gt_last_energy_uj, self._rapl_gt_last_time = self._read_rapl_watts(
                    self._rapl_gt_path, self._rapl_gt_last_energy_uj,
                    self._rapl_gt_last_time, self._rapl_gt_max_energy,
                )
                return watts
            except (OSError, ValueError):
                return 0.0

        if self._gpu_method == "powermetrics":
            with self._powermetrics_lock:
                return self._last_powermetrics_gpu_w

        if self._gpu_method == "tdp":
            load_factor = 0.8 if self._active_requests else 0.1
            return self._effective_tdp_gpu * load_factor

        return 0.0

    def _read_powermetrics(self):
        """Read macOS powermetrics and update cached values. Called from sample loop."""
        if not self._powermetrics_available:
            return
        try:
            import plistlib
            result = subprocess.run(
                ["sudo", "-n", "powermetrics", "--samplers", "cpu_power,gpu_power",
                 "-n", "1", "-i", "100", "-f", "plist"],
                capture_output=True, timeout=5,
            )
            if result.returncode != 0:
                return
            data = plistlib.loads(result.stdout)
            processor = data.get("processor", {})
            with self._powermetrics_lock:
                # powermetrics reports in mW
                self._last_powermetrics_cpu_w = processor.get("cpu_power", 0) / 1000.0
                self._last_powermetrics_gpu_w = processor.get("gpu_power", 0) / 1000.0
        except Exception as e:
            logger.debug("powermetrics read failed: %s", e)

    # --- Sampling loop ---

    def _powermetrics_loop(self):
        """Separate thread for macOS powermetrics (slow subprocess calls)."""
        while not self._stop_event.is_set():
            self._read_powermetrics()
            self._stop_event.wait(1.0)  # Read every 1 second

    def _sample_loop(self):
        interval = 1.0 / settings.power_sample_hz

        # Start powermetrics in its own thread to avoid blocking samples
        if self._is_macos and self._powermetrics_available:
            pm_thread = threading.Thread(
                target=self._powermetrics_loop, daemon=True, name="powermetrics-reader",
            )
            pm_thread.start()

        while not self._stop_event.is_set():
            cpu_w = self._read_cpu_watts()
            gpu_w = self._read_gpu_watts()
            sample = PowerSample(
                timestamp=time.monotonic(),
                cpu_watts=cpu_w,
                gpu_watts=gpu_w,
            )
            with self._lock:
                self._samples.append(sample)
            self._stop_event.wait(interval)

    def start(self):
        if self._power_source == "none":
            logger.info("Power monitoring disabled (no sensors or TDP)")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True, name="power-sampler")
        self._thread.start()
        logger.info("Power sampling started at %dHz", settings.power_sample_hz)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
            logger.info("Power sampling stopped")
        # Clean up NVML
        if self._gpu_method == "nvml":
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass

    def begin_request(self) -> str:
        request_id = str(uuid.uuid4())
        with self._lock:
            self._active_requests[request_id] = ActiveRequest(start_time=time.monotonic())
        return request_id

    def end_request(self, request_id: str, tokens_out: int = 0) -> EnergyResult:
        with self._lock:
            req = self._active_requests.pop(request_id, None)
            if req is None:
                return EnergyResult(energy_joules=0.0, power_source=self._power_source)

            req.tokens_out = tokens_out
            end_time = time.monotonic()

            if self._power_source == "none":
                return EnergyResult(energy_joules=0.0, power_source="none")

            # Integrate power over request duration using samples
            total_joules = 0.0
            interval = 1.0 / settings.power_sample_hz

            for sample in self._samples:
                if sample.timestamp < req.start_time:
                    continue
                if sample.timestamp > end_time:
                    break

                concurrent = sum(
                    1 for r in self._active_requests.values()
                    if r.start_time <= sample.timestamp
                )
                concurrent += 1  # +1 for the request we just removed

                share = sample.total_watts * interval / concurrent
                total_joules += share

            return EnergyResult(energy_joules=total_joules, power_source=self._power_source)

    def get_current_power(self) -> dict:
        with self._lock:
            if self._samples:
                latest = self._samples[-1]
                return {
                    "cpu_watts": round(latest.cpu_watts, 2),
                    "gpu_watts": round(latest.gpu_watts, 2),
                    "total_watts": round(latest.total_watts, 2),
                    "power_source": self._power_source,
                    "cpu_method": self._cpu_method or "none",
                    "gpu_method": self._gpu_method or "none",
                    "active_requests": len(self._active_requests),
                }
        return {
            "cpu_watts": 0,
            "gpu_watts": 0,
            "total_watts": 0,
            "power_source": self._power_source,
            "cpu_method": self._cpu_method or "none",
            "gpu_method": self._gpu_method or "none",
            "active_requests": 0,
        }


# Singleton
monitor = PowerMonitor()
