from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import get_settings
from .domain import (
    ModelDefinition,
    migrate_model_snapshot,
    normalize_legacy_parameter_values,
    validate_parameters,
)
from .excel_executor import (
    ExcelExecutionError,
    require_interactive_windows_session,
    verify_excel_startable,
)
from .executors import ExecutionError
from .llm import DeepSeekClient
from .reporting import export_word_preview, generate_report
from .storage import Storage
from .workflow import execute_model


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("excel-worker")


def _heartbeat_loop(
    storage: Storage, job_id: str, worker_id: str, stop_event: threading.Event
) -> None:
    while not stop_event.wait(60):
        if not storage.heartbeat(job_id, worker_id, None, lease_seconds=900):
            logger.warning("job=%s lost its worker lease", job_id)
            return


class ExcelReadiness:
    """缓存「Excel 能否被自动化启动」的结论。

    每个任务都硬撞一次 COM 超时要等 30~130 秒，用户会以为界面卡死。
    这里把自检结论缓存一小段时间：一旦判定不可用，后续任务**秒级失败**并带上原因；
    同时自检会定期重跑，所以 Excel 修好之后不需要重启 Worker。
    """

    def __init__(self, *, ttl_seconds: float = 60.0, checker=verify_excel_startable):
        self.ttl_seconds = ttl_seconds
        self.checker = checker
        self._checked_at: float | None = None
        self._version = ""
        self._failure: tuple[str, str] | None = None

    def ensure(self) -> str:
        """确认 Excel 可用；不可用时抛出缓存下来的 :class:`ExecutionError`。"""
        now = time.monotonic()
        if self._checked_at is not None and now - self._checked_at < self.ttl_seconds:
            if self._failure is not None:
                code, message = self._failure
                raise ExcelExecutionError(code, message)
            return self._version

        self._checked_at = now
        try:
            self._version = self.checker()
            self._failure = None
        except ExecutionError as exc:
            self._failure = (exc.code, str(exc))
            raise
        return self._version

    def invalidate(self) -> None:
        """丢弃缓存，强制下一次调用重新自检。"""
        self._checked_at = None


def run_once(
    storage: Storage,
    settings,
    worker_id: str,
    *,
    readiness: "ExcelReadiness | None" = None,
) -> bool:
    job = storage.claim_job(worker_id, lease_seconds=900)
    if job is None:
        return False

    job_id = job["job_id"]
    logger.info("claimed job=%s attempt=%s", job_id, job["attempts"])
    stop_event = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(storage, job_id, worker_id, stop_event),
        name=f"heartbeat-{job_id[:8]}",
        daemon=True,
    )
    heartbeat.start()
    try:
        if readiness is not None:
            # Excel 起不来就立刻失败：不要让用户等到 COM 超时结束。
            readiness.ensure()
        model_snapshot = migrate_model_snapshot(job["model_snapshot"])
        model = ModelDefinition.model_validate(model_snapshot)
        if model.model.id != job["model_id"]:
            raise ValueError(
                f"任务 model_id {job['model_id']!r} 与 Calculation Model Snapshot "
                f"ID {model.model.id!r} 不一致"
            )
        parameters = normalize_legacy_parameter_values(job["model_snapshot"], job["parameters"])
        parameters, missing = validate_parameters(model, parameters, require_all=True)
        if missing:
            raise ValueError(f"任务快照缺少必填业务参数：{', '.join(missing)}")
        job_directory = (settings.artifact_root / job_id).resolve()
        job_directory.relative_to(settings.artifact_root.resolve())
        private_workbook_directory = (settings.private_workbook_root / job_id).resolve()
        private_workbook_directory.relative_to(settings.private_workbook_root.resolve())

        def report_stage(stage: str) -> None:
            if not storage.heartbeat(job_id, worker_id, stage, lease_seconds=900):
                raise RuntimeError("任务租约已失效，不能更新计算进度")

        results, artifacts = execute_model(
            model=model,
            parameters=parameters,
            template_root=settings.template_root,
            artifact_directory=job_directory,
            private_workbook_directory=private_workbook_directory,
            on_stage=report_stage,
        )
        # 后处理阶段由模型定义的 report 段决定，不在 Worker 里写死。
        # 顺序是固定的（说明 -> 报告 -> 预览），但「跑不跑」由配置说了算。
        report = model.report
        if report.enabled:
            report_stage("report_generation")
            summary = (
                DeepSeekClient(settings).summarize_results(model, parameters, results)
                if report.ai_summary
                else ""
            )
            report_path = generate_report(
                job_directory / report.file_name,
                model,
                parameters,
                results,
                summary,
                job_id,
            )
            report_artifact: dict[str, Any] = {
                "kind": "word",
                "file_name": report_path.name,
                "file_path": str(report_path.resolve()),
                "size_bytes": report_path.stat().st_size,
            }
            if report.pdf_preview:
                report_preview_path = export_word_preview(
                    report_path, job_directory / f"{report_path.stem}-preview.pdf"
                )
                report_artifact["preview_path"] = str(report_preview_path.resolve())
            artifacts.append(report_artifact)
        if not storage.finish_job(job_id, worker_id, results, artifacts):
            raise RuntimeError("任务租约已失效，Worker 结果未被接受")
        logger.info("completed job=%s", job_id)
    except ExecutionError as exc:
        logger.exception("job failed job=%s code=%s", job_id, exc.code)
        storage.fail_job(job_id, worker_id, exc.code, str(exc))
    except Exception as exc:
        logger.exception("job failed job=%s", job_id)
        storage.fail_job(job_id, worker_id, "WORKER_JOB_FAILED", str(exc))
    finally:
        stop_event.set()
        heartbeat.join(timeout=2)
    return True


def main() -> None:
    try:
        session_id = require_interactive_windows_session()
    except ExcelExecutionError as exc:
        raise SystemExit(f"Worker 启动失败 [{exc.code}]：{exc}") from exc
    settings = get_settings()
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    storage = Storage(settings.database_path)
    worker_id = settings.worker_id or f"win-{uuid.uuid4().hex[:8]}"
    logger.info("starting Windows Excel Worker id=%s", worker_id)
    logger.info("running in interactive Windows session=%s", session_id)

    # 启动自检。失败**不退出**：Worker 照常领任务，但每个任务会立刻失败并把原因写给用户，
    # 这样前端不会干等 COM 超时。自检每隔 ttl_seconds 重跑一次，Excel 修好后自动恢复。
    readiness = ExcelReadiness()
    try:
        logger.info("Excel 自动化自检通过：version=%s", readiness.ensure())
    except ExecutionError as exc:
        logger.error(
            "Excel 自动化自检失败 [%s]：%s\n"
            ">>> Worker 仍会启动，但在问题修好之前，每个计算任务都会立即失败并向用户显示上面的原因。\n"
            ">>> 自检每 %.0f 秒重跑一次，修复 Excel 后无需重启 Worker。",
            exc.code,
            exc,
            readiness.ttl_seconds,
        )

    try:
        while True:
            try:
                if not run_once(storage, settings, worker_id, readiness=readiness):
                    time.sleep(settings.worker_poll_seconds)
            except KeyboardInterrupt:
                raise
            except Exception:
                logger.exception("worker loop error")
                time.sleep(max(1.0, settings.worker_poll_seconds))
    except KeyboardInterrupt:
        logger.info("worker stopped")


if __name__ == "__main__":
    main()
