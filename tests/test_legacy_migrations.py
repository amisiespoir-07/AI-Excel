"""历史 ID 翻译机制：映射表来自配置，不写死在 Python 里。

本部署已经删除了 `config/legacy_migrations.json`（部署方判断不再需要，理由见
config/README.md）。但**翻译机制本身**必须继续可用：将来若要恢复历史数据兼容，
把下面的 HISTORICAL_MAPPINGS 写回那个文件即可，代码一行都不用改。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.legacy as legacy
from app.domain import migrate_model_snapshot, normalize_legacy_parameter_values
from app.legacy import LegacyMigrations, LegacyMigrationsUnavailable, get_legacy_migrations, set_legacy_migrations

ROOT = Path(__file__).resolve().parents[1]

#: 这个部署历史上用过的 ID 映射（原先存放在 config/legacy_migrations.json）。
HISTORICAL_MAPPINGS = LegacyMigrations(
    parameter_ids={
        "heat_load_mw": "heat_load",
        "input_b11": "heat_load",
        "input_t11": "fuel_hydrogen_content",
        "input_g74": "target_exhaust_temperature",
    },
    result_ids={
        "b22": "thermal_result",
        "k63": "air_outlet_temperature",
        "s63": "flue_gas_flow",
        "u63": "air_flow",
    },
    artifact_ids={"calculation": "energy_saving_calc", "air_preheater": "air_preheater"},
    handoff_inputs={
        "from_calculation_s63": "flue_gas_flow",
        "from_calculation_u63": "air_flow",
    },
)


@pytest.fixture(autouse=True)
def restore_migrations():
    yield
    set_legacy_migrations(None)


def legacy_snapshot(parameter_id: str = "input_b11") -> dict:
    return {
        "model_id": "old-model",
        "version": 8,
        "parameters": {
            parameter_id: {"description": "Heat load", "display_name": "Heat load", "required": True},
        },
        "workbooks": [
            {
                "workbook_id": "calculation",
                "template": "thermal.xlsm",
                "inputs": {parameter_id: {"cell_ref": "Process!B11"}},
                "outputs": [{"output_id": "b22", "sheet": "Process", "cell": "B22"}],
            }
        ],
        "handoffs": [],
    }


def test_historical_legacy_ids_still_translate_through_the_mechanism():
    """映射表换成显式注入后，机制必须完全等价——配置删了不等于能力没了。"""
    set_legacy_migrations(HISTORICAL_MAPPINGS)
    migrations = get_legacy_migrations()
    assert migrations.translate_parameter("heat_load_mw") == "heat_load"
    assert migrations.translate_parameter("input_t11") == "fuel_hydrogen_content"
    assert migrations.translate_parameter("input_g74") == "target_exhaust_temperature"
    assert migrations.translate_result("s63") == "flue_gas_flow"
    assert migrations.translate_result("k63") == "air_outlet_temperature"
    assert migrations.translate_artifact("calculation") == "energy_saving_calc"
    assert migrations.translate_handoff_input("from_calculation_s63") == "flue_gas_flow"

    migrated = migrate_model_snapshot(legacy_snapshot())
    assert set(migrated["parameters"]) == {"heat_load"}
    assert migrated["bindings"]["energy_saving_calc"]["inputs"]["heat_load"]["cell"] == "B11"


def test_missing_config_means_no_migration_not_a_crash():
    """本部署就是这种状态：文件不存在 → 视为「无迁移」，不能报错。"""
    set_legacy_migrations(None)
    migrations = get_legacy_migrations()

    assert migrations.translate_parameter("input_b11") == "input_b11"
    # 但「长得像单元格的历史别名」这条识别能力必须保留——它是防回归的守卫
    assert migrations.is_legacy_input_alias("input_b11") is True


def test_mapping_can_be_redriven_purely_by_configuration():
    set_legacy_migrations(
        LegacyMigrations(
            parameter_ids={"legacy_power": "heat_load"},
            result_ids={"old_result": "thermal_result"},
            artifact_ids={"old_book": "energy_saving_calc"},
            handoff_inputs={},
        )
    )
    snapshot = {
        "model_id": "old-model",
        "version": 1,
        "parameters": {
            "legacy_power": {"description": "Power", "display_name": "Power", "required": True}
        },
        "workbooks": [
            {
                "workbook_id": "old_book",
                "template": "thermal.xlsm",
                "inputs": {"legacy_power": {"cell_ref": "Process!B11"}},
                "outputs": [{"output_id": "old_result", "sheet": "Process", "cell": "B22"}],
            }
        ],
        "handoffs": [],
    }
    migrated = migrate_model_snapshot(snapshot)
    assert set(migrated["parameters"]) == {"heat_load"}
    assert set(migrated["results"]) == {"thermal_result"}
    assert set(migrated["artifacts"]) == {"energy_saving_calc"}


def test_normalized_parameter_values_follow_the_config():
    set_legacy_migrations(LegacyMigrations(parameter_ids={"legacy_power": "heat_load"}))
    assert normalize_legacy_parameter_values({}, {"legacy_power": 240}) == {"heat_load": 240}
    # 已经带 schema_version 的快照不做翻译
    assert normalize_legacy_parameter_values(
        {"schema_version": "2.0"}, {"legacy_power": 240}
    ) == {"legacy_power": 240}


def test_no_config_file_means_no_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(
        legacy, "get_settings", lambda: SimpleNamespace(legacy_migrations=tmp_path / "absent.json")
    )
    set_legacy_migrations(None)
    migrations = get_legacy_migrations()
    assert migrations.translate_parameter("input_b11") == "input_b11"


def test_broken_config_file_fails_loudly(tmp_path, monkeypatch):
    path = tmp_path / "legacy_migrations.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(
        legacy, "get_settings", lambda: SimpleNamespace(legacy_migrations=path)
    )
    set_legacy_migrations(None)
    with pytest.raises(LegacyMigrationsUnavailable, match="无法解析"):
        get_legacy_migrations()


def test_config_with_wrong_shape_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "legacy_migrations.json"
    path.write_text(json.dumps({"parameter_ids": ["not", "a", "map"]}), encoding="utf-8")
    monkeypatch.setattr(
        legacy, "get_settings", lambda: SimpleNamespace(legacy_migrations=path)
    )
    set_legacy_migrations(None)
    with pytest.raises(LegacyMigrationsUnavailable, match="必须是对象"):
        get_legacy_migrations()


def test_invalid_alias_pattern_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "legacy_migrations.json"
    path.write_text(json.dumps({"legacy_input_alias_pattern": "(unclosed"}), encoding="utf-8")
    monkeypatch.setattr(
        legacy, "get_settings", lambda: SimpleNamespace(legacy_migrations=path)
    )
    set_legacy_migrations(None)
    with pytest.raises(LegacyMigrationsUnavailable, match="不是合法正则"):
        get_legacy_migrations()


def test_legacy_input_aliases_are_dropped_using_the_configured_pattern():
    set_legacy_migrations(
        LegacyMigrations(
            parameter_ids={"input_b11": "heat_load"},
            legacy_input_alias_pattern=r"col_[A-Z]+[0-9]+",
        )
    )
    snapshot = legacy_snapshot()
    snapshot["parameters"]["input_b11"]["aliases"] = ["col_X9", "热负荷"]
    migrated = migrate_model_snapshot(snapshot)
    assert migrated["parameters"]["heat_load"]["input"]["aliases"] == ["热负荷"]
