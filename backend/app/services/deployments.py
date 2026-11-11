"""Deployment service: proposals that wait for a human, and the jobs that flash the board once approved.

An AI agent can PROPOSE a deployment or a recovery (this runs the read-only pre-flight checks and creates a job
that waits for approval). Only an operator can approve it, by sending the confirmation text. Approval is tied to the
exact firmware image that was proposed: if the file changes afterwards, the job refuses to run.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Callable, List

from ..audit import AuditLog
from ..deploy.board import Board
from ..deploy.engine import (SUCCESS, Approval, DeployConfig, DeployError, deploy, known_good_manifest,
                             promote_known_good, recover, recovery_image, resolve_build_image, resolve_known_good,
                             resolve_stock, run_preflight)
from ..deploy.store import DeploymentStore
from ..errors import ApiError
from ..jobs import JobContext, JobFailed, JobManager
from ..security import Principal
from .device import DeviceManager


class DeployService:
    def __init__(self, cfg: DeployConfig, jobs: JobManager, device: DeviceManager, audit: AuditLog,
                 board_factory: Callable[[], object] = Board) -> None:
        self.cfg = cfg
        self.jobs = jobs
        self.device = device
        self.audit = audit
        self.board_factory = board_factory
        jobs.register("deploy", self._run_deploy, "board")
        jobs.register("recover", self._run_recover, "board")

    # ------------------------------------------------------------ helpers
    def _store(self) -> DeploymentStore:
        return DeploymentStore(self.cfg.db_path)

    def _preflight(self, image, kind: str, who: str):
        with self.device.exclusive(f"pre-flight check for {who}", wait_s=3.0):
            return run_preflight(self.cfg, self.board_factory(), image, self._store(), kind=kind)

    @staticmethod
    def _preflight_failure(checks: list) -> ApiError:
        """An error that says WHY the proposal was refused (the board is missing, the image changed, ...)."""
        reasons = "; ".join(f"{c['name']}: {c['detail']}" for c in checks if c["status"] == "FAIL")
        return ApiError(409, "preflight_failed", f"the pre-flight checks failed - {reasons} (nothing was proposed)",
                        checks)

    def _next_step(self, job_id: int, token: str) -> str:
        minutes = max(1, self.jobs.ttl // 60)
        return (f"WAITING FOR A HUMAN. An operator must approve job {job_id} by sending POST /jobs/{job_id}/approve "
                f"with confirm='{token}'. The proposal expires in {minutes} minutes. Nothing has been flashed.")

    # ------------------------------------------------------------ proposing
    def propose_deploy(self, build_id: int, requested_by: str, rollback: bool = True) -> dict:
        image = resolve_build_image(self.cfg, build_id)                # DeployError (400): unknown / failed build
        pf = self._preflight(image, "deploy", requested_by)
        checks = [asdict(c) for c in pf.checks]
        if not pf.ok:
            raise self._preflight_failure(checks)
        uid = pf.before.uid
        job = self.jobs.submit(
            "deploy",
            {"build_id": build_id, "image_sha256": image.sha256, "rollback": rollback, "checks": checks,
             "board_uid": uid, "recovery": pf.recovery.label if pf.recovery else None},
            requested_by, summary=f"deploy build #{build_id} ({image.project}) to board {uid or 'unknown'}",
            needs_approval=True, confirm_token=str(build_id))
        job["next_step"] = self._next_step(job["id"], str(build_id))
        return job

    def propose_recover(self, target: str, requested_by: str) -> dict:
        image = self._recovery_image(target)
        pf = self._preflight(image, "recover", requested_by)
        checks = [asdict(c) for c in pf.checks]
        if not pf.ok:
            raise self._preflight_failure(checks)
        job = self.jobs.submit(
            "recover", {"target": target, "image_sha256": image.sha256, "checks": checks,
                        "board_uid": pf.before.uid},
            requested_by, summary=f"recover the board to: {image.label}", needs_approval=True,
            confirm_token="RECOVER")
        job["next_step"] = self._next_step(job["id"], "RECOVER")
        return job

    def _recovery_image(self, target: str):
        img = {"stock": resolve_stock, "known-good": resolve_known_good}.get(target, recovery_image)(self.cfg)
        if img is None:
            raise ApiError(409, "no_recovery_image", f"there is no usable recovery image for target '{target}'")
        return img

    # ------------------------------------------------------------ running (after approval)
    def _approval_for(self, ctx: JobContext) -> Approval:
        job = self.jobs.get(ctx.job_id) or {}
        return Approval(by=f"{job.get('approved_by') or 'unknown'} approved job #{ctx.job_id}")

    def _run_deploy(self, ctx: JobContext) -> dict:
        p = ctx.params
        image = resolve_build_image(self.cfg, p["build_id"])
        if image.sha256 != p["image_sha256"]:
            raise JobFailed("the firmware image changed after it was approved; propose the deployment again")
        with self.device.exclusive(f"deploy job #{ctx.job_id}"):
            res = deploy(self.cfg, self.board_factory(), image, self._approval_for(ctx),
                         rollback=bool(p.get("rollback", True)), store=self._store(), on_line=ctx.log)
        out = res.to_dict()
        if res.status != SUCCESS:
            raise JobFailed(f"deployment {res.status}: {res.message}", out)
        return out

    def _run_recover(self, ctx: JobContext) -> dict:
        p = ctx.params
        image = self._recovery_image(p["target"])
        if image.sha256 != p["image_sha256"]:
            raise JobFailed("the recovery image changed after it was approved; propose the recovery again")
        with self.device.exclusive(f"recovery job #{ctx.job_id}"):
            res = recover(self.cfg, self.board_factory(), p["target"], self._approval_for(ctx),
                          store=self._store(), on_line=ctx.log)
        out = res.to_dict()
        if res.status != SUCCESS:
            raise JobFailed(f"recovery {res.status}: {res.message}", out)
        return out

    # ------------------------------------------------------------ reading
    def history(self, limit: int = 20) -> List[dict]:
        return self._store().list(max(1, min(limit, 100)))

    def get(self, dep_id: int) -> dict:
        row = self._store().get(dep_id)
        if not row:
            raise ApiError(404, "not_found", f"no deployment {dep_id}")
        return row

    def known_good(self) -> dict:
        manifest = known_good_manifest(self.cfg)
        kg = resolve_known_good(self.cfg)
        stock = resolve_stock(self.cfg)
        return {
            "known_good": {"present": kg is not None, "manifest": manifest,
                           "valid": bool(kg and not kg.problems), "problems": kg.problems if kg else []},
            "factory_backup": {"present": stock is not None, "valid": bool(stock and not stock.problems),
                               "size": stock.size if stock else 0},
            "registered_boards": [d["uid"] for d in self._store().devices()],
        }

    def promote(self, build_id: int, confirm: str, who: Principal) -> dict:
        expected = f"PROMOTE {build_id}"
        if (confirm or "").strip() != expected:
            self.audit.write(who.name, who.role, "known_good.promote", f"build:{build_id}", "refused:confirmation_mismatch")
            raise ApiError(400, "confirmation_mismatch", f"type exactly '{expected}' to confirm")
        try:
            manifest = promote_known_good(self.cfg, build_id, self._store())
        except DeployError as e:
            self.audit.write(who.name, who.role, "known_good.promote", f"build:{build_id}", "refused:not_proven", str(e))
            raise
        self.audit.write(who.name, who.role, "known_good.promote", f"build:{build_id}", "ok")
        return manifest