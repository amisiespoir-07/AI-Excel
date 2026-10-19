from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from .domain import CalculationIntent, ModelRoute
from .model_catalog import ModelCatalogRegistry


class InvalidModelRoute(RuntimeError):
    pass


@dataclass(frozen=True)
class RouteCandidate:
    """交给路由器的候选模型。

    只包含业务语义，绝不包含文件名、工作表名或单元格地址。

    ``parameter_hints`` 是这个模型能接受的业务参数提法（显示名 + 自然语言别名）。
    没有它，两个都叫「空气预热器」的模型只能靠 keywords 猜——实测 48 次全选了同一个；
    有了它，「烟气量」这种只属于某一个模型的提法就能一眼定位。
    """

    id: str
    name: str
    description: str
    keywords: tuple[str, ...] = ()
    category: str = ""
    enabled: bool = True
    parameter_hints: tuple[str, ...] = ()


class ModelRouter:
    """Route a message to an enabled business model without exposing adapters."""

    def __init__(self, llm, catalog: ModelCatalogRegistry, resolver=None):
        self.llm = llm
        self.catalog = catalog
        # 用于给候选补充参数提法；缺省时不补充（路由仍可用，只是判别力弱一些）
        self.resolver = resolver

    def validate_explicit(self, model_id: str) -> None:
        entry = self.catalog.get_entry(model_id)
        if entry is None or not entry.enabled:
            raise InvalidModelRoute(f"模型不存在或未启用：{model_id}")

    def _candidate(self, entry) -> RouteCandidate:
        hints: list[str] = []
        if self.resolver is not None:
            try:
                definition = self.resolver.get(entry.id)
            except Exception:
                definition = None
            if definition is not None:
                for spec in definition.parameters.values():
                    for name in (spec.display_name, *spec.aliases):
                        if name and name not in hints:
                            hints.append(name)
        return RouteCandidate(
            id=entry.id,
            name=entry.name,
            description=entry.description,
            keywords=tuple(entry.keywords),
            category=entry.category,
            enabled=entry.enabled,
            parameter_hints=tuple(hints),
        )

    def route(self, user_message: str) -> ModelRoute:
        entries = self.catalog.get_enabled()
        if not entries:
            raise InvalidModelRoute("没有已启用的计算模型")
        candidates = [self._candidate(entry) for entry in entries]

        try:
            route = ModelRoute.model_validate(self.llm.route_model(user_message, candidates))
        except ValidationError as exc:
            raise InvalidModelRoute("Router 输出不符合路由 Schema") from exc
        enabled_ids = {candidate.id for candidate in candidates}
        if any(model_id not in enabled_ids for model_id in route.candidate_models):
            raise InvalidModelRoute("Router 返回了注册表中不存在或未启用的候选模型")

        if route.intent == CalculationIntent.CLARIFICATION_REQUIRED:
            return route

        if route.model_id not in enabled_ids:
            raise InvalidModelRoute(f"模型不存在或未启用：{route.model_id}")

        self.validate_explicit(route.model_id)
        return route
