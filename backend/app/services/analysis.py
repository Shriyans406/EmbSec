"""Analysis service: the security scan and size report of projects / builds, and latency measurements on the
board. Used by the security and performance agents through the tool registry."""
from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Dict, Optional

from ..analysis.security_scan import scan_sources
from ..analysis.size_report import size_report
from ..build.engine import SUCCESS, BuildStore
from ..deploy.engine import DeployConfig
from ..errors import ApiError
from ..hardware.protocol import parse_reply
from .device import DeviceManager, validate_command
from .firmware import FirmwareManager


class AnalysisService:
    def __init__(self, cfg: DeployConfig, firmware: FirmwareManager, device: DeviceManager) -> None:
        self.cfg = cfg
        self.firmware = firmware
        self.device = device

    # ------------------------------------------------------------ sources
    def _build(self, build_id: int) -> dict:
        row = BuildStore(self.cfg.db_path).get(build_id)
        if not row or not row.get("result"):
            raise ApiError(404, "not_found", f"no build #{build_id}")
        return row["result"]

    def build_sources(self, build_id: int) -> Dict[str, str]:
        d = Path(self._build(build_id).get("artifact_dir") or "") / "app_src"
        if not d.is_dir():
            raise ApiError(404, "not_found", f"build #{build_id} has no source snapshot")
        return {p.name: p.read_text(encoding="utf-8", errors="replace") for p in sorted(d.iterdir()) if p.is_file()}

    def project_sources(self, project: str) -> Dict[str, str]:
        return {f["name"]: self.firmware.read_source(project, f["name"])["content"]
                for f in self.firmware.list_sources(project) if f["name"].endswith((".c", ".h"))}

    # ------------------------------------------------------------ security
    def security_scan(self, project: Optional[str] = None, build_id: Optional[int] = None) -> dict:
        if (project is None) == (build_id is None):
            raise ApiError(422, "invalid_arguments", "give exactly one of project or build_id")
        files = self.build_sources(build_id) if build_id else self.project_sources(project)
        out = scan_sources(files)
        out["target"] = {"build_id": build_id} if build_id else {"project": project}
        return out

    # ------------------------------------------------------------ size
    def size_report(self, build_id: int) -> dict:
        build = self._build(build_id)
        if build.get("status") != SUCCESS:
            raise ApiError(409, "build_not_successful", f"build #{build_id} is {build.get('status')}; no sizes")
        previous = None
        for row in BuildStore(self.cfg.db_path).list(build["project"], 50):
            if row["id"] < build_id and row["status"] == SUCCESS:
                prev = BuildStore(self.cfg.db_path).get(row["id"])
                previous = prev["result"] if prev else None
                break
        nm = Path(self.cfg.build.size_exe).with_name("arm-none-eabi-nm.exe")
        try:
            sources = self.build_sources(build_id)
        except ApiError:
            sources = {}
        return size_report(build, previous, nm, sources)

    # ------------------------------------------------------------ latency
    def measure_latency(self, command: str, count: int, who: str) -> dict:
        validate_command(command, operator=False)
        times, errors, replies = [], 0, []
        with self.device.session(who) as send:
            for _ in range(count):
                t0 = time.perf_counter()
                reply = send(command)
                times.append((time.perf_counter() - t0) * 1000)
                if not parse_reply(reply).ok:
                    errors += 1
                if len(replies) < 3 and reply not in replies:
                    replies.append(reply)
        times.sort()
        p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
        return {"command": command, "count": count, "errors": errors, "replies": replies,
                "min_ms": round(times[0], 2), "mean_ms": round(statistics.fmean(times), 2),
                "median_ms": round(statistics.median(times), 2), "p95_ms": round(p95, 2),
                "max_ms": round(times[-1], 2)}
