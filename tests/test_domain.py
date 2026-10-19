import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.domain import (
    ModelDefinition,
    load_model_definition,
    migrate_model_snapshot,
    validate_parameters,
)


ROOT = Path(__file__).resolve().parents[1]
MODEL_CONFIG_PATH = ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json"


@pytest.fixture
def historical_migrations():
    """历史 ID 迁移映射。

    本部署已删除 config/legacy_migrations.json（部署方的判断，理由见 config/README.md），
    所以需要翻译能力的测试显式注入映射表——测的是**机制**，不是那个文件在不在。
    """
    from app.legacy import set_legacy_migrations
    from test_legacy_migrations import HISTORICAL_MAPPINGS

    set_legacy_migrations(HISTORICAL_MAPPINGS)
    yield
    set_legacy_migrations(None)


def configured_payload() -> dict:
    """期望值直接来自配置文件：加参数/加工作簿不应该让这些测试失效。"""
    return json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))


def required_parameter_ids(model: ModelDefinition) -> list[str]:
    return [key for key, spec in model.parameters.items() if spec.missing.policy == "error"]


def test_schema_v2_business_model_is_independent_from_excel_cells():
    model = load_model_definition(MODEL_CONFIG_PATH)
    payload = configured_payload()
    assert model.schema_version == "2.0"
    assert model.model.id == payload["model"]["id"]
    assert model.model.version == payload["model"]["version"]
    assert set(model.parameters) == set(payload["parameters"])
    assert "input_b11" not in model.parameters
    assert "input_t11" not in model.parameters
    assert "input_g74" not in model.parameters
    assert model.parameters["heat_load"].input.aliases == ["热负荷", "热负载", "heat load"]


def test_bindings_map_business_parameters_to_current_template_cells():
    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    binding = model.bindings["energy_saving_calc"]
    assert model.artifacts["energy_saving_calc"].template == "节能减排计算.xlsm"
    assert binding.inputs["heat_load"].sheet == "工艺计算"
    assert binding.inputs["heat_load"].cell == "B11"
    assert binding.inputs["fuel_hydrogen_content"].cell_ref == "工艺计算!T11"
    assert binding.inputs["target_exhaust_temperature"].cell_ref == "预热器计算!G74"


def test_business_handoff_ids_resolve_to_excel_bindings():
    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    assert model.bindings["energy_saving_calc"].outputs["flue_gas_flow"].cell == "S63"
    assert model.bindings["air_preheater"].inputs["flue_gas_flow"].cell == "AA14"
    assert model.bindings["energy_saving_calc"].outputs["air_flow"].cell == "U63"
    assert model.bindings["air_preheater"].inputs["air_flow"].cell == "AO14"
    assert {(handoff.source, handoff.target) for handoff in model.handoffs} == {
        ("energy_saving_calc.flue_gas_flow", "air_preheater.flue_gas_flow"),
        ("energy_saving_calc.air_flow", "air_preheater.air_flow"),
    }
    assert [step.artifact for step in model.ordered_steps()] == ["energy_saving_calc", "air_preheater"]


def test_optional_parameters_retain_template_defaults_and_required_heat_load():
    model = load_model_definition(MODEL_CONFIG_PATH)
    assert model.parameters["heat_load"].missing.policy == "error"
    assert model.parameters["fuel_hydrogen_content"].missing.policy == "template_default"
    assert model.parameters["target_exhaust_temperature"].missing.policy == "template_default"
    values, missing = validate_parameters(model, {"heat_load": 240}, require_all=True)
    assert values == {"heat_load": 240.0}
    # 只有声明为必填（missing.policy == "error"）的参数才可能阻塞确认；
    # 这样以后新增可选参数不会让这条测试失效。
    assert missing == [key for key in required_parameter_ids(model) if key != "heat_load"]
    _, missing = validate_parameters(model, {}, require_all=True)
    assert missing == required_parameter_ids(model)


def test_changing_cell_binding_does_not_change_business_schema():
    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    original_parameters = deepcopy(model.parameters)
    payload = model.model_dump(mode="json")
    payload["bindings"]["energy_saving_calc"]["inputs"]["heat_load"] = {
        "sheet": "工艺计算", "cell": "C15"
    }
    moved = ModelDefinition.model_validate(payload)
    assert moved.parameters == original_parameters
    assert moved.bindings["energy_saving_calc"].inputs["heat_load"].cell == "C15"


def test_changing_artifact_template_does_not_change_business_schema():
    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    payload = model.model_dump(mode="json")
    payload["artifacts"]["energy_saving_calc"]["template"] = "new-template.xlsm"
    changed = ModelDefinition.model_validate(payload)
    assert changed.artifacts["energy_saving_calc"].template == "new-template.xlsm"
    assert changed.parameters == model.parameters
    assert changed.results == model.results


