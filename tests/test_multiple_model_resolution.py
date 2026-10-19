import json
from pathlib import Path

import pytest

from app.model_catalog import ModelCatalogRegistry
from app.model_registry import ModelDefinitionRegistry, ModelDefinitionUnavailable, ModelNotFound
from app.model_router import ModelRouter


ROOT = Path(__file__).resolve().parents[1]
THERMAL_ID = "model_energy_saving_calc"
BOILER_ID = "boiler-efficiency"
THERMAL_CONFIG_PATH = ROOT / "config" / "models" / THERMAL_ID / "model.json"


def thermal_config() -> dict:
    """直接读配置文件作为「期望值」来源。

    参数、结果和模板清单会随业务扩展而变化，测试不应该把它们抄写一遍——
    否则每加一个参数就要改一大片测试，扩展成本反而更高。这里改成与配置对账，
    仍然能抓住「加载器丢字段」「两个模型串味」这类真实缺陷。
    """
    return json.loads(THERMAL_CONFIG_PATH.read_text(encoding="utf-8"))


def expected_parameters(payload: dict) -> set[str]:
    return set(payload["parameters"])


def expected_results(payload: dict) -> set[str]:
    return set(payload["results"])


def expected_templates(payload: dict) -> set[str]:
    return {artifact_template(artifact) for artifact in payload["artifacts"].values()}


def artifact_template(artifact: dict) -> str:
    """取 artifact 的模板文件名。

    模型文件里可能只写了 ``template_ref``，这时要顺着 config/templates/<id>.json
    再读一层——测试独立地做这件事，才能真正校验「模型 + 模板适配层」的组合结果，
    而不是把被测代码算出来的东西再断言一遍。
    """
    if artifact.get("template"):
        return artifact["template"]
    reference = artifact["template_ref"]
    catalog = json.loads(
        (ROOT / "config" / "templates" / f"{reference}.json").read_text(encoding="utf-8")
    )
    return catalog["template"]


def ordered_artifact_templates(payload: dict) -> list[str]:
    """按 workflow 声明顺序返回模板文件名。"""
    return [
        artifact_template(payload["artifacts"][step["artifact"]])
        for step in payload["workflow"]["steps"]
    ]


def boiler_definition_payload():
    return {
        "schema_version": "2.0",
        "model": {
            "id": BOILER_ID,
            "version": 1,
            "name": "锅炉热效率计算",
            "description": "根据燃料消耗、低位发热量和蒸汽产量计算锅炉效率。",
        },
        "parameters": {
            "fuel_consumption": {
                "display_name": "燃料消耗量",
                "description": "锅炉燃料消耗量。",
                "type": "number",
                "unit": "kg/h",
                "required": True,
                "input": {"aliases": ["燃料消耗", "燃料量"]},
                "validation": {"minimum": 0, "maximum": None},
                "missing": {"policy": "error"},
            },
            "lower_heating_value": {
                "display_name": "低位发热量",
                "description": "燃料低位发热量。",
                "type": "number",
                "unit": "kJ/kg",
                "required": True,
                "input": {"aliases": ["低位热值"]},
                "validation": {"minimum": 0, "maximum": None},
                "missing": {"policy": "error"},
            },
            "steam_output": {
                "display_name": "蒸汽产量",
                "description": "锅炉蒸汽产量。",
                "type": "number",
                "unit": "kg/h",
                "required": True,
                "input": {"aliases": ["蒸汽量"]},
                "validation": {"minimum": 0, "maximum": None},
                "missing": {"policy": "error"},
            },
        },
        "results": {
            "boiler_efficiency": {
                "display_name": "锅炉热效率",
                "type": "number",
                "unit": "%",
                "required": True,
            }
        },
        "artifacts": {
            "boiler_calculation": {
                "type": "excel",
                "display_name": "锅炉计算模型",
                "template": "boiler-efficiency.xlsx",
            }
        },
        "bindings": {
            "boiler_calculation": {
                "inputs": {
                    "fuel_consumption": {"sheet": "锅炉", "cell": "B2"},
                    "lower_heating_value": {"sheet": "锅炉", "cell": "B3"},
                    "steam_output": {"sheet": "锅炉", "cell": "B4"},
                },
                "outputs": {
                    "boiler_efficiency": {"sheet": "锅炉", "cell": "B5"}
                },
            }
        },
        "workflow": {
            "steps": [
                {
                    "id": "boiler_calculation",
                    "type": "excel_calculation",
                    "artifact": "boiler_calculation",
                    "depends_on": [],
                }
            ]
        },
        "handoffs": [],
    }


