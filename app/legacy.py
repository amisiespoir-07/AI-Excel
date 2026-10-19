"""遗留快照迁移映射表。

这些映射描述的是**历史数据**——旧版本写进 SQLite 队列的任务快照、以及旧的用户偏好记录——
而不是业务规则。所以它们从程序代码里挪进了 ``config/legacy_migrations.json``：
调整或新增一条历史 ID 翻译不需要改 Python，也不需要重新发版。

配置文件缺失时按「没有配置任何迁移」处理（映射为空）；文件存在但无法解析时**显式报错**，
不做静默兜底——否则历史任务会以难以定位的方式失败。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import get_settings


class LegacyMigrationsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyMigrations:
    """旧 ID -> 现行业务 ID 的翻译表。"""

    parameter_ids: dict[str, str] = field(default_factory=dict)
    result_ids: dict[str, str] = field(default_factory=dict)
    artifact_ids: dict[str, str] = field(default_factory=dict)
    handoff_inputs: dict[str, str] = field(default_factory=dict)
    legacy_input_alias_pattern: str = r"input_[A-Z]+[0-9]+"

    def translate_parameter(self, key: str) -> str:
        return self.parameter_ids.get(key, key)

    def translate_result(self, key: str) -> str:
        return self.result_ids.get(key, key)

    def translate_artifact(self, key: str) -> str:
        return self.artifact_ids.get(key, key)

    def translate_handoff_input(self, key: str) -> str:
        return self.handoff_inputs.get(key, key)

    def is_legacy_input_alias(self, alias: str) -> bool:
        return bool(re.fullmatch(self.legacy_input_alias_pattern, alias, re.IGNORECASE))


_EMPTY = LegacyMigrations()
_LOCK = threading.RLock()
_CACHE: tuple[tuple[int, int], LegacyMigrations] | None = None
_OVERRIDE: LegacyMigrations | None = None


def _parse(payload: object, path: Path) -> LegacyMigrations:
    if not isinstance(payload, dict):
        raise LegacyMigrationsUnavailable(f"迁移配置 {path.name} 的顶层必须是 JSON 对象")

    def mapping(key: str) -> dict[str, str]:
        raw = payload.get(key, {})
        if not isinstance(raw, dict):
            raise LegacyMigrationsUnavailable(f"迁移配置 {path.name} 的 {key} 必须是对象")
        for old, new in raw.items():
            if not isinstance(old, str) or not isinstance(new, str):
                raise LegacyMigrationsUnavailable(
                    f"迁移配置 {path.name} 的 {key}.{old!r} 必须是字符串到字符串"
                )
        return dict(raw)

    alias_pattern = payload.get("legacy_input_alias_pattern", _EMPTY.legacy_input_alias_pattern)
    if not isinstance(alias_pattern, str):
        raise LegacyMigrationsUnavailable(
            f"迁移配置 {path.name} 的 legacy_input_alias_pattern 必须是字符串"
        )
    try:
        re.compile(alias_pattern)
    except re.error as exc:
        raise LegacyMigrationsUnavailable(
            f"迁移配置 {path.name} 的 legacy_input_alias_pattern 不是合法正则：{exc}"
        ) from exc

    return LegacyMigrations(
        parameter_ids=mapping("parameter_ids"),
        result_ids=mapping("result_ids"),
        artifact_ids=mapping("artifact_ids"),
        handoff_inputs=mapping("handoff_inputs"),
        legacy_input_alias_pattern=alias_pattern,
    )


def get_legacy_migrations() -> LegacyMigrations:
    """读取迁移映射；按文件签名热重载，缺文件时视为「无迁移」。"""
    global _CACHE
    with _LOCK:
        if _OVERRIDE is not None:
            return _OVERRIDE
        path = get_settings().legacy_migrations
        try:
            stat = path.stat()
        except OSError:
            return _EMPTY
        signature = (stat.st_mtime_ns, stat.st_size)
        if _CACHE is not None and _CACHE[0] == signature:
            return _CACHE[1]
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise LegacyMigrationsUnavailable(
                f"迁移配置 {path.name} 无法解析：{exc}"
            ) from exc
        migrations = _parse(payload, path)
        _CACHE = (signature, migrations)
        return migrations


def set_legacy_migrations(migrations: LegacyMigrations | None) -> None:
    """覆盖迁移映射（供测试使用）；传 None 恢复从文件读取。"""
    global _OVERRIDE, _CACHE
    with _LOCK:
        _OVERRIDE = migrations
        _CACHE = None
