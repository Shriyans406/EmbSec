"""Builds and holds every service the API uses (so routes and tests get them from one place)."""
from __future__ import annotations

import time
from dataclasses import dataclass

from ..audit import AuditLog
from ..build.engine import BuildStore
from ..db import migrate
from ..deploy.engine import DeployConfig, load_deploy_config
from ..deploy.store import DeploymentStore
from ..jobs import JobContext, JobFailed, JobManager
from ..settings import Settings
from .device import DeviceManager


@dataclass
class Services:
    settings: Settings
    cfg: DeployConfig
    audit: AuditLog
    device: DeviceManager
    jobs: JobManager

    def start(self) -> None:
        self.jobs.start()

    def stop(self) -> None:
        self.jobs.stop()


def _selftest_runner(ctx: JobContext) -> dict:
    """A harmless job for checking that the job system, approvals and logs work. It does not touch the board."""
    seconds = int(ctx.params.get("seconds", 3))
    for i in range(seconds):
        ctx.log(f"working ... {i + 1}/{seconds}")
        time.sleep(1)
    if ctx.params.get("fail"):
        raise JobFailed("the self-test was asked to fail", {"slept": seconds})
    return {"slept": seconds}


def build_services(settings: Settings) -> Services:
    migrate(settings.db_path)
    cfg = load_deploy_config(settings.root)
    BuildStore(settings.db_path)        # make sure the build and deployment tables exist
    DeploymentStore(settings.db_path)
    jobs = JobManager(settings.db_path, {"build": settings.build_workers, "board": 1, "system": 1},
                      settings.proposal_ttl_s)
    jobs.register("selftest", _selftest_runner, "system")
    return Services(settings=settings, cfg=cfg, audit=AuditLog(settings.db_path), device=DeviceManager(), jobs=jobs)