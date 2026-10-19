import json
from pathlib import Path

import pytest

from app.domain import CalculationIntent, ModelRoute
from app.model_catalog import ModelCatalogRegistry
from app.model_router import InvalidModelRoute, ModelRouter


ROOT = Path(__file__).resolve().parents[1]


class FakeRouterLLM:
    def __init__(self, route):
        self.result = route
        self.candidates = None

    def route_model(self, message, candidates):
        self.candidates = candidates
        return self.result


def build_router(tmp_path, route, registry_payload=None):
    registry_path = tmp_path / "model_registry.json"
    if registry_payload is None:
        registry_path.write_text(
            (ROOT / "config" / "model_registry.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    else:
        registry_path.write_text(json.dumps(registry_payload, ensure_ascii=False), encoding="utf-8")
    llm = FakeRouterLLM(route)
    return ModelRouter(llm, ModelCatalogRegistry(registry_path)), llm


def enabled_registry_ids() -> set[str]:
    registry = json.loads((ROOT / "config" / "model_registry.json").read_text(encoding="utf-8"))
    return {entry["id"] for entry in registry["models"] if entry["enabled"]}


def test_air_preheater_message_routes_to_registered_calculation_model(tmp_path):
    router, llm = build_router(
        tmp_path,
        {"intent": "calculate", "model_id": "model_energy_saving_calc"},
    )

    route = router.route("帮我算一下空气预热器，热负荷240MW")

    # 候选必须是注册表里**全部**已启用模型——以后加模型不应该让这条测试失效
    assert {candidate.id for candidate in llm.candidates} == enabled_registry_ids()
    assert route.model_id in enabled_registry_ids()


@pytest.mark.parametrize("model_id", ["fake-model", "B11", "节能减排计算.xlsm"])
def test_router_rejects_unknown_or_excel_derived_model_id(tmp_path, model_id):
    router, _ = build_router(
        tmp_path,
        {"intent": "calculate", "model_id": model_id},
    )

    with pytest.raises(InvalidModelRoute):
        router.route("帮我计算")


def test_clarification_returns_without_resolving_or_executing_a_model(tmp_path):
    registry = {
        "models": [
            {
                "id": "model_energy_saving_calc",
                "name": "空气预热器工艺计算",
                "description": "热工性能计算",
                "keywords": ["空气预热器", "热负荷"],
                "category": "thermal",
                "enabled": True,
            },
            {
                "id": "heat-exchanger",
                "name": "换热器计算",
                "description": "换热器相关计算",
                "keywords": ["换热器"],
                "category": "thermal",
                "enabled": True,
            },
        ]
    }
    router, llm = build_router(
        tmp_path,
        {
            "intent": "clarification_required",
            "candidate_models": ["model_energy_saving_calc", "heat-exchanger"],
            "clarification": "请确认需要进行哪一种计算",
        },
        registry,
    )

    route = router.route("帮我算一下预热器")

    assert route.intent == CalculationIntent.CLARIFICATION_REQUIRED
    assert route.model_id is None
    assert route.candidate_models == ["model_energy_saving_calc", "heat-exchanger"]
    assert {candidate.id for candidate in llm.candidates} == {
        "model_energy_saving_calc", "heat-exchanger"
    }


def test_router_rejects_extra_parameter_extraction_fields(tmp_path):
    router, _ = build_router(
        tmp_path,
        {
            "intent": "calculate",
            "model_id": "model_energy_saving_calc",
            "parameters": {"heat_load": 240},
        },
    )

    with pytest.raises(InvalidModelRoute):
        router.route("热负荷240MW")


def test_domain_route_schema_rejects_invalid_calculate_and_clarification_shapes():
    with pytest.raises(ValueError):
        ModelRoute.model_validate({"intent": "calculate", "model_id": None})
    with pytest.raises(ValueError):
        ModelRoute.model_validate(
            {"intent": "clarification_required", "model_id": "model_energy_saving_calc"}
        )
    with pytest.raises(ValueError):
        ModelRoute.model_validate(
            {"intent": "clarification_required", "clarification": "请澄清", "model_id": None,
             "candidate_models": ["B11"]}
        )