def registry_payload(*, boiler_enabled=True):
    return {
        "models": [
            {
                "id": THERMAL_ID,
                "name": "空气预热器工艺计算",
                "description": "空气预热器相关工艺计算。",
                "keywords": ["空气预热器", "热负荷"],
                "category": "thermal",
                "enabled": True,
            },
            {
                "id": BOILER_ID,
                "name": "锅炉热效率计算",
                "description": "锅炉效率计算。",
                "keywords": ["锅炉", "热效率"],
                "category": "boiler",
                "enabled": boiler_enabled,
            },
        ]
    }


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def build_resolver(tmp_path, *, boiler_enabled=True, mismatched_boiler_id=None):
    definitions_root = tmp_path / "models"
    thermal_path = ROOT / "config" / "models" / THERMAL_ID / "model.json"
    thermal = json.loads(thermal_path.read_text(encoding="utf-8"))
    write_json(definitions_root / THERMAL_ID / "model.json", thermal)

    boiler = boiler_definition_payload()
    if mismatched_boiler_id:
        boiler["model"]["id"] = mismatched_boiler_id
    write_json(definitions_root / BOILER_ID / "model.json", boiler)

    catalog_path = tmp_path / "model_registry.json"
    write_json(catalog_path, registry_payload(boiler_enabled=boiler_enabled))
    catalog = ModelCatalogRegistry(catalog_path)
    resolver = ModelDefinitionRegistry(
        catalog=catalog,
        definitions_root=definitions_root,
    )
    return resolver, catalog


def test_multiple_models_resolve_to_distinct_definitions(tmp_path):
    resolver, _ = build_resolver(tmp_path)

    thermal = resolver.get(THERMAL_ID)
    boiler = resolver.get(BOILER_ID)
    thermal_expected = thermal_config()
    boiler_expected = boiler_definition_payload()

    assert thermal.model.id == THERMAL_ID
    assert boiler.model.id == BOILER_ID
    assert thermal.parameters.keys() != boiler.parameters.keys()
    assert set(thermal.parameters) == expected_parameters(thermal_expected)
    assert set(thermal.results) == expected_results(thermal_expected)
    assert set(boiler.parameters) == expected_parameters(boiler_expected)
    assert set(thermal.artifacts) == set(thermal_expected["artifacts"])


def test_resolver_reports_unknown_model_as_model_not_found(tmp_path):
    resolver, _ = build_resolver(tmp_path)

    with pytest.raises(ModelNotFound, match="does-not-exist"):
        resolver.get("does-not-exist")


def test_registry_and_definition_ids_must_match(tmp_path):
    resolver, _ = build_resolver(tmp_path, mismatched_boiler_id="wrong-model-id")

    with pytest.raises(ModelDefinitionUnavailable, match="不一致"):
        resolver.get(BOILER_ID)


def test_enabled_catalog_entries_are_validated_at_startup(tmp_path):
    resolver, _ = build_resolver(tmp_path)
    (tmp_path / "models" / BOILER_ID / "model.json").unlink()

    with pytest.raises(ModelNotFound, match=BOILER_ID):
        resolver.validate_enabled()


def test_disabled_model_is_not_resolvable(tmp_path):
    resolver, _ = build_resolver(tmp_path, boiler_enabled=False)

    with pytest.raises(ModelNotFound, match="未启用"):
        resolver.get(BOILER_ID)


def test_router_result_can_resolve_second_model(tmp_path):
    resolver, catalog = build_resolver(tmp_path)

    class BoilerRouterLLM:
        def route_model(self, user_message, candidates):
            assert {candidate.id for candidate in candidates} == {THERMAL_ID, BOILER_ID}
            return {"intent": "calculate", "model_id": BOILER_ID}

    route = ModelRouter(BoilerRouterLLM(), catalog).route("帮我计算锅炉热效率")
    definition = resolver.get(route.model_id)

    assert definition.model.id == BOILER_ID
    assert set(definition.parameters) == expected_parameters(boiler_definition_payload())


