"""
power_source.py — Power source abstraction + once-at-startup detection

This module defines the source layer of the hardware-power-tracking stage:

  * ``PowerReading``  — an immutable timestamped observation. Numeric fields are
                        watts or ``None``; ``None`` (never ``0``) means "this
                        component is not reported / unavailable" so a genuine
                        measured ``0.0 W`` stays distinguishable (Req 3.8).
  * ``Quality``       — the honesty flag: ``measured`` | ``estimated`` | ``unavailable``.
  * ``PowerSource``   — a ``typing.Protocol`` every concrete source implements:
                        a ``name`` attribute plus ``read() -> PowerReading`` whose
                        contract is that it NEVER raises, returns an ``unavailable``
                        reading on any failure, and makes no network call.
  * ``UtilizationEstimateSource`` — the psutil-backed ESTIMATED tier (default on M5).
  * ``NvidiaSmiSource``           — the discrete-GPU MEASURED tier (absent on M5).
  * ``PowermetricsSource``        — the Apple Silicon MEASURED tier + a pure parser.
  * ``probe_powermetrics_authorized`` — the gated, non-interactive Capability_Probe.
  * ``detect_power_source``       — precedence selection, run once at startup.

Design references: the "Components and Interfaces → power_source.py",
"Power source detection and tiers", and Correctness Property 1 sections of the
hardware-power-tracking design document.

Imports of ``psutil`` and ``subprocess`` are LAZY (inside functions) so this
module imports cleanly even where those are absent, mirroring ``redactor.py``'s
``load_ner_model`` degrade-on-any-failure style. Nothing here makes a network
call.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Exactly one honesty flag per reading (Req 2.10).
Quality = Literal["measured", "estimated", "unavailable"]


# ---------------------------------------------------------------------------
# PowerReading (Req 2.10, 2.12, 3.8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PowerReading:
    """A single timestamped power observation.

    Numeric fields are watts or ``None``. ``None`` means "this source does not
    report this component" or "unavailable" — NEVER a fabricated ``0`` (Req 3.8).
    A measured ``0.0 W`` is a legitimate value and must remain distinguishable
    from ``None``.

    ``timestamp`` is a wall-clock reading (``time.time()``) used for window
    selection so it matches the request-path timestamps. ``quality`` is exactly
    one honesty flag. The dataclass is ``frozen`` so a reading cannot be mutated
    (and therefore cannot be relabelled in place) after creation, which makes
    the "quality flag is never upgraded" invariant structurally hard to break.
    """

    timestamp: float
    cpu_watts: float | None
    gpu_watts: float | None
    package_watts: float | None
    source: str
    quality: Quality


# ---------------------------------------------------------------------------
# PowerSource protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class PowerSource(Protocol):
    """Interface implemented by every concrete power source.

    Contract: ``read()`` NEVER raises, returns an ``unavailable`` ``PowerReading``
    on any failure, and makes no network call.
    """

    name: str

    def read(self) -> PowerReading:
        """Return one ``PowerReading`` (never raises; unavailable on failure)."""
        ...


def _unavailable_reading(source: str) -> PowerReading:
    """Build an ``unavailable`` reading for ``source`` with all watts ``None``."""
    return PowerReading(
        timestamp=time.time(),
        cpu_watts=None,
        gpu_watts=None,
        package_watts=None,
        source=source,
        quality="unavailable",
    )


# ---------------------------------------------------------------------------
# UtilizationEstimateSource — estimated tier (Req 2.6, 2.8, 2.11, 3.4)
# ---------------------------------------------------------------------------

class UtilizationEstimateSource:
    """psutil-backed ESTIMATED tier (Req 2.6, 2.8, 2.11).

    ``read()`` derives an estimated wattage from ``psutil.cpu_percent()`` and a
    configurable TDP model::

        est_watts = idle_watts + (cpu_percent / 100) * (tdp_watts - idle_watts)

    and returns a reading tagged ``estimated`` (package + cpu populated, gpu
    ``None`` on the unified-memory M5). If ``psutil`` is not importable OR the
    read fails for any reason, it returns an ``unavailable`` reading and never
    raises. This is the DEFAULT active source on the M5 target (Req 2.9).
    """

    name = "utilization-estimate"

    def __init__(self, tdp_watts: float = 20.0, idle_watts: float = 2.0):
        self.tdp_watts = tdp_watts
        self.idle_watts = idle_watts

    def read(self) -> PowerReading:
        try:
            import psutil  # lazy — a missing dependency degrades, never crashes import

            cpu_percent = psutil.cpu_percent()
            est_watts = self.idle_watts + (cpu_percent / 100.0) * (
                self.tdp_watts - self.idle_watts
            )
            return PowerReading(
                timestamp=time.time(),
                cpu_watts=est_watts,
                gpu_watts=None,  # unified-memory SoC: folded into package
                package_watts=est_watts,
                source=self.name,
                quality="estimated",
            )
        except Exception as err:  # noqa: BLE001 — degrade on ANY failure (Req 2.8, 3.4)
            logger.warning("Utilization estimate unavailable: %s", err)
            return _unavailable_reading(self.name)


# ---------------------------------------------------------------------------
# NvidiaSmiSource — discrete-GPU measured tier (Req 2.7, 3.4)
# ---------------------------------------------------------------------------

class NvidiaSmiSource:
    """Discrete-GPU MEASURED tier (Req 2.7). Absent on the M5 target.

    ``read()`` runs ``nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits``
    via subprocess (lazy import) and returns a ``measured`` GPU reading on a
    clean parse. If the binary is absent (``FileNotFoundError``) or any failure
    occurs, it returns an ``unavailable`` reading and never raises.
    """

    name = "nvidia-smi"

    def __init__(self, timeout_s: float = 0.5):
        self.timeout_s = timeout_s

    @staticmethod
    def available() -> bool:
        """Return True iff the ``nvidia-smi`` binary is present on PATH.

        Lets detection skip this source without invoking it (Req 2.7).
        """
        import shutil  # lazy

        return shutil.which("nvidia-smi") is not None

    def read(self) -> PowerReading:
        try:
            import subprocess  # lazy

            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=power.draw",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
            if completed.returncode != 0:
                return _unavailable_reading(self.name)
            watts = float(completed.stdout.strip().splitlines()[0].strip())
            return PowerReading(
                timestamp=time.time(),
                cpu_watts=None,
                gpu_watts=watts,
                package_watts=watts,
                source=self.name,
                quality="measured",
            )
        except Exception as err:  # noqa: BLE001 — missing binary / parse / timeout
            logger.warning("nvidia-smi reading unavailable: %s", err)
            return _unavailable_reading(self.name)


# ---------------------------------------------------------------------------
# PowermetricsSource — Apple Silicon measured tier (Req 2.5, 2.12, 3.2, 3.4)
# ---------------------------------------------------------------------------

def parse_powermetrics_output(
    text: str,
) -> tuple[float | None, float | None, float | None]:
    """Pure parser: extract (cpu_watts, gpu_watts, package_watts) from output.

    ``powermetrics --samplers cpu_power`` reports power lines such as::

        CPU Power: 1234 mW
        GPU Power: 56 mW
        Combined Power (CPU + GPU + ANE): 1290 mW

    Values are reported in milliwatts and converted to watts here. The
    "Combined Power" / "Package Power" line maps to ``package_watts``. Any
    component whose line is missing or malformed is returned as ``None`` (never
    a fabricated number), so a partial/malformed parse yields ``(None, None,
    None)`` and the caller reports ``unavailable`` (Req 2.12, 3.8).
    """

    def _extract_mw(label_fragment: str) -> float | None:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            low = line.lower()
            if label_fragment in low and ":" in line:
                # Take the text after the first colon and read the leading number.
                after = line.split(":", 1)[1].strip()
                token = after.split()[0] if after.split() else ""
                try:
                    return float(token)
                except ValueError:
                    return None
        return None

    cpu_mw = _extract_mw("cpu power")
    gpu_mw = _extract_mw("gpu power")
    # "Combined Power (...)" is the SoC package figure; some builds print
    # "Package Power". Try both.
    package_mw = _extract_mw("combined power")
    if package_mw is None:
        package_mw = _extract_mw("package power")

    def _to_w(mw: float | None) -> float | None:
        return None if mw is None else mw / 1000.0

    return _to_w(cpu_mw), _to_w(gpu_mw), _to_w(package_mw)


class PowermetricsSource:
    """Apple Silicon MEASURED tier (Req 2.5, 2.12).

    ``read()`` runs ``sudo -n powermetrics --samplers cpu_power -n 1 -i <ms>``
    via subprocess with a bounded timeout. The ``-n`` (non-interactive) sudo
    flag guarantees NO password prompt: if passwordless sudoers is not
    configured the call fails immediately and ``read()`` returns an
    ``unavailable`` reading (Req 3.2). On a clean parse it returns a ``measured``
    reading with the CPU / GPU / package components. Never raises, never prompts.
    """

    name = "powermetrics"

    def __init__(self, interval_ms: int = 200, timeout_s: float = 0.5):
        self.interval_ms = interval_ms
        # Telemetry_Time_Budget default (Req 3.5). Callers may pass a longer,
        # still-bounded cap, but the source stays bounded regardless.
        self.timeout_s = timeout_s

    def read(self) -> PowerReading:
        try:
            import subprocess  # lazy

            completed = subprocess.run(
                [
                    "sudo",
                    "-n",
                    "powermetrics",
                    "--samplers",
                    "cpu_power",
                    "-n",
                    "1",
                    "-i",
                    str(self.interval_ms),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
            if completed.returncode != 0:
                return _unavailable_reading(self.name)
            cpu_w, gpu_w, package_w = parse_powermetrics_output(completed.stdout)
            if cpu_w is None and gpu_w is None and package_w is None:
                # Malformed / partial output → unavailable, not a fabricated 0.
                return _unavailable_reading(self.name)
            return PowerReading(
                timestamp=time.time(),
                cpu_watts=cpu_w,
                gpu_watts=gpu_w,
                package_watts=package_w,
                source=self.name,
                quality="measured",
            )
        except Exception as err:  # noqa: BLE001 — timeout / non-zero / missing binary
            logger.warning("powermetrics reading unavailable: %s", err)
            return _unavailable_reading(self.name)


# ---------------------------------------------------------------------------
# Capability probe — gated behind POWER_TRY_MEASURED (Req 2.4, 3.2, 3.3)
# ---------------------------------------------------------------------------

def probe_powermetrics_authorized(timeout_s: float = 5.0) -> bool:
    """Non-interactive Capability_Probe for powermetrics, gated by an env flag.

    Returns ``False`` IMMEDIATELY — WITHOUT invoking ``sudo`` / any subprocess —
    unless ``os.environ["POWER_TRY_MEASURED"] == "1"``. This is the critical
    directive: by DEFAULT the backend never runs ``sudo`` at all (no password
    prompt, and no logged failed-sudo attempt in hardened environments) and
    goes straight to the estimated tier (Req 3.2, 3.3).

    ONLY when the flag is exactly ``"1"`` does it run
    ``sudo -n powermetrics --samplers cpu_power -n 1 -i 200`` once with the given
    timeout and return ``True`` ONLY on a clean exit (return code 0) within the
    budget. ``-n`` guarantees no prompt; a ``TimeoutExpired``, non-zero exit, or
    missing binary all return ``False`` (fail-closed → not authorized). Never
    raises.
    """
    # FIRST LINE of logic: bail out before touching subprocess at all (Req 3.2).
    if os.environ.get("POWER_TRY_MEASURED") != "1":
        return False

    try:
        import subprocess  # lazy — only reached under the opt-in flag

        completed = subprocess.run(
            [
                "sudo",
                "-n",
                "powermetrics",
                "--samplers",
                "cpu_power",
                "-n",
                "1",
                "-i",
                "200",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return completed.returncode == 0
    except Exception as err:  # noqa: BLE001 — fail closed on anything (Req 2.4)
        logger.warning("powermetrics capability probe failed closed: %s", err)
        return False


# ---------------------------------------------------------------------------
# detect_power_source — precedence selection, once at startup (Req 2.2, 2.3, 2.9)
# ---------------------------------------------------------------------------

def detect_power_source(*, tdp_watts: float = 20.0) -> tuple[PowerSource, bool]:
    """Select the active power source ONCE at startup.

    Precedence: ``powermetrics → nvidia-smi → utilization-estimate``; the first
    source that is BOTH available AND authorized is selected (Req 2.3):

      * ``powermetrics`` is selected only if ``probe_powermetrics_authorized()``
        returns True — and that probe is itself gated behind ``POWER_TRY_MEASURED``,
        so on the DEFAULT M5 target (flag unset) no ``sudo`` runs and this tier
        is skipped without any invocation.
      * ``nvidia-smi`` is selected only if its binary is present on PATH
        (``shutil.which``), an unprivileged availability check (Req 2.7).
      * otherwise the ``UtilizationEstimateSource`` (``estimated``); and if even
        ``psutil`` is unavailable its readings come back ``unavailable`` (Req 2.9).

    Returns ``(active_source, measured_authorized)`` where ``measured_authorized``
    is True only when a Privileged_Source (powermetrics or nvidia-smi) was
    selected. Runs exactly once from the lifespan; no probe runs during a request.

    On the M5 target with ``POWER_TRY_MEASURED`` unset, this returns
    ``(UtilizationEstimateSource, False)``.
    """
    if probe_powermetrics_authorized():
        return PowermetricsSource(), True

    if NvidiaSmiSource.available():
        return NvidiaSmiSource(), True

    return UtilizationEstimateSource(tdp_watts=tdp_watts), False
