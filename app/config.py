from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    root: Path
    model_definitions_root: Path
    model_registry: Path
    database_path: Path
    template_root: Path
    template_catalog_root: Path
    report_template_root: Path
    legacy_migrations: Path
    artifact_root: Path
    private_workbook_root: Path
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    worker_poll_seconds: float
    worker_id: str
    #: API 监听地址（仅 app/serve.py 使用）。默认只监听本机。
    api_host: str
    api_port: int
    #: 访问口令清单原文（`用户名:口令` 逗号分隔）。空 = 不启用认证。
    #: 解析与校验在 app/security.py，格式错误会直接报错，不做静默忽略。
    api_auth: str


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    path = Path(raw).expanduser() if raw else default
    return path.resolve()


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数，当前值：{raw!r}") from exc


def get_settings() -> Settings:
    root = _path_from_env("EXCEL_ENGINE_HOME", ROOT)
    default_template_root = root / "templates"
    if not default_template_root.is_dir():
        default_template_root = root
    return Settings(
        root=root,
        model_definitions_root=_path_from_env(
            "EXCEL_MODEL_DEFINITIONS_ROOT", root / "config" / "models"
        ),
        model_registry=_path_from_env(
            "EXCEL_MODEL_REGISTRY", root / "config" / "model_registry.json"
        ),
        database_path=_path_from_env(
            "EXCEL_DATABASE", root / ".runtime" / "jobs.sqlite3"
        ),
        template_root=_path_from_env("EXCEL_TEMPLATE_ROOT", default_template_root),
        # 模板适配层目录：每个 Excel 工作簿一个 <id>.json，只放 Sheet/Cell 映射。
        # 注意与上面的 template_root 区分：那是工作簿本体（*.xlsm）所在目录。
        template_catalog_root=_path_from_env(
            "EXCEL_TEMPLATE_CATALOG", root / "config" / "templates"
        ),
        report_template_root=_path_from_env(
            "EXCEL_REPORT_TEMPLATE_ROOT", default_template_root / "reports"
        ),
        legacy_migrations=_path_from_env(
            "EXCEL_LEGACY_MIGRATIONS", root / "config" / "legacy_migrations.json"
        ),
        artifact_root=_path_from_env(
            "EXCEL_ARTIFACT_ROOT", root / ".runtime" / "artifacts"
        ),
        private_workbook_root=_path_from_env(
            "EXCEL_PRIVATE_WORKBOOK_ROOT", root / ".runtime" / "private-workbooks"
        ),
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        deepseek_base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        worker_poll_seconds=max(0.2, float(os.environ.get("EXCEL_WORKER_POLL_SECONDS", "1.0"))),
        worker_id=os.environ.get("EXCEL_WORKER_ID", f"win-{os.environ.get('COMPUTERNAME', 'local')}")[:100],
        api_host=os.environ.get("EXCEL_API_HOST", "127.0.0.1").strip() or "127.0.0.1",
        api_port=_int_from_env("EXCEL_API_PORT", 8000),
        api_auth=os.environ.get("EXCEL_API_AUTH", ""),
    )