def test_api_resolves_explicit_model_id_and_lists_multiple_models(tmp_path, monkeypatch):
    import app.api as api_module
    from fastapi.testclient import TestClient
    from app.domain import ModelRoute
    from app.storage import Storage

    resolver, catalog = build_resolver(tmp_path)
    monkeypatch.setattr(api_module, "model_registry", resolver)
    monkeypatch.setattr(api_module, "model_catalog", catalog)
    monkeypatch.setattr(api_module, "storage", Storage(tmp_path / "api.sqlite3"))

    class FakeLLM:
        def __init__(self, settings):
            pass

        def route_model(self, message, candidates):
            return ModelRoute(intent="calculate", model_id=THERMAL_ID)

        def extract_parameters(self, model, message, current_values):
            return {"parameters": {"heat_load": 240}, "clarification": None}

    class FakeRouter:
        call_count = 0

        def __init__(self, llm, model_catalog, resolver=None):
            self.llm = llm
            self.resolver = resolver

        def route(self, message):
            type(self).call_count += 1
            return self.llm.route_model(message, [])

        def validate_explicit(self, model_id):
            assert model_id in {THERMAL_ID, BOILER_ID}

    monkeypatch.setattr(api_module, "DeepSeekClient", FakeLLM)
    monkeypatch.setattr(api_module, "ModelRouter", FakeRouter)

    with TestClient(api_module.app) as client:
        models = client.get("/api/v1/models")
        assert models.status_code == 200
        assert {item["model_id"] for item in models.json()} == {THERMAL_ID, BOILER_ID}

        session = client.post(
            "/api/v1/sessions", json={"model_id": BOILER_ID}
        )
        assert session.status_code == 201
        assert session.json()["model_id"] == BOILER_ID

        unbound_session = client.post("/api/v1/sessions", json={})
        assert unbound_session.status_code == 201
        assert unbound_session.json()["model_id"] is None

        first_message = client.post(
            f"/api/v1/sessions/{unbound_session.json()['session_id']}/messages",
            json={"message": "帮我计算空气预热器，热负荷240MW"},
        )
        assert first_message.status_code == 200
        assert first_message.json()["parameters"]["heat_load"] == 240.0
        assert FakeRouter.call_count == 1
        assert api_module.storage.get_session(
            unbound_session.json()["session_id"], "local-user"
        )["model_id"] == THERMAL_ID


def test_parameter_extractor_service_receives_resolved_boiler_definition(tmp_path):
    from app.service import ParameterService
    from app.storage import Storage

    resolver, _ = build_resolver(tmp_path)
    boiler = resolver.get(BOILER_ID)

    class FakeLLM:
        received_parameter_ids = None

        def extract_parameters(self, model, user_message, current_values):
            self.received_parameter_ids = set(model.parameters)
            return {
                "parameters": {
                    "fuel_consumption": 100,
                    "lower_heating_value": 42000,
                    "steam_output": 900,
                },
                "clarification": None,
            }

    class FakeRouter:
        def route(self, message):
            from app.domain import ModelRoute
            return ModelRoute(intent="calculate", model_id=BOILER_ID)

        def validate_explicit(self, model_id):
            assert model_id == BOILER_ID

    llm = FakeLLM()
    service = ParameterService(
        Storage(tmp_path / "service.sqlite3"), boiler, llm, FakeRouter()
    )
    session = service.create_session("tester")
    draft = service.process_message("tester", session["session_id"], "帮我算锅炉热效率")

    assert llm.received_parameter_ids == expected_parameters(boiler_definition_payload())
    assert draft["parameters"] == {
        "fuel_consumption": 100.0,
        "lower_heating_value": 42000.0,
        "steam_output": 900.0,
    }


def test_registry_without_catalog_only_resolves_configured_definitions_root(tmp_path):
    """没有 catalog 时不再有「单文件兜底」：模型定义一律来自 config/models/<id>/。"""
    registry = ModelDefinitionRegistry(definitions_root=ROOT / "config" / "models")

    assert registry.get(THERMAL_ID).model.id == THERMAL_ID
    with pytest.raises(ModelNotFound, match="does-not-exist"):
        registry.get("does-not-exist")