def test_schema_validation_rejects_unknown_binding_reference_and_bad_cell():
    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    bad_reference = model.model_dump(mode="json")
    bad_reference["bindings"]["energy_saving_calc"]["inputs"]["unknown"] = {
        "sheet": "工艺计算", "cell": "C15"
    }
    with pytest.raises(ValueError, match="unknown parameter/result input"):
        ModelDefinition.model_validate(bad_reference)

    bad_cell = model.model_dump(mode="json")
    bad_cell["bindings"]["energy_saving_calc"]["inputs"]["heat_load"]["cell"] = "B0"
    with pytest.raises(ValueError, match="invalid Excel cell"):
        ModelDefinition.model_validate(bad_cell)


def test_schema_validation_rejects_duplicate_input_cells_and_invalid_handoff():
    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    duplicate = model.model_dump(mode="json")
    duplicate["bindings"]["energy_saving_calc"]["inputs"]["fuel_hydrogen_content"] = {
        "sheet": "工艺计算", "cell": "B11"
    }
    with pytest.raises(ValueError, match="same Excel cell"):
        ModelDefinition.model_validate(duplicate)

    bad_handoff = model.model_dump(mode="json")
    bad_handoff["handoffs"][0]["source"] = "energy_saving_calc.B11"
    with pytest.raises(ValueError, match="unknown result"):
        ModelDefinition.model_validate(bad_handoff)


def test_schema_validation_rejects_workflow_dependency_cycle():
    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    payload = model.model_dump(mode="json")
    payload["workflow"]["steps"][0]["depends_on"] = ["air_preheater"]
    with pytest.raises(ValueError, match="dependency cycle"):
        ModelDefinition.model_validate(payload)


def test_template_bindings_match_real_excel_artifacts():
    import openpyxl

    model = load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")
    for artifact_id, artifact in model.artifacts.items():
        workbook_path = ROOT / "templates" / artifact.template
        assert workbook_path.is_file(), f"missing artifact template: {workbook_path}"
        workbook = openpyxl.load_workbook(workbook_path, read_only=True, data_only=False, keep_vba=True)
        try:
            binding = model.bindings[artifact_id]
            for cell_binding in [*binding.inputs.values(), *binding.outputs.values()]:
                assert cell_binding.sheet in workbook.sheetnames
                assert workbook[cell_binding.sheet][cell_binding.cell] is not None
        finally:
            workbook.close()


def test_legacy_queued_job_snapshot_migrates_to_schema_v2(historical_migrations):
    legacy = {
        "model_id": "old-model",
        "version": 8,
        "description": "legacy",
        "parameters": {
            "heat_load_mw": {"description": "Heat load", "display_name": "Heat load", "type": "number", "unit": "MW", "required": True},
            "input_t11": {"description": "Hydrogen", "type": "number", "unit": "%", "required": False},
            "input_g74": {"description": "Temperature", "type": "number", "unit": "°C", "required": False},
        },
        "workbooks": [
            {
                "workbook_id": "calculation", "template": "thermal.xlsm",
                "inputs": {
                    "heat_load_mw": {"cell_ref": "Process!B11"},
                    "input_t11": {"cell_ref": "Process!T11", "missing_value_policy": "template_default"},
                    "input_g74": {"cell_ref": "Heater!G74", "missing_value_policy": "template_default"},
                },
                "outputs": [
                    {"output_id": "b22", "sheet": "Process", "cell": "B22"},
                    {"output_id": "s63", "sheet": "Heater", "cell": "S63"},
                    {"output_id": "u63", "sheet": "Heater", "cell": "U63"},
                ],
            },
            {
                "workbook_id": "air_preheater", "template": "air.xlsm",
                "inputs": {
                    "from_calculation_s63": {"cell_ref": "Air!AA14"},
                    "from_calculation_u63": {"cell_ref": "Air!AO14"},
                },
                "outputs": [],
            },
        ],
        "handoffs": [
            {"source": "calculation.s63", "target": "air_preheater.from_calculation_s63"},
            {"source": "calculation.u63", "target": "air_preheater.from_calculation_u63"},
        ],
    }
    migrated = ModelDefinition.model_validate(migrate_model_snapshot(legacy))
    assert migrated.schema_version == "2.0"
    assert set(migrated.parameters) == {"heat_load", "fuel_hydrogen_content", "target_exhaust_temperature"}
    assert migrated.bindings["energy_saving_calc"].inputs["heat_load"].cell == "B11"
    assert migrated.bindings["air_preheater"].inputs["flue_gas_flow"].cell == "AA14"
    assert {handoff.source for handoff in migrated.handoffs} == {
        "energy_saving_calc.flue_gas_flow",
        "energy_saving_calc.air_flow",
    }
