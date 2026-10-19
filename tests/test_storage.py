from pathlib import Path

import pytest

from app.storage import Storage


def test_local_queue_claims_completes_and_enforces_owner(tmp_path: Path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    session = storage.create_session("alice", "sample")
    job = storage.enqueue_job(
        user_id="alice",
        session_id=session["session_id"],
        model_id="sample",
        model_snapshot={"model_id": "sample", "version": 1},
        parameters={"amount": 10},
    )
    assert job["status"] == "QUEUED"

    claimed = storage.claim_job("worker-test")
    assert claimed is not None
    assert claimed["job_id"] == job["job_id"]
    assert claimed["status"] == "RUNNING"

    artifact_path = tmp_path / "report.docx"
    artifact_path.write_bytes(b"test artifact")
    accepted = storage.finish_job(
        job["job_id"],
        "worker-test",
        {"result": 25},
        [{"kind": "word", "file_name": artifact_path.name,
          "file_path": str(artifact_path), "size_bytes": artifact_path.stat().st_size}],
    )
    assert accepted is True

    result = storage.get_job(job["job_id"], "alice")
    assert result["status"] == "SUCCEEDED"
    assert result["results"] == {"result": 25}
    assert len(result["artifacts"]) == 1
    assert storage.get_job(job["job_id"], "bob") is None
    assert storage.get_artifact(result["artifacts"][0]["artifact_id"], "bob") is None


def test_preferences_are_scoped_by_user_and_model(tmp_path: Path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    storage.set_preferences("alice", {"rate": 0.05})
    storage.set_preferences("alice", {"rate": 0.04}, model_id="model-a")
    storage.set_preferences("bob", {"rate": 0.03})
    assert storage.get_preferences("alice", "model-a") == (
        {"rate": 0.05}, {"rate": 0.04}
    )
    assert storage.get_preferences("alice", "model-b") == ({"rate": 0.05}, {})
    assert storage.get_preferences("bob", "model-a") == ({"rate": 0.03}, {})


def test_excel_workbooks_cannot_be_registered_as_downloadable_artifacts(tmp_path: Path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    session = storage.create_session("alice", "sample")
    job = storage.enqueue_job(
        user_id="alice", session_id=session["session_id"], model_id="sample",
        model_snapshot={"model_id": "sample"}, parameters={},
    )
    storage.claim_job("worker-test")
    workbook = tmp_path / "private.xlsm"
    workbook.write_bytes(b"confidential workbook")

    with pytest.raises(ValueError, match="Only PDF and Word"):
        storage.finish_job(
            job["job_id"], "worker-test", {},
            [{"kind": "excel", "file_name": workbook.name,
              "file_path": str(workbook), "size_bytes": workbook.stat().st_size}],
        )

    result = storage.get_job(job["job_id"], "alice")
    assert result["status"] == "RUNNING"
    assert result["artifacts"] == []


def test_worker_stage_survives_heartbeat_and_failure(tmp_path: Path):
    storage = Storage(tmp_path / "jobs.sqlite3")
    session = storage.create_session("alice", "sample")
    job = storage.enqueue_job(
        user_id="alice", session_id=session["session_id"], model_id="sample",
        model_snapshot={"model_id": "sample"}, parameters={},
    )
    storage.claim_job("worker-test")
    assert storage.heartbeat(job["job_id"], "worker-test", "excel_calculation_2")
    assert storage.heartbeat(job["job_id"], "worker-test", None)
    assert storage.fail_job(job["job_id"], "worker-test", "EXCEL_FAILED", "failed")

    result = storage.get_job(job["job_id"], "alice")
    assert result["status"] == "FAILED"
    assert result["stage"] == "excel_calculation_2"
