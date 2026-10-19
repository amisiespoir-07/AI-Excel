from __future__ import annotations

import threading
from pathlib import Path

from .config import get_settings
from .domain import (
    ModelDefinition,
    default_template_catalog_root,
    load_model_definition,
    template_catalog_signature,
)
from .model_catalog import ModelCatalogRegistry


class ModelDefinitionUnavailable(RuntimeError):
    pass


class ModelNotFound(ModelDefinitionUnavailable):
    pass


class ModelDefinitionRegistry:
    """把模型 ID 解析成经过校验、按文件签名热重载的模型定义。

    模型定义一律来自 ``config/models/<model_id>/model.json``——没有别的来源，
    也没有「旧单文件配置」的兜底分支（那条路早已不可达，留着只会让人误解配置从哪来）。
    """

    def __init__(
        self,
        catalog: ModelCatalogRegistry | None = None,
        definitions_root: Path | None = None,
        templates_root: Path | None = None,
    ):
        settings = get_settings()
        self.definitions_root = (definitions_root or settings.model_definitions_root).resolve()
        # 模板适配层目录（config/templates/*.json）；缺省时由 domain 走全局设置。
        self.templates_root = templates_root
        self.catalog = catalog
        self._lock = threading.RLock()
        self._definitions: dict[str, tuple[tuple, ModelDefinition]] = {}

    @property
    def catalog_root(self) -> Path:
        """模板适配层目录；未显式配置时用全局设置里的默认值。"""
        return (
            self.templates_root
            if self.templates_root is not None
            else default_template_catalog_root()
        )

    def _definition_path(self, model_id: str) -> Path:
        if self.catalog is not None:
            entry = self.catalog.get_entry(model_id)
            if entry is None or not entry.enabled:
                raise ModelNotFound(f"模型不存在或未启用：{model_id}")

        # Registry IDs are validated slugs. Resolve and contain the path anyway
        # so a future schema change cannot turn an ID into path traversal.
        candidate = (self.definitions_root / model_id / "model.json").resolve()
        try:
            candidate.relative_to(self.definitions_root)
        except ValueError as exc:
            raise ModelNotFound(f"模型 ID 无效：{model_id}") from exc
        if candidate.is_file():
            return candidate
        raise ModelNotFound(f"未找到模型定义：{model_id}")

    def get(self, model_id: str) -> ModelDefinition:
        with self._lock:
            definition_path = self._definition_path(model_id)
            try:
                stat = definition_path.stat()
            except OSError as exc:
                raise ModelDefinitionUnavailable(
                    f"无法读取模型定义：{definition_path.name}"
                ) from exc

            signature = (
                stat.st_mtime_ns,
                stat.st_size,
                template_catalog_signature(self.catalog_root),
            )
            cached = self._definitions.get(model_id)
            if cached is not None and cached[0] == signature:
                return cached[1]

            try:
                candidate = load_model_definition(definition_path, self.templates_root)
            except Exception as exc:
                raise ModelDefinitionUnavailable(
                    f"模型定义 {definition_path.name} 无法加载或校验失败：{exc}"
                ) from exc
            if candidate.model.id != model_id:
                raise ModelDefinitionUnavailable(
                    f"Registry ID {model_id!r} 与 Definition ID {candidate.model.id!r} 不一致"
                )
            self._definitions[model_id] = (signature, candidate)
            return candidate

    def validate_enabled(self) -> list[ModelDefinition]:
        """Fail early if any enabled catalog entry lacks a matching definition."""
        if self.catalog is None:
            raise ModelDefinitionUnavailable("模型定义 Registry 未配置 Model Catalog")
        return [self.get(entry.id) for entry in self.catalog.get_enabled()]
