"""Excel 启动失败必须「快速失败 + 说人话」。

背景：`xw.App()` 在 COM 激活失败时要等 30~130 秒才抛错，而错误只是一个裸 HRESULT。
用户在前端看到的是长时间转圈，运维拿到的是「服务器运行失败」——两者都无法排查。
本文件守护三件事：

1. 已知 HRESULT 被翻译成带排查指引的专门错误码；
2. 自检结论会被缓存，后续任务**秒级失败**，且 Excel 修好后能自动恢复；
3. Worker 在 Excel 不可用时**不会**再去执行工作流。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.excel_executor as excel_executor
from app.domain import load_model_definition
from app.excel_executor import (
    ExcelExecutionError,
    classify_com_error,
    verify_excel_startable,
)
from app.storage import Storage
from app.worker import ExcelReadiness

ROOT = Path(__file__).resolve().parents[1]
CO_E_SERVER_EXEC_FAILURE = -2146959355


class FakeComError(Exception):
    """形状与 pywintypes.com_error 一致：args[0] 是 HRESULT。"""

    def __init__(self, hresult: int):
        super().__init__(hresult, "服务器运行失败", None, None)
        self.hresult = hresult


class AlwaysFailingChecker:
    def __init__(self, code: str = "EXCEL_COM_ACTIVATION_FAILED", message: str = "Excel 起不来（测试）"):
        self.code = code
        self.message = message

    def __call__(self) -> str:
        raise ExcelExecutionError(self.code, self.message)


@pytest.fixture
def no_session_check(monkeypatch):
    monkeypatch.setattr(excel_executor, "require_interactive_windows_session", lambda: 3)


def failing_xlwings(hresult: int):
    def _app(**kwargs):
        raise FakeComError(hresult)

    return SimpleNamespace(App=_app)


@pytest.mark.parametrize(
    "hresult,code",
    [
        (CO_E_SERVER_EXEC_FAILURE, "EXCEL_COM_ACTIVATION_FAILED"),
        (-2147024891, "EXCEL_COM_ACCESS_DENIED"),
        (-2147417851, "EXCEL_COM_SERVER_FAULT"),
        (-2147023174, "EXCEL_RPC_UNAVAILABLE"),
    ],
)
def test_known_com_errors_map_to_actionable_codes(hresult, code):
    classified = classify_com_error(FakeComError(hresult))
    assert classified is not None
    assert classified[0] == code
    # 必须带有实际可执行的排查指引，而不是又一句「服务器运行失败」
    assert len(classified[1]) > 40


def test_unknown_errors_are_not_misclassified():
    assert classify_com_error(FakeComError(-1)) is None
    assert classify_com_error(ValueError("boom")) is None
    assert classify_com_error(Exception()) is None


def test_activation_failure_is_reported_with_guidance(monkeypatch, no_session_check):
    monkeypatch.setitem(sys.modules, "xlwings", failing_xlwings(CO_E_SERVER_EXEC_FAILURE))

    with pytest.raises(ExcelExecutionError) as excinfo:
        verify_excel_startable()

    assert excinfo.value.code == "EXCEL_COM_ACTIVATION_FAILED"
    # 指引里必须给出运维真正能查的东西
    assert "10010" in str(excinfo.value)
    assert "Office" in str(excinfo.value)


def test_unknown_com_failure_falls_back_to_generic_code(monkeypatch, no_session_check):
    monkeypatch.setitem(sys.modules, "xlwings", failing_xlwings(-12345))

    with pytest.raises(ExcelExecutionError) as excinfo:
        verify_excel_startable()

    assert excinfo.value.code == "EXCEL_START_FAILED"


def test_successful_check_reports_version_and_quits_excel(monkeypatch, no_session_check):
    quit_calls = []

    class FakeApp:
        version = "15.0"

        def quit(self):
            quit_calls.append(True)

    monkeypatch.setitem(sys.modules, "xlwings", SimpleNamespace(App=lambda **kwargs: FakeApp()))

    assert verify_excel_startable() == "15.0"
    assert quit_calls == [True]


def test_readiness_caches_failure_so_later_jobs_fail_fast():
    calls = []

    def checker():
        calls.append(1)
        raise ExcelExecutionError("EXCEL_COM_ACTIVATION_FAILED", "Excel 起不来")

    readiness = ExcelReadiness(ttl_seconds=60, checker=checker)

    with pytest.raises(ExcelExecutionError) as first:
        readiness.ensure()
    assert first.value.code == "EXCEL_COM_ACTIVATION_FAILED"

    with pytest.raises(ExcelExecutionError) as second:
        readiness.ensure()
    assert second.value.code == "EXCEL_COM_ACTIVATION_FAILED"
    # 关键：第二次不再真的去撞 COM
    assert len(calls) == 1


def test_readiness_recovers_without_restarting_the_worker():
    state = {"fail": True}
    calls = []

    def checker():
        calls.append(1)
        if state["fail"]:
            raise ExcelExecutionError("EXCEL_COM_ACTIVATION_FAILED", "Excel 起不来")
        return "15.0"

    readiness = ExcelReadiness(ttl_seconds=0, checker=checker)

    with pytest.raises(ExcelExecutionError):
        readiness.ensure()

    state["fail"] = False
    assert readiness.ensure() == "15.0"
    assert len(calls) == 2


def test_readiness_invalidate_forces_a_recheck():
    calls = []

    def checker():
        calls.append(1)
        return "15.0"

    readiness = ExcelReadiness(ttl_seconds=3600, checker=checker)
    assert readiness.ensure() == "15.0"
    assert readiness.ensure() == "15.0"
    assert len(calls) == 1

    readiness.invalidate()
    assert readiness.ensure() == "15.0"
    assert len(calls) == 2


def test_run_once_fails_job_immediately_when_excel_is_unavailable(tmp_path, monkeypatch):
    """Excel 不可用时任务必须**立刻**失败，而不是等 COM 超时。"""
    import app.worker as worker

    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")
    session = storage.create_session("alice", model.model.id)
    job = storage.enqueue_job(
        user_id="alice",
        session_id=session["session_id"],
        model_id=model.model.id,
        model_snapshot=model.model_dump(mode="json"),
        parameters={"heat_load": 240},
    )

    executor_calls = []

    def forbidden_executor(*args, **kwargs):
        executor_calls.append(1)
        raise AssertionError("Excel 不可用时不应该再去执行工作流")

    monkeypatch.setattr(worker, "execute_model", forbidden_executor)

    settings = SimpleNamespace(
        artifact_root=tmp_path / "artifacts",
        private_workbook_root=tmp_path / "private",
        template_root=tmp_path / "templates",
    )
    readiness = ExcelReadiness(checker=AlwaysFailingChecker())

    assert worker.run_once(storage, settings, "w1", readiness=readiness) is True

    failed = storage.get_job(job["job_id"], "alice")
    assert failed["status"] == "FAILED"
    assert failed["error"]["code"] == "EXCEL_COM_ACTIVATION_FAILED"
    assert "测试" in failed["error"]["message"]
    assert executor_calls == []


def test_run_once_without_readiness_still_runs_the_workflow(tmp_path, monkeypatch):
    """不传 readiness 时保持原行为（既有调用方与测试不受影响）。"""
    import app.worker as worker

    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")
    session = storage.create_session("alice", model.model.id)
    job = storage.enqueue_job(
        user_id="alice",
        session_id=session["session_id"],
        model_id=model.model.id,
        model_snapshot=model.model_dump(mode="json"),
        parameters={"heat_load": 240},
    )

    calls = []

    def fake_execute_model(model, parameters, **kwargs):
        calls.append(parameters)
        return {"thermal_result": 1.0}, []

    def fake_generate_report(path, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock docx")
        return path

    monkeypatch.setattr(worker, "execute_model", fake_execute_model)
    monkeypatch.setattr(worker, "generate_report", fake_generate_report)
    monkeypatch.setattr(worker, "export_word_preview", lambda path, preview: path)
    monkeypatch.setattr(
        worker, "DeepSeekClient", lambda settings: SimpleNamespace(summarize_results=lambda *a: "")
    )

    settings = SimpleNamespace(
        artifact_root=tmp_path / "artifacts",
        private_workbook_root=tmp_path / "private",
        template_root=tmp_path / "templates",
    )

    assert worker.run_once(storage, settings, "w1") is True
    assert calls == [{"heat_load": 240.0}]
    assert storage.get_job(job["job_id"], "alice")["status"] == "SUCCEEDED"
