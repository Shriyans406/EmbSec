"""Builds and holds every service the API uses (so routes and tests get them from one place)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from ..ai.service import AgentService
from ..audit import AuditLog
from ..build.engine import BuildStore
from ..db import migrate
from ..deploy.engine import DeployConfig, load_deploy_config
from ..deploy.store import DeploymentStore
from ..jobs import JobContext, JobFailed, JobManager
from ..pipeline.engine import PipelineEngine
from ..settings import Settings
from ..tools import ToolRegistry, build_registry
from .analysis import AnalysisService
from .builds import BuildService
from .deployments import DeployService
from .device import DeviceManager
from .firmware import FirmwareManager
from .testing import TestService


@dataclass
class Services:
    settings: Settings
    cfg: DeployConfig
    audit: AuditLog
    device: DeviceManager
    jobs: JobManager
    firmware: Optional[FirmwareManager] = None
    builds: Optional[BuildService] = None
    deploys: Optional[DeployService] = None
    tests: Optional[TestService] = None
    tools: Optional[ToolRegistry] = None
    agents: Optional[AgentService] = None
    analysis: Optional[AnalysisService] = None
    pipelines: Optional["PipelineEngine"] = None

    def start(self) -> None:
        self.jobs.start()

    def stop(self) -> None:
        self.jobs.stop()
        if self.agents:
            self.agents.close()


def _selftest_runner(ctx: JobContext) -> dict:
    """A harmless job for checking that the job system, approvals and logs work. It does not touch the board."""
    seconds = int(ctx.params.get("seconds", 3))
    for i in range(seconds):
        ctx.log(f"working ... {i + 1}/{seconds}")
        time.sleep(1)
    if ctx.params.get("fail"):
        raise JobFailed("the self-test was asked to fail", {"slept": seconds})
    return {"slept": seconds}


def build_services(settings: Settings, llm_factory=None) -> Services:
    migrate(settings.db_path)
    cfg = load_deploy_config(settings.root)
    BuildStore(settings.db_path)        # make sure the build and deployment tables exist
    DeploymentStore(settings.db_path)
    jobs = JobManager(settings.db_path, {"build": settings.build_workers, "board": 1, "system": 1, "ai": 2},
                      settings.proposal_ttl_s)
    jobs.register("selftest", _selftest_runner, "system")
    audit = AuditLog(settings.db_path)
    device = DeviceManager()

    s = Services(settings=settings, cfg=cfg, audit=audit, device=device, jobs=jobs)
    s.firmware = FirmwareManager(cfg)
    s.builds = BuildService(cfg, jobs, s.firmware)
    s.deploys = DeployService(cfg, jobs, device, audit)
    s.tests = TestService(settings.db_path, jobs, device)
    s.analysis = AnalysisService(cfg, s.firmware, device)
    s.tools = build_registry(s)
    s.agents = AgentService(settings, jobs, s.tools, llm_factory)
    s.pipelines = PipelineEngine(s, settings)
    return s