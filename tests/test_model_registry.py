import json
from pathlib import Path

import pytest

from app.model_registry import ModelDefinitionRegistry, ModelDefinitionUnavailable


ROOT = Path(__file__).resolve().parents[1]
CHAINED_ID = "model_energy_saving_calc"


def write_model(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def prepare_config(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    """把模型定义与模板适配层都复制到 tmp，方便在测试里安全地改配置。

    返回 (definitions_root, model_path, templates_root, payload)。
    """
    definitions_root = tmp_path / "models"
    model_path = definitions_root / CHAINED_ID / "model.json"
    model_path.parent.mkdir(parents=True)

    templates_root = tmp_path / "templates"
    templates_root.mkdir()
    for entry in (ROOT / "config" / "templates").glob("*.json"):
        (templates_root / entry.name).write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")

    payload = json.loads(
        (ROOT / "config" / "models" / CHAINED_ID / "model.json").read_text(encoding="utf-8")
    )
    write_model(model_path, payload)
    return definitions_root, model_path, templates_root, payload


def add_slot(templates_root: Path, catalog_id: str, slot: str, sheet: str, cell: str) -> None:
    path = templates_root / f"{catalog_id}.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["inputs"][slot] = {"sheet": sheet, "cell": cell}
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")


def test_registry_reloads_when_model_json_changes(tmp_path: Path):
    definitions_root, model_path, templates_root, config = prepare_config(tmp_path)
    registry = ModelDefinitionRegistry(
        definitions_root=definitions_root, templates_root=templates_root
    )

    first = registry.get(CHAINED_ID)
    assert first.model.version == config["model"]["version"]
    assert first.canonical_parameter_id("热负载") == "heat_load"

    # 新增参数：单元格加在模板适配层，参数同名，于是**模型侧一行都不用改**
    # （模型没写 bindings，同名槽位自动接入）
    add_slot(templates_root, "energy_saving_calc", "test_extra", "工艺计算", "C15")
    config["model"]["version"] += 1
    config["parameters"]["test_extra"] = {
        "description": "测试新增参数",
        "display_name": "测试新增参数",
        "type": "number",
        "unit": "kg",
        "required": False,
        "input": {"aliases": ["测试额外参数"]},
        "validation": {"minimum": None, "maximum": None},
        "missing": {"policy": "template_default"},
    }
    assert "bindings" not in config, "串联模型现在整段省略 bindings"
    write_model(model_path, config)

    second = registry.get(CHAINED_ID)
    assert second.model.version == config["model"]["version"]
    assert second is not first
    assert second.parameters["test_extra"].unit == "kg"
    assert second.bindings["energy_saving_calc"].inputs["test_extra"].cell == "C15"


def test_registry_reloads_when_only_the_template_catalog_changes(tmp_path: Path):
    """只改模板适配层（挪单元格）也必须生效，否则「解耦」等于白做。"""
    definitions_root, _, templates_root, _ = prepare_config(tmp_path)
    registry = ModelDefinitionRegistry(
        definitions_root=definitions_root, templates_root=templates_root
    )

    before = registry.get(CHAINED_ID).bindings["energy_saving_calc"].inputs["heat_load"].cell
    assert before == "B11"

    path = templates_root / "energy_saving_calc.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    catalog["inputs"]["heat_load"]["cell"] = "C15"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    after = registry.get(CHAINED_ID).bindings["energy_saving_calc"].inputs["heat_load"].cell
    assert after == "C15"


def test_registry_rejects_invalid_edit_then_recovers(tmp_path: Path):
    definitions_root, model_path, templates_root, _ = prepare_config(tmp_path)
    original = model_path.read_text(encoding="utf-8")
    registry = ModelDefinitionRegistry(
        definitions_root=definitions_root, templates_root=templates_root
    )
    previous = registry.get(CHAINED_ID)

    model_path.write_text("{ invalid json", encoding="utf-8")
    with pytest.raises(ModelDefinitionUnavailable):
        registry.get(CHAINED_ID)

    model_path.write_text(original, encoding="utf-8")
    recovered = registry.get(CHAINED_ID)
    assert recovered.model_id == previous.model_id


def test_unknown_model_id_has_no_fallback_definition(tmp_path: Path):
    """回退机制已移除：未注册/无定义文件的模型 ID 必须直接报错，不许悄悄换成别的模型。"""
    from app.model_registry import ModelNotFound

    definitions_root, _, templates_root, _ = prepare_config(tmp_path)
    registry = ModelDefinitionRegistry(
        definitions_root=definitions_root, templates_root=templates_root
    )

    with pytest.raises(ModelNotFound, match="未找到模型定义"):
        registry.get("not-a-model")


def test_api_models_endpoint_picks_up_model_json_edits_without_restart(tmp_path: Path, monkeypatch):
    import app.api as api_module
    from app.model_catalog import ModelCatalogRegistry

    definitions_root, model_path, templates_root, config = prepare_config(tmp_path)

    # 只留热负荷模型，让这条测试不依赖真实注册表里当前还有哪些模型
    catalog_path = tmp_path / "model_registry.json"
    catalog_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": config["model"]["id"],
                        "name": config["model"]["name"],
                        "description": config["model"]["description"],
                        "keywords": [],
                        "category": "thermal",
                        "enabled": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    registry = ModelDefinitionRegistry(
        definitions_root=definitions_root, templates_root=templates_root
    )
    monkeypatch.setattr(api_module, "model_registry", registry)
    monkeypatch.setattr(api_module, "model_catalog", ModelCatalogRegistry(catalog_path))
    from fastapi.testclient import TestClient

    client = TestClient(api_module.app)
    before = client.get("/api/v1/models")
    assert before.status_code == 200
    assert before.json()[0]["version"] == config["model"]["version"]

    add_slot(templates_root, "energy_saving_calc", "runtime_added", "工艺计算", "D15")
    config["model"]["version"] += 1
    config["parameters"]["runtime_added"] = {
        "description": "运行期间新增的参数",
        "display_name": "运行时参数",
        "type": "number",
        "unit": "kg",
        "required": False,
        "input": {"aliases": []},
        "validation": {"minimum": None, "maximum": None},
        "missing": {"policy": "template_default"},
    }
    assert "bindings" not in config, "串联模型现在整段省略 bindings"
    write_model(model_path, config)

    after = client.get("/api/v1/models")
    assert after.status_code == 200
    assert after.json()[0]["version"] == config["model"]["version"]
    assert after.json()[0]["parameters"]["runtime_added"]["display_name"] == "运行时参数"
