from __future__ import annotations

import json
import threading
from pathlib import Path

from .domain import ModelRegistry, ModelRegistryEntry


class ModelCatalogUnavailable(RuntimeError):
    pass


class ModelCatalogRegistry:
    """Reload business-only routing metadata when the registry file changes."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._lock = threading.RLock()
        self._signature: tuple[int, int] | None = None
        self._registry: ModelRegistry | None = None

    def get(self) -> ModelRegistry:
        with self._lock:
            try:
                stat = self.path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if self._registry is not None and signature == self._signature:
                    return self._registry
                with self.path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                registry = ModelRegistry.model_validate(payload)
            except Exception as exc:
                raise ModelCatalogUnavailable(
                    f"模型注册表 {self.path.name} 无法加载或校验失败：{exc}"
                ) from exc
            self._registry = registry
            self._signature = signature
            return registry

    def get_enabled(self) -> list[ModelRegistryEntry]:
        return [model for model in self.get().models if model.enabled]

    def get_entry(self, model_id: str) -> ModelRegistryEntry | None:
        return next((entry for entry in self.get().models if entry.id == model_id), None)
