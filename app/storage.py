from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else default


class Storage:
    """本机 SQLite 会话存储和 Windows Worker 任务队列。"""

    def __init__(self, database_path: Path):
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    pending_confirmation INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_owner
                    ON sessions(user_id, session_id);

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    model_snapshot_json TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    lease_until TEXT,
                    result_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_queue
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_owner
                    ON jobs(user_id, job_id);

                CREATE TABLE IF NOT EXISTS preferences (
                    user_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    model_id TEXT NOT NULL DEFAULT '',
                    values_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, scope, model_id)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    preview_path TEXT,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_job
                    ON artifacts(job_id, user_id);
                """
            )
            artifact_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(artifacts)").fetchall()
            }
            if "preview_path" not in artifact_columns:
                db.execute("ALTER TABLE artifacts ADD COLUMN preview_path TEXT")

    def create_session(self, user_id: str, model_id: str | None) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        now = utc_now()
        with self._connection() as db:
            db.execute(
                "INSERT INTO sessions(session_id,user_id,model_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (session_id, user_id, model_id or "", now, now),
            )
        return self.get_session(session_id, user_id)  # type: ignore[return-value]

    def get_session(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE session_id=? AND user_id=?", (session_id, user_id)
            ).fetchone()
        if row is None:
            return None
        session = {
            **dict(row),
            "parameters": decode(row["parameters_json"], {}),
            "pending_confirmation": bool(row["pending_confirmation"]),
        }
        if not session["model_id"]:
            session["model_id"] = None
        return session

    def update_session(
        self, session_id: str, user_id: str, parameters: dict[str, Any], pending: bool
    ) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE sessions SET parameters_json=?,pending_confirmation=?,updated_at=? "
                "WHERE session_id=? AND user_id=?",
                (encode(parameters), int(pending), utc_now(), session_id, user_id),
            )
            return cursor.rowcount == 1

    def bind_session_model(
        self,
        session_id: str,
        user_id: str,
        model_id: str,
        *,
        clear_parameters: bool,
    ) -> bool:
        """Bind a selected model and optionally clear values from the prior model."""
        with self._connection() as db:
            if clear_parameters:
                cursor = db.execute(
                    "UPDATE sessions SET model_id=?,parameters_json='{}',pending_confirmation=0,updated_at=? "
                    "WHERE session_id=? AND user_id=?",
                    (model_id, utc_now(), session_id, user_id),
                )
            else:
                cursor = db.execute(
                    "UPDATE sessions SET model_id=?,pending_confirmation=0,updated_at=? "
                    "WHERE session_id=? AND user_id=?",
                    (model_id, utc_now(), session_id, user_id),
                )
            return cursor.rowcount == 1

    def get_preferences(self, user_id: str, model_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._connection() as db:
            global_row = db.execute(
                "SELECT values_json FROM preferences WHERE user_id=? AND scope='global' AND model_id=''",
                (user_id,),
            ).fetchone()
            model_row = db.execute(
                "SELECT values_json FROM preferences WHERE user_id=? AND scope='model' AND model_id=?",
                (user_id, model_id),
            ).fetchone()
        return (
            decode(global_row["values_json"], {}) if global_row else {},
            decode(model_row["values_json"], {}) if model_row else {},
        )

    def set_preferences(
        self, user_id: str, values: dict[str, Any], model_id: str | None = None
    ) -> None:
        scope = "model" if model_id else "global"
        model_key = model_id or ""
        with self._connection() as db:
            db.execute(
                "INSERT INTO preferences(user_id,scope,model_id,values_json,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id,scope,model_id) DO UPDATE SET values_json=excluded.values_json, "
                "updated_at=excluded.updated_at",
                (user_id, scope, model_key, encode(values), utc_now()),
            )

    def enqueue_job(
        self,
        user_id: str,
        session_id: str,
        model_id: str,
        model_snapshot: dict[str, Any],
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = utc_now()
        with self._connection() as db:
            db.execute(
                "INSERT INTO jobs(job_id,user_id,session_id,model_id,model_snapshot_json,parameters_json,"
                "status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (job_id, user_id, session_id, model_id, encode(model_snapshot), encode(parameters),
                 "QUEUED", "queued", now, now),
            )
            db.execute(
                "UPDATE sessions SET pending_confirmation=0,updated_at=? WHERE session_id=? AND user_id=?",
                (now, session_id, user_id),
            )
        return self.get_job(job_id, user_id)  # type: ignore[return-value]

    def claim_job(self, worker_id: str, lease_seconds: int = 300, max_attempts: int = 3) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        lease_text = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE jobs SET status='FAILED',stage='worker_lost',error_json=?,updated_at=? "
                "WHERE status='RUNNING' AND lease_until < ? AND attempts >= ?",
                (encode({"code": "WORKER_LEASE_EXPIRED", "message": "Worker 租约过期且已达最大重试次数"}),
                 now_text, now_text, max_attempts),
            )
            db.execute(
                "UPDATE jobs SET status='QUEUED',stage='queued',worker_id=NULL,lease_until=NULL,updated_at=? "
                "WHERE status='RUNNING' AND lease_until < ? AND attempts < ?",
                (now_text, now_text, max_attempts),
            )
            row = db.execute(
                "SELECT job_id FROM jobs WHERE status='QUEUED' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                db.commit()
                return None
            job_id = row["job_id"]
            db.execute(
                "UPDATE jobs SET status='RUNNING',stage='starting',worker_id=?,lease_until=?,"
                "attempts=attempts+1,updated_at=? WHERE job_id=? AND status='QUEUED'",
                (worker_id, lease_text, now_text, job_id),
            )
            db.commit()
        return self.get_job(job_id)

    def heartbeat(self, job_id: str, worker_id: str, stage: str | None, lease_seconds: int = 300) -> bool:
        lease = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE jobs SET stage=COALESCE(?,stage),lease_until=?,updated_at=? "
                "WHERE job_id=? AND worker_id=? AND status='RUNNING'",
                (stage, lease, utc_now(), job_id, worker_id),
            )
            return cursor.rowcount == 1

    def finish_job(
        self,
        job_id: str,
        worker_id: str,
        results: dict[str, Any],
        artifacts: list[dict[str, Any]],
    ) -> bool:
        now = utc_now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute(
                "SELECT user_id FROM jobs WHERE job_id=? AND worker_id=? AND status='RUNNING'",
                (job_id, worker_id),
            ).fetchone()
            if owner is None:
                db.rollback()
                return False
            db.execute(
                "UPDATE jobs SET status='SUCCEEDED',stage='complete',result_json=?,lease_until=NULL,updated_at=? "
                "WHERE job_id=? AND worker_id=?",
                (encode(results), now, job_id, worker_id),
            )
            for artifact in artifacts:
                if artifact.get("kind") not in {"pdf", "word"}:
                    db.rollback()
                    raise ValueError("Only PDF and Word artifacts may be published for download")
                db.execute(
                    "INSERT INTO artifacts(artifact_id,job_id,user_id,kind,file_name,file_path,preview_path,size_bytes,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), job_id, owner["user_id"], artifact["kind"], artifact["file_name"],
                     str(Path(artifact["file_path"]).resolve()),
                     str(Path(artifact["preview_path"]).resolve()) if artifact.get("preview_path") else None,
                     int(artifact["size_bytes"]), now),
                )
            db.commit()
            return True

    def fail_job(self, job_id: str, worker_id: str, code: str, message: str) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE jobs SET status='FAILED',error_json=?,lease_until=NULL,updated_at=? "
                "WHERE job_id=? AND worker_id=? AND status='RUNNING'",
                (encode({"code": code, "message": message[:1000]}), utc_now(), job_id, worker_id),
            )
            return cursor.rowcount == 1

    def get_job(self, job_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        with self._connection() as db:
            if user_id is None:
                row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM jobs WHERE job_id=? AND user_id=?", (job_id, user_id)
                ).fetchone()
            if row is None:
                return None
            artifacts = db.execute(
                "SELECT artifact_id,kind,file_name,preview_path,size_bytes,created_at FROM artifacts WHERE job_id=? AND user_id=?",
                (job_id, row["user_id"]),
            ).fetchall()
        return {
            **dict(row),
            "model_snapshot": decode(row["model_snapshot_json"], {}),
            "parameters": decode(row["parameters_json"], {}),
            "results": decode(row["result_json"], None),
            "error": decode(row["error_json"], None),
            "artifacts": [dict(item) for item in artifacts],
        }

    def get_artifact(self, artifact_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM artifacts WHERE artifact_id=? AND user_id=?", (artifact_id, user_id)
            ).fetchone()
        return dict(row) if row else None
