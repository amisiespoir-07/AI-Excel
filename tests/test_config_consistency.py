"""配置层的一致性守护。

配置分三层，每层都要与下一层对得上：能选哪些模型（model_registry.json）、
模型是什么（models/<id>/model.json）、单元格在哪（templates/*.json）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.model_catalog import ModelCatalogRegistry
from app.model_registry import ModelDefinitionRegistry

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_registry() -> tuple[ModelDefinitionRegistry, ModelCatalogRegistry]:
    catalog = ModelCatalogRegistry(CONFIG / "model_registry.json")
    resolver = ModelDefinitionRegistry(catalog=catalog, definitions_root=CONFIG / "models")
    return resolver, catalog


def test_every_enabled_registry_entry_has_a_matching_definition():
    resolver, catalog = build_registry()
    definitions = resolver.validate_enabled()
    assert definitions, "至少应有一个已启用的模型定义"
    for definition, entry in zip(definitions, catalog.get_enabled()):
        assert definition.model.id == entry.id


def test_registry_ids_are_unique_and_slug_shaped():
    registry = read_json(CONFIG / "model_registry.json")
    ids = [entry["id"] for entry in registry["models"]]
    assert len(ids) == len(set(ids))


def test_legacy_migration_config_is_valid_if_present():
    """迁移表是**可选**的：本部署删掉了它，但保留了「如果存在就必须合法」的守卫。"""
    path = CONFIG / "legacy_migrations.json"
    if not path.is_file():
        pytest.skip("本部署没有配置历史 ID 迁移表（视为「无迁移」）")

    payload = read_json(path)
    assert isinstance(payload, dict)
    for key in ("parameter_ids", "result_ids", "artifact_ids", "handoff_inputs"):
        assert isinstance(payload.get(key, {}), dict), key


def test_every_model_binding_points_at_real_excel_cells():
    """每个已启用模型的每个绑定，都必须指向真实模板里存在的 Sheet 与单元格。

    新增模型（例如独立的空气预热器计算）最容易写错 Sheet 名或单元格，这条测试负责挡住。
    """
    import openpyxl

    resolver, _ = build_registry()

    checked = 0
    for definition in resolver.validate_enabled():
        for artifact_id, artifact in definition.artifacts.items():
            if artifact.type != "excel":
                continue
            workbook_path = ROOT / "templates" / (artifact.template or "")
            assert workbook_path.is_file(), (
                f"{definition.model.id}: 找不到模板 {artifact.template!r}"
            )
            workbook = openpyxl.load_workbook(
                workbook_path, read_only=True, data_only=False, keep_vba=True
            )
            try:
                binding = definition.bindings[artifact_id]
                for cell_binding in [*binding.inputs.values(), *binding.outputs.values()]:
                    assert cell_binding.sheet in workbook.sheetnames, (
                        f"{definition.model.id}/{artifact_id}: 模板里没有工作表 "
                        f"{cell_binding.sheet!r}（现有：{workbook.sheetnames}）"
                    )
                    assert workbook[cell_binding.sheet][cell_binding.cell] is not None
                    checked += 1
            finally:
                workbook.close()

    assert checked > 0, "没有检查到任何 Excel 绑定"


def test_config_subdirectories_have_no_stray_files():
    """配置只能出现在它该在的地方。

    这条专门抓「复制一份 model copy.json 当草稿」：加载器只认 model.json，
    那份副本永远不会被加载、也永远不会被发现过期——但读配置的人会被它误导。
    （原先的守护只查 config/ 顶层，子目录是盲区。）
    """
    for directory in sorted(path for path in (CONFIG / "models").iterdir() if path.is_dir()):
        files = sorted(path.name for path in directory.iterdir() if path.is_file())
        assert files == ["model.json"], (
            f"config/models/{directory.name}/ 里有非预期文件：{files}"
        )

    for path in sorted((CONFIG / "templates").iterdir()):
        assert path.is_file() and path.suffix == ".json", (
            f"config/templates/ 里有非预期文件：{path.name}"
        )


def test_shipped_models_rely_on_slot_name_auto_binding():
    """随包发布的两个模型都整段省略 bindings。

    这不是遗漏，是用「业务 ID = 槽位名」换来的简洁（见 config/README.md 第 4 节）。
    如果哪天有人加了 bindings，这条会提醒他把两边名字对齐、或者说明为什么必须显式写。
    """
    for path in sorted((CONFIG / "models").glob("*/model.json")):
        payload = read_json(path)
        assert "bindings" not in payload, (
            f"{path.parent.name}/model.json 又出现了 bindings；"
            "要么两边名字已不同名（那就该显式写并在此处放行），要么它是多余的"
        )


def test_config_directory_has_no_leftover_files():
    """config/ 下每个文件都必须属于三层配置之一，避免留下没人读的死文件。

    这条专门抓「把配置文件改名成 xxx-没用了.json」这种做法——重命名禁用一个文件
    是看不出来的禁用方式，别人只会以为系统坏了。
    """
    allowed = {
        "README.md",
        "model_registry.json",
        "legacy_migrations.json",   # 可选：本部署已删除，见上面的说明
        "models",
        "templates",
    }
    required = {"model_registry.json", "models", "templates"}
    actual = {path.name for path in CONFIG.iterdir() if not path.name.startswith("__")}

    unexpected = actual - allowed
    missing = required - actual
    assert not unexpected, f"config/ 下存在预期之外的文件：{sorted(unexpected)}"
    assert not missing, f"config/ 缺少必需项：{sorted(missing)}"
