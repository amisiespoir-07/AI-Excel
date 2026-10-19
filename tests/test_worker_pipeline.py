"""Worker 后处理流程必须由模型配置驱动，而不是写死在代码里。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain import ModelDefinition, load_model_definition
from app.storage import Storage

ROOT = Path(__file__).resolve().parents[1]


def model_with_report(**report_overrides) -> ModelDefinition:
    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    payload = model.model_dump(mode="json")
    payload["report"].update(report_overrides)
    return ModelDefinition.model_validate(payload)


def enqueue(storage: Storage, model: ModelDefinition, parameters: dict):
    session = storage.create_session("alice", model.model.id)
    return storage.enqueue_job(
        user_id="alice",
        session_id=session["session_id"],
        model_id=model.model.id,
        model_snapshot=model.model_dump(mode="json"),
        parameters=parameters,
    )


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    """把 Worker 外部依赖全部替换掉，只保留被测试的流程编排。"""
    import app.worker as worker

    llm_calls: list[str] = []

    class RecordingLLM:
        def __init__(self, settings):
            llm_calls.append("constructed")

        def summarize_results(self, model, parameters, results):
            llm_calls.append("summarize")
            return "AI 总结文本"

    def fake_execute_model(model, parameters, **kwargs):
        return {"thermal_result": 1.0}, []

    def fake_export_word_preview(docx_path, pdf_path):
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-1.4 fake")
        return pdf_path

    monkeypatch.setattr(worker, "execute_model", fake_execute_model)
    monkeypatch.setattr(worker, "DeepSeekClient", RecordingLLM)
    monkeypatch.setattr(worker, "export_word_preview", fake_export_word_preview)

    artifact_root = tmp_path / "artifacts"
    settings = SimpleNamespace(
        artifact_root=artifact_root,
        private_workbook_root=tmp_path / "private",
        template_root=tmp_path / "templates",
    )
    storage = Storage(tmp_path / "jobs.sqlite3")
    return SimpleNamespace(
        worker=worker,
        storage=storage,
        settings=settings,
        artifact_root=artifact_root,
        llm_calls=llm_calls,
    )


def test_default_report_produces_word_artifact_with_preview(worker_env):
    model = model_with_report()
    job = enqueue(worker_env.storage, model, {"heat_load": 240})

    assert worker_env.worker.run_once(worker_env.storage, worker_env.settings, "w1") is True
    finished = worker_env.storage.get_job(job["job_id"], "alice")

    assert finished["status"] == "SUCCEEDED"
    assert [item["kind"] for item in finished["artifacts"]] == ["word"]
    assert finished["artifacts"][0]["file_name"] == "calculation-report.docx"
    assert finished["artifacts"][0]["preview_path"]
    assert worker_env.llm_calls == ["constructed", "summarize"]


def test_report_disabled_skips_report_and_never_calls_llm(worker_env):
    model = model_with_report(enabled=False)
    job = enqueue(worker_env.storage, model, {"heat_load": 240})

    assert worker_env.worker.run_once(worker_env.storage, worker_env.settings, "w1") is True
    finished = worker_env.storage.get_job(job["job_id"], "alice")

    assert finished["status"] == "SUCCEEDED"
    assert finished["artifacts"] == []
    assert worker_env.llm_calls == []
    assert not list(worker_env.artifact_root.rglob("*.docx"))


def test_ai_summary_can_be_disabled_while_report_still_generates(worker_env):
    model = model_with_report(ai_summary=False)
    job = enqueue(worker_env.storage, model, {"heat_load": 240})

    worker_env.worker.run_once(worker_env.storage, worker_env.settings, "w1")
    finished = worker_env.storage.get_job(job["job_id"], "alice")

    assert finished["status"] == "SUCCEEDED"
    assert worker_env.llm_calls == []  # 完全不构造 LLM 客户端
    reports = list(worker_env.artifact_root.rglob("*.docx"))
    assert len(reports) == 1
    assert "结果说明暂不可用" in _docx_text(reports[0])


def test_pdf_preview_can_be_disabled(worker_env):
    model = model_with_report(pdf_preview=False)
    job = enqueue(worker_env.storage, model, {"heat_load": 240})

    worker_env.worker.run_once(worker_env.storage, worker_env.settings, "w1")
    finished = worker_env.storage.get_job(job["job_id"], "alice")

    assert finished["status"] == "SUCCEEDED"
    assert len(finished["artifacts"]) == 1
    assert not finished["artifacts"][0].get("preview_path")


def test_report_file_name_is_configurable(worker_env):
    model = model_with_report(file_name="锅炉效率报告.docx")
    job = enqueue(worker_env.storage, model, {"heat_load": 240})

    worker_env.worker.run_once(worker_env.storage, worker_env.settings, "w1")
    finished = worker_env.storage.get_job(job["job_id"], "alice")

    assert finished["artifacts"][0]["file_name"] == "锅炉效率报告.docx"


def _docx_text(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)
