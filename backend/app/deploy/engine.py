"""Deployment and recovery engine.

Safety rules enforced here (the CLI and, later, the agents all go through this code):
  1. Only a validated UF2 image is ever written; a build's image must still match its build record.
  2. Nothing is flashed without an explicit Approval.
  3. A recovery image (known-good firmware, or the factory backup) must exist before we flash anything.
  4. The board must be present, unambiguous and a registered board (or explicitly trusted).
  5. After flashing, the new firmware must prove itself: the right version AND build number answer on
     the serial port and pass a health check. If it does not, the previous good firmware is restored.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from ..build.config import BuildConfig, load_config
from ..build.engine import SUCCESS as BUILD_SUCCESS
from ..build.engine import BuildStore
from ..build.uf2 import validate_uf2
from .board import (ABSENT, BOOTSEL, MULTIPLE, RUNNING, UNRESPONSIVE, BoardState, BootloaderError, FlashError)
from .store import DeployLog, DeploymentStore, now_iso

# deployment statuses
SUCCESS = "SUCCESS"                  # new firmware flashed and verified
ABORTED = "ABORTED"                  # a pre-flight check failed or nobody approved; the board was not touched
FAILED = "FAILED"                    # something went wrong (see message); no rollback was needed or possible
ROLLED_BACK = "ROLLED_BACK"          # the new firmware failed verification; the previous good firmware is back
ROLLBACK_FAILED = "ROLLBACK_FAILED"  # the new firmware failed AND restoring the old one failed: manual recovery
DRY_RUN = "DRY_RUN"                  # checks only


class DeployError(Exception):
    """A problem the caller can fix (unknown build, missing file...)."""


@dataclass(frozen=True)
class DeployConfig:
    build: BuildConfig
    known_good_dir: Path
    stock_backup: Path
    deployments_dir: Path

    @property
    def db_path(self) -> Path:
        return self.build.db_path


def load_deploy_config(root: Optional[Path] = None) -> DeployConfig:
    b = load_config(root)
    r = b.root
    return DeployConfig(b, r / "firmware" / "known_good", r / "backups" / "shrike_stock_backup.uf2",
                        r / "data" / "deployments")


@dataclass
class Approval:
    by: str        # who/what approved, e.g. "cli: typed the build number"
    note: str = ""


@dataclass
class Image:
    path: Path
    label: str
    origin: str                      # build | known_good | stock | file
    sha256: str = ""
    size: int = 0
    build_id: Optional[int] = None
    src_hash: Optional[str] = None   # first 8 hex characters of the sources hash
    fw_version: Optional[str] = None
    project: Optional[str] = None
    problems: List[str] = field(default_factory=list)

    @property
    def protocol_verifiable(self) -> bool:
        """True if we know which version/build the board must report after flashing."""
        return self.fw_version is not None and self.build_id is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        return d


@dataclass
class Check:
    name: str
    status: str   # PASS | WARN | FAIL
    detail: str


@dataclass
class Preflight:
    checks: List[Check]
    before: BoardState
    recovery: Optional[Image]

    @property
    def ok(self) -> bool:
        return not any(c.status == "FAIL" for c in self.checks)


@dataclass
class DeployResult:
    id: int = 0
    kind: str = "deploy"
    status: str = ""
    message: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    approved_by: str = ""
    image: dict = field(default_factory=dict)
    checks: List[dict] = field(default_factory=list)
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    method: str = ""
    failure: dict = field(default_factory=dict)
    rollback: dict = field(default_factory=dict)
    log: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- images
def _load_image(cfg: DeployConfig, path: Path, label: str, origin: str, **meta) -> Image:
    path = Path(path)
    if not path.is_file():
        raise DeployError(f"image file not found: {path}")
    data = path.read_bytes()
    rep = validate_uf2(data, cfg.build.flash_capacity_bytes)
    return Image(path=path, label=label, origin=origin, sha256=rep["sha256"], size=rep["bytes"],
                 problems=list(rep["problems"]), **meta)


def resolve_build_image(cfg: DeployConfig, build_id: int) -> Image:
    row = BuildStore(cfg.db_path).get(build_id)
    if not row or not row.get("result"):
        raise DeployError(f"no build #{build_id} (see: python -m app.build.cli history)")
    r = row["result"]
    if r["status"] != BUILD_SUCCESS:
        raise DeployError(f"build #{build_id} did not succeed (status {r['status']}); only successful builds can be deployed")
    path = cfg.build.artifacts_dir / str(build_id) / "firmware.uf2"
    label = f"build #{build_id} ({r['project']}, sources {r['sources_sha256'][:8]}, harness {r['harness_version']})"
    img = _load_image(cfg, path, label, "build", build_id=build_id, src_hash=r["sources_sha256"][:8],
                      fw_version=r["harness_version"], project=r["project"])
    if img.sha256 != r["uf2_sha256"]:
        img.problems.append("the file has changed since it was built (sha256 does not match the build record)")
    return img


def known_good_manifest(cfg: DeployConfig) -> Optional[dict]:
    p = cfg.known_good_dir / "known_good.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def resolve_known_good(cfg: DeployConfig) -> Optional[Image]:
    m = known_good_manifest(cfg)
    uf2 = cfg.known_good_dir / "known_good.uf2"
    if not m or not uf2.is_file():
        return None
    img = _load_image(cfg, uf2, f"known-good firmware ({m.get('label', '?')})", "known_good",
                      build_id=m.get("build_id"), src_hash=m.get("src_hash"), fw_version=m.get("fw_version"),
                      project=m.get("project"))
    if img.sha256 != m.get("uf2_sha256"):
        img.problems.append("known-good file does not match its manifest (it was modified)")
    return img


def resolve_stock(cfg: DeployConfig) -> Optional[Image]:
    if not cfg.stock_backup.is_file():
        return None
    return _load_image(cfg, cfg.stock_backup, "factory firmware backup (as shipped on the board)", "stock")


def resolve_file(cfg: DeployConfig, path: Path) -> Image:
    p = Path(path)
    return _load_image(cfg, p, f"file {p.name} (not from the build system)", "file")


def recovery_image(cfg: DeployConfig, avoid_sha: str = "") -> Optional[Image]:
    """Best image to restore: known-good first, then the factory backup. Skips invalid files."""
    for img in (resolve_known_good(cfg), resolve_stock(cfg)):
        if img and not img.problems and img.sha256 != avoid_sha:
            return img
    return None


# ---------------------------------------------------------------- pre-flight
def run_preflight(cfg: DeployConfig, board, image: Image, store: DeploymentStore, *, kind: str = "deploy",
                  trust_new_board: bool = False, allow_no_recovery: bool = False) -> Preflight:
    checks: List[Check] = []

    if image.problems:
        checks.append(Check("image", "FAIL", "; ".join(image.problems)))
    else:
        extra = " and matches its build record" if image.origin == "build" else ""
        checks.append(Check("image", "PASS", f"{image.label}; valid UF2, {image.size:,} bytes, "
                                              f"sha256 {image.sha256[:12]}{extra}"))
    if image.origin in ("file",):
        checks.append(Check("image origin", "WARN", "this file did not come from the build system and has "
                                                    "no build record; only a limited check is possible afterwards"))

    rec = recovery_image(cfg, avoid_sha=image.sha256 if kind == "deploy" else "")
    if kind == "deploy":
        if rec is None:
            checks.append(Check("recovery image", "WARN" if allow_no_recovery else "FAIL",
                                "no usable known-good firmware or factory backup to fall back on"))
        else:
            checks.append(Check("recovery image", "PASS", f"if anything goes wrong: {rec.label}"))

    before = board.identify()
    if before.mode == ABSENT:
        checks.append(Check("board", "FAIL", "no board found on USB. Plug it in (or hold BOOT while plugging)."))
    elif before.mode == MULTIPLE:
        checks.append(Check("board", "FAIL", "more than one board is attached: " + before.detail))
    elif before.mode == UNRESPONSIVE:
        checks.append(Check("board", "WARN", before.summary() + ". Will try the 1200-baud reset."))
    else:
        checks.append(Check("board", "PASS", before.summary()))

    uid = before.uid
    devices = store.devices()
    known = {d["uid"] for d in devices}
    if before.mode in (ABSENT, MULTIPLE):
        pass
    elif not uid:
        checks.append(Check("board identity", "WARN",
                            "board id is unknown while it is in the bootloader; it is checked after flashing"))
    elif uid in known:
        checks.append(Check("board identity", "PASS", f"board {uid} is a registered board"))
    elif known and not trust_new_board:
        checks.append(Check("board identity", "FAIL", f"board {uid} is NOT one of the registered boards "
                            f"({', '.join(sorted(known))}). If you really want to use it: --trust-new-board"))
    else:
        checks.append(Check("board identity", "WARN", f"first time we see board {uid}; it will be registered"))

    if (kind == "deploy" and before.mode == RUNNING and image.build_id is not None
            and before.build_id == str(image.build_id) and before.src_hash == image.src_hash):
        checks.append(Check("already running", "WARN", "the board already runs exactly this build"))

    return Preflight(checks, before, rec)


# ---------------------------------------------------------------- flashing steps
@dataclass
class _Outcome:
    ok: bool
    stage: str
    message: str
    state: Optional[BoardState]
    method: str
    changed: bool


def _verify(board, image: Image, before: BoardState, state: BoardState, log: DeployLog) -> List[str]:
    problems: List[str] = []
    if image.protocol_verifiable:
        if state.mode != RUNNING:
            problems.append(f"the new firmware is not answering: {state.summary()}")
            return problems
        if state.fw_version != image.fw_version:
            problems.append(f"firmware version is {state.fw_version}, expected {image.fw_version}")
        if state.build_id != str(image.build_id):
            problems.append(f"firmware reports build '{state.build_id}', expected '{image.build_id}'")
        if image.src_hash and state.src_hash != image.src_hash:
            problems.append(f"firmware reports sources '{state.src_hash}', expected '{image.src_hash}'")
        ok, detail = board.health_check(log)
        log(f"health check: {detail}")
        if not ok:
            problems.append("health check failed: " + detail)
        if before.uid and state.uid and before.uid != state.uid:
            problems.append(f"board id changed from {before.uid} to {state.uid}: a different board?")
    else:
        if state.mode in (ABSENT, BOOTSEL, MULTIPLE):
            problems.append(f"the board did not come back: {state.summary()}")
        else:
            log(f"limited check for this image type: the board is back on USB ({state.summary()})")
    return problems


def _flash_and_verify(board, image: Image, before: BoardState, log: DeployLog) -> _Outcome:
    log(f"entering the bootloader ...")
    try:
        method = board.enter_bootloader(log)
    except BootloaderError as e:
        log(f"could not enter the bootloader: {e}")
        return _Outcome(False, "bootloader", str(e), None, "", False)
    log(f"bootloader ready (via {method})")
    try:
        board.flash(image.path, log)
    except FlashError as e:
        log(f"flash failed: {e}")
        return _Outcome(False, "flash", str(e), None, method, True)
    state = board.wait_for_firmware(0, log)
    log(f"after flashing: {state.summary()}")
    problems = _verify(board, image, before, state, log)
    if problems:
        for p in problems:
            log(f"VERIFY FAILED: {p}")
        return _Outcome(False, "verify", "; ".join(problems), state, method, True)
    log("verification passed")
    return _Outcome(True, "done", "ok", state, method, True)


def _finish(cfg: DeployConfig, store: DeploymentStore, res: DeployResult, dep_id: int, log: DeployLog,
            t0: float, uid: str) -> DeployResult:
    res.id = dep_id
    res.finished_at = now_iso()
    res.duration_s = round(time.monotonic() - t0, 1)
    res.log = list(log.lines)
    d = cfg.deployments_dir / str(dep_id)
    log.save(d / "deploy.log")
    (d / "result.json").write_text(json.dumps(res.to_dict(), indent=2) + "\n", encoding="utf-8")
    store.finish(dep_id, res.status, res.message, res.duration_s, uid, res.to_dict())
    return res


def _start(store: DeploymentStore, kind: str, image: Image, approval: Optional[Approval], uid: str) -> int:
    return store.start(kind, image.label, image.origin, image.sha256, image.build_id, uid,
                       approval.by if approval else "NONE")


# ---------------------------------------------------------------- deploy
def deploy(cfg: DeployConfig, board, image: Image, approval: Optional[Approval], *, rollback: bool = True,
           dry_run: bool = False, trust_new_board: bool = False, allow_no_recovery: bool = False,
           store: Optional[DeploymentStore] = None, on_line: Optional[Callable[[str], None]] = None) -> DeployResult:
    store = store or DeploymentStore(cfg.db_path)
    log = DeployLog(on_line)
    t0 = time.monotonic()
    pf = run_preflight(cfg, board, image, store, kind="deploy", trust_new_board=trust_new_board,
                       allow_no_recovery=allow_no_recovery)
    res = DeployResult(kind="deploy", started_at=now_iso(), image=image.to_dict(),
                       checks=[asdict(c) for c in pf.checks], before=pf.before.to_dict(),
                       approved_by=approval.by if approval else "")
    if dry_run:
        res.status, res.message = DRY_RUN, "pre-flight checks only; nothing was changed"
        res.finished_at = now_iso()
        return res

    dep_id = _start(store, "deploy", image, approval, pf.before.uid)
    log(f"deployment #{dep_id}: {image.label}")
    if not pf.ok:
        bad = ", ".join(c.name for c in pf.checks if c.status == "FAIL")
        res.status, res.message = ABORTED, f"pre-flight failed ({bad}); the board was not touched"
        return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)
    if approval is None:
        res.status, res.message = ABORTED, "not approved; the board was not touched"
        return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)
    log(f"approved by: {approval.by}")

    out = _flash_and_verify(board, image, pf.before, log)
    res.method = out.method
    res.after = out.state.to_dict() if out.state else {}
    if out.ok:
        new = store.register_device(out.state.uid or pf.before.uid, dep_id)
        res.status = SUCCESS
        res.message = f"deployed and verified: {out.state.summary()}" + ("; board registered" if new else "")
        return _finish(cfg, store, res, dep_id, log, t0, out.state.uid or pf.before.uid)

    res.failure = {"stage": out.stage, "message": out.message}
    if not out.changed:
        res.status = FAILED
        res.message = f"{out.message} The board was not changed."
        return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)
    if not rollback:
        res.status = FAILED
        res.message = (f"{out.stage} failed: {out.message}. Rollback was disabled, so the board may be in an "
                       f"unknown state. Run:  python -m app.deploy.cli recover")
        return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)

    log("rolling back to the last good firmware ...")
    rec = pf.recovery or recovery_image(cfg, avoid_sha=image.sha256)
    if rec is None:
        res.status = ROLLBACK_FAILED
        res.message = "the new firmware failed and there is no recovery image to restore"
        return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)
    log(f"restoring: {rec.label}")
    out2 = _flash_and_verify(board, rec, pf.before, log)
    res.rollback = {"image": rec.label, "ok": out2.ok, "message": out2.message}
    if out2.state:
        res.after = out2.state.to_dict()
    if out2.ok:
        res.status = ROLLED_BACK
        res.message = (f"the new firmware failed ({out.stage}: {out.message}); the board was restored to "
                       f"{rec.label} and verified")
    else:
        res.status = ROLLBACK_FAILED
        res.message = (f"the new firmware failed ({out.stage}: {out.message}) AND restoring {rec.label} failed "
                       f"({out2.stage}: {out2.message}). Manual recovery: hold BOOT while plugging in USB, then run: "
                       f"python -m app.deploy.cli recover")
    return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)


# ---------------------------------------------------------------- recover
def recover(cfg: DeployConfig, board, which: str, approval: Optional[Approval], *, dry_run: bool = False,
            trust_new_board: bool = False, store: Optional[DeploymentStore] = None,
            on_line: Optional[Callable[[str], None]] = None) -> DeployResult:
    """Bring the board back to a known state. which = 'auto' | 'known-good' | 'stock'."""
    store = store or DeploymentStore(cfg.db_path)
    log = DeployLog(on_line)
    t0 = time.monotonic()
    if which == "stock":
        image = resolve_stock(cfg)
    elif which == "known-good":
        image = resolve_known_good(cfg)
    else:
        image = recovery_image(cfg)
    if image is None:
        raise DeployError("no recovery image available (no known-good firmware and no factory backup at "
                          f"{cfg.stock_backup})")
    pf = run_preflight(cfg, board, image, store, kind="recover", trust_new_board=trust_new_board)
    res = DeployResult(kind="recover", started_at=now_iso(), image=image.to_dict(),
                       checks=[asdict(c) for c in pf.checks], before=pf.before.to_dict(),
                       approved_by=approval.by if approval else "")
    if dry_run:
        res.status, res.message = DRY_RUN, "pre-flight checks only; nothing was changed"
        res.finished_at = now_iso()
        return res

    dep_id = _start(store, "recover", image, approval, pf.before.uid)
    log(f"recovery #{dep_id}: {image.label}")
    if not pf.ok:
        bad = ", ".join(c.name for c in pf.checks if c.status == "FAIL")
        res.status, res.message = ABORTED, f"pre-flight failed ({bad}); the board was not touched"
        return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)
    if approval is None:
        res.status, res.message = ABORTED, "not approved; the board was not touched"
        return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)
    log(f"approved by: {approval.by}")

    out = _flash_and_verify(board, image, pf.before, log)
    res.method = out.method
    res.after = out.state.to_dict() if out.state else {}
    if out.ok:
        uid = out.state.uid or pf.before.uid
        store.register_device(uid, dep_id)
        res.status, res.message = SUCCESS, f"board restored: {out.state.summary()}"
        return _finish(cfg, store, res, dep_id, log, t0, uid)
    res.failure = {"stage": out.stage, "message": out.message}
    res.status = FAILED
    res.message = f"recovery failed at '{out.stage}': {out.message}"
    return _finish(cfg, store, res, dep_id, log, t0, pf.before.uid)


# ---------------------------------------------------------------- known-good
def promote_known_good(cfg: DeployConfig, build_id: int, store: Optional[DeploymentStore] = None) -> dict:
    """Make a build the known-good firmware. Only a build that has already run successfully on the
    board (a SUCCESS deployment exists) can be promoted."""
    store = store or DeploymentStore(cfg.db_path)
    img = resolve_build_image(cfg, build_id)
    if img.problems:
        raise DeployError("build image is not usable: " + "; ".join(img.problems))
    dep = store.successful_for_build(build_id)
    if not dep:
        raise DeployError(f"build #{build_id} has never been deployed and verified on the board; deploy it first "
                          f"(python -m app.deploy.cli deploy {build_id})")
    cfg.known_good_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(img.path, cfg.known_good_dir / "known_good.uf2")
    manifest = {
        "label": f"build #{build_id}, {img.project}",
        "build_id": build_id,
        "project": img.project,
        "src_hash": img.src_hash,
        "fw_version": img.fw_version,
        "uf2_sha256": img.sha256,
        "promoted_at": now_iso(),
        "deployment_id": dep["id"],
        "board_uid": dep.get("board_uid") or "",
    }
    (cfg.known_good_dir / "known_good.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


# ---------------------------------------------------------------- status helpers
def match_build_record(cfg: DeployConfig, state: BoardState) -> Optional[dict]:
    """If the board reports a build number, look it up in the build history."""
    if state.mode != RUNNING or not state.build_id.isdigit() or state.build_id == "0":
        return None
    row = BuildStore(cfg.db_path).get(int(state.build_id))
    if not row or not row.get("result"):
        return {"found": False, "build_id": int(state.build_id)}
    r = row["result"]
    return {
        "found": True,
        "build_id": r["id"],
        "project": r["project"],
        "status": r["status"],
        "started_at": r["started_at"],
        "sources_match": r["sources_sha256"][:8] == state.src_hash,
        "sources_sha256": r["sources_sha256"],
    }