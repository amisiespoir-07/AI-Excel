"""模板适配层：每个 Excel 工作簿一个文件，Sheet/Cell 只出现一次。

业务动机：`空气预热器工艺计算程序.xlsm` 的文件名与 `AA14/AO14` 两个单元格，
原来在两个模型文件里各写了一遍。现在它们只在 config/templates/<id>.json 里出现，
模型文件只写「业务 ID -> 槽位名」。

这些测试同时守住了这次拆分的**核心承诺**：模型文件里不许再出现 Sheet/Cell。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.domain import (
    ModelDefinition,
    compose_template_bindings,
    load_model_definition,
    load_template_catalog_entry,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "templates"
MODELS = ROOT / "config" / "models"


def model_paths() -> list[Path]:
    """所有模型定义文件——现在只有 config/models/<id>/model.json 一处来源。"""
    return sorted(MODELS.glob("*/model.json"))


# --------------------------------------------------------------------------- #
# 目录本身
# --------------------------------------------------------------------------- #

def test_catalog_ids_match_their_file_names_and_workbooks_exist():
    entries = sorted(CATALOG.glob("*.json"))
    assert entries, "至少应该有一个模板适配文件"
    for path in entries:
        entry = load_template_catalog_entry(path)
        assert entry.id == path.stem
        workbook = ROOT / "templates" / entry.template
        assert workbook.is_file(), f"{path.name} 指向的模板不存在：{workbook}"


def test_catalog_id_mismatch_is_rejected(tmp_path):
    path = tmp_path / "wrong-name.json"
    path.write_text(
        json.dumps({"id": "other-id", "template": "x.xlsm"}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must match its file name"):
        load_template_catalog_entry(path)


def test_catalog_rejects_invalid_cell_reference(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {"id": "bad", "template": "x.xlsm", "inputs": {"a": {"sheet": "S", "cell": "0"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_template_catalog_entry(path)


# --------------------------------------------------------------------------- #
# 展开
# --------------------------------------------------------------------------- #

def test_models_resolve_bindings_through_the_catalog():
    model = load_model_definition(ROOT / "config" / "models" / "model_air_preheater" / "model.json")
    assert model.artifacts["air_preheater"].template == "空气预热器工艺计算程序.xlsm"
    assert model.bindings["air_preheater"].inputs["flue_gas_flow"].cell_ref == "空气预热器-H!AA14"
    assert model.bindings["air_preheater"].inputs["air_flow"].cell_ref == "空气预热器-H!AO14"


def test_business_ids_may_differ_from_slot_names():
    """同一个槽位在不同模型里可以叫不同业务名——这正是两个模型能复用一本工作簿的原因。"""
    chained = load_model_definition(MODELS / "model_energy_saving_calc" / "model.json")
    standalone = load_model_definition(MODELS / "model_air_preheater" / "model.json")

    chained_cell = chained.bindings["air_preheater"].inputs["flue_gas_flow"]
    standalone_cell = standalone.bindings["air_preheater"].inputs["flue_gas_flow"]
    assert chained_cell.cell_ref == standalone_cell.cell_ref == "空气预热器-H!AA14"


def test_composed_snapshot_round_trips_without_the_catalog():
    """任务快照必须自包含：展开后不再依赖 config/templates。"""
    model = load_model_definition(MODELS / "model_energy_saving_calc" / "model.json")
    payload = model.model_dump(mode="json")
    for artifact in payload["artifacts"].values():
        assert artifact.get("template"), "快照里必须有模板文件名"
        assert not artifact.get("template_ref"), "快照不应该再持有模板目录引用"
    ModelDefinition.model_validate(payload)  # 重新校验不需要模板目录


def test_missing_catalog_file_is_reported():
    payload = {
        "artifacts": {"x": {"type": "excel", "template_ref": "does-not-exist"}},
        "bindings": {},
    }
    with pytest.raises(ValueError, match="does not exist"):
        compose_template_bindings(payload, CATALOG)


def test_unknown_slot_is_reported(tmp_path):
    payload = {
        "artifacts": {"x": {"type": "excel", "template_ref": "air_preheater"}},
        "bindings": {"x": {"inputs": {"p": "no_such_slot"}, "outputs": {}}},
    }
    with pytest.raises(ValueError, match="unknown slot"):
        compose_template_bindings(payload, CATALOG)


def test_inline_cells_are_rejected_for_referenced_templates():
    """用了 template_ref 就不许再内联写 Sheet/Cell——否则「只出现一次」的承诺就破了。"""
    payload = {
        "artifacts": {"x": {"type": "excel", "template_ref": "air_preheater"}},
        "bindings": {"x": {"inputs": {"p": {"sheet": "空气预热器-H", "cell": "AA14"}}}},
    }
    with pytest.raises(ValueError, match="Sheet/Cell only belong in"):
        compose_template_bindings(payload, CATALOG)


def test_excel_artifact_needs_exactly_one_of_template_or_ref():
    base = {
        "schema_version": "2.0",
        "model": {"id": "t", "version": 1, "name": "t"},
        "parameters": {},
        "results": {},
        "bindings": {"a": {"inputs": {}, "outputs": {}}},
        "workflow": {"steps": [{"id": "a", "type": "excel_calculation", "artifact": "a"}]},
    }
    with pytest.raises(ValueError, match="exactly one of"):
        ModelDefinition.model_validate({**base, "artifacts": {"a": {"type": "excel"}}})
    with pytest.raises(ValueError, match="exactly one of"):
        ModelDefinition.model_validate(
            {
                **base,
                "artifacts": {
                    "a": {"type": "excel", "template": "x.xlsm", "template_ref": "air_preheater"}
                },
            }
        )


# --------------------------------------------------------------------------- #
# 核心承诺：模型文件里不再出现 Sheet/Cell
# --------------------------------------------------------------------------- #

def test_model_files_never_inline_excel_cells():
    """凡是走 template_ref 的 artifact，模型文件里不许出现任何 {sheet, cell}。"""
    offenders: list[str] = []
    for path in model_paths():
        payload = json.loads(path.read_text(encoding="utf-8"))
        for artifact_id, artifact in payload.get("artifacts", {}).items():
            if not artifact.get("template_ref"):
                continue
            if artifact.get("template"):
                offenders.append(f"{path.name}:{artifact_id} 同时写了 template 与 template_ref")
            for direction in ("inputs", "outputs"):
                for business_id, slot in (
                    payload.get("bindings", {}).get(artifact_id, {}).get(direction) or {}
                ).items():
                    if not isinstance(slot, str):
                        offenders.append(f"{path.name}:{artifact_id}.{direction}.{business_id}")
    assert offenders == [], f"模型文件里仍然内联了 Sheet/Cell：{offenders}"


def test_excel_cells_live_only_in_the_template_catalog():
    """每个模型的每个绑定，单元格来源都应该是某个模板适配文件，而不是模型自己。"""
    settings = get_settings()
    catalog_cells: set[tuple[str, str, str]] = set()
    for path in CATALOG.glob("*.json"):
        entry = load_template_catalog_entry(path)
        for cell in [*entry.inputs.values(), *entry.outputs.values()]:
            catalog_cells.add((entry.template, cell.sheet, cell.cell))

    for path in model_paths():
        model = load_model_definition(path, settings.template_catalog_root)
        for artifact_id, binding in model.bindings.items():
            template = model.artifacts[artifact_id].template
            for cell in [*binding.inputs.values(), *binding.outputs.values()]:
                assert (template, cell.sheet, cell.cell) in catalog_cells, (
                    f"{path.name}:{artifact_id} 的 {cell.cell_ref} 不在任何模板适配文件里"
                )
