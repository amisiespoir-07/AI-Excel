from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.api as api_module
from app.storage import Storage


def test_job_hides_legacy_excel_artifacts_and_allows_pdf_preview_download(tmp_path: Path, monkeypatch):
    artifact_root = tmp_path / "artifacts"
    job_dir = artifact_root / "job-1"
    job_dir.mkdir(parents=True)
    workbook_path = job_dir / "secret.xlsm"
    workbook_path.write_bytes(b"confidential workbook")
    pdf_path = job_dir / "results.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test PDF")
    word_path = job_dir / "report.docx"
    word_path.write_bytes(b"fake docx")
    word_preview_path = job_dir / "report-preview.pdf"
    word_preview_path.write_bytes(b"%PDF-1.4 word preview")

    storage = Storage(tmp_path / "jobs.sqlite3")
    session = storage.create_session("local-user", "sample")
    job = storage.enqueue_job(
        user_id="local-user",
        session_id=session["session_id"],
        model_id="sample",
        model_snapshot={"model_id": "sample"},
        parameters={},
    )
    storage.claim_job("worker-test")
    storage.finish_job(
        job["job_id"], "worker-test", {"result": 1},
        [{"kind": "pdf", "file_name": pdf_path.name,
                    "file_path": str(pdf_path), "size_bytes": pdf_path.stat().st_size},
                 {"kind": "word", "file_name": word_path.name,
                    "file_path": str(word_path), "preview_path": str(word_preview_path),
                    "size_bytes": word_path.stat().st_size}],
    )

    # Simulate a row created by the earlier application version, which exposed XLSM artifacts.
    with storage._connection() as db:
        db.execute(
            "INSERT INTO artifacts(artifact_id,job_id,user_id,kind,file_name,file_path,size_bytes,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("legacy-excel-id", job["job_id"], "local-user", "excel", workbook_path.name,
             str(workbook_path.resolve()), workbook_path.stat().st_size, "2026-10-05T00:00:00+00:00"),
        )

    monkeypatch.setattr(api_module, "storage", storage)
    monkeypatch.setattr(api_module, "settings", SimpleNamespace(artifact_root=artifact_root))
    client = TestClient(api_module.app)

    response = client.get(f"/api/v1/jobs/{job['job_id']}")
    assert response.status_code == 200
    public_artifacts = response.json()["artifacts"]
    assert len(public_artifacts) == 2
    pdf_artifact = next(item for item in public_artifacts if item["kind"] == "pdf")
    word_artifact = next(item for item in public_artifacts if item["kind"] == "word")
    assert "preview_path" in pdf_artifact
    assert "preview_path" in word_artifact
    assert word_artifact["preview_name"] == "report-preview.pdf"
    assert "secret.xlsm" not in response.text

    blocked = client.get(f"/api/v1/jobs/{job['job_id']}/artifacts/legacy-excel-id")
    assert blocked.status_code == 404
    assert "workbook" not in blocked.content.decode("latin-1", errors="ignore")

    download = client.get(pdf_artifact["download_path"])
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/pdf")
    assert "attachment" in download.headers["content-disposition"]
    assert download.content.startswith(b"%PDF-")

    preview = client.get(pdf_artifact["preview_path"])
    assert preview.status_code == 200
    assert "inline" in preview.headers["content-disposition"]
    assert preview.headers["cache-control"] == "private, no-store"

    word_preview = client.get(word_artifact["preview_path"])
    assert word_preview.status_code == 200
    assert word_preview.headers["content-type"].startswith("application/pdf")
    assert "report-preview.pdf" in word_preview.headers["content-disposition"]

    word_download = client.get(word_artifact["download_path"])
    assert word_download.status_code == 200
    assert "attachment" in word_download.headers["content-disposition"]
    assert word_download.content == word_path.read_bytes()
