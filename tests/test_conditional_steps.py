"""可选步骤与独立调用。

业务背景：`节能减排计算.xlsm` 与 `空气预热器工艺计算程序.xlsm` 原来被写死成一串两步流程。
现在两者可以各自独立调用，而「算节能减排时默认继续算空气预热器、但用户可以说不用」
纯粹由配置表达（控制参数 + 步骤条件），核心代码不认识任何业务名词。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import executors as executors_module
from app.domain import ModelDefinition, load_model_definition
from app.executors import StepOutcome, register_executor, unregister_executor
from app.excel_executor import ExcelCalculationExecutor
from app.workflow import execute_model

ROOT = Path(__file__).resolve().parents[1]
CHAINED_CONFIG = ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json"
STANDALONE_CONFIG = ROOT / "config" / "models" / "model_air_preheater" / "model.json"


def chained_payload() -> dict:
    """已展开的模型 payload（模板适配层已经解析成 Sheet/Cell）。

    ``ModelDefinition.model_validate`` 只接受展开后的写法，所以这里从加载结果反推 payload，
    而不是直接拿磁盘上那份用了槽位名的原始文件。
    """
    return load_model_definition(CHAINED_CONFIG).model_dump(mode="json")


class RecordingExcelExecutor:
    """替代真实 Excel 执行器：只记录哪些步骤真的被跑了。"""

    step_type = "excel_calculation"
    artifact_types = frozenset({"excel"})

    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        # 按 artifact 声明产出结果，这样下游 handoff 才有值可取
        return StepOutcome(
            results={result_id: 1.0 for result_id in request.binding.outputs}, artifacts=[]
        )


@pytest.fixture
def recording_executor():
    recorder = RecordingExcelExecutor()
    unregister_executor("excel_calculation")
    register_executor(recorder)
    try:
        yield recorder
    finally:
        unregister_executor("excel_calculation")
        register_executor(ExcelCalculationExecutor())


def run(model, parameters, tmp_path, executor):
    stages = []
    results, artifacts = execute_model(
        model,
        parameters,
        template_root=tmp_path / "templates",
        artifact_directory=tmp_path / "artifacts",
        private_workbook_directory=tmp_path / "private",
        on_stage=stages.append,
    )
    assert artifacts == []
    return [request.step.artifact for request in executor.requests], stages


# --------------------------------------------------------------------------- #
# 配置层面
# --------------------------------------------------------------------------- #

def test_chained_model_declares_the_second_step_as_conditional():
    model = load_model_definition(CHAINED_CONFIG)
    assert model.control_parameter_ids == {"include_air_preheater"}
    condition = next(s for s in model.workflow.steps if s.artifact == "air_preheater").when
    assert condition is not None
    assert condition.parameter == "include_air_preheater"
    assert condition.equals is True
    # 控制参数的默认值就是「继续算」——这正是“默认串联”的实现方式
    assert model.parameters["include_air_preheater"].missing.value is True


def test_standalone_model_is_a_single_separate_step():
    model = load_model_definition(STANDALONE_CONFIG)
    assert model.model.id == "model_air_preheater"
    assert [step.artifact for step in model.ordered_steps()] == ["air_preheater"]
    assert model.handoffs == []
    assert model.control_parameter_ids == set()
    # 两个输入都是必填的用户参数，不再是上游 handoff
    assert set(model.parameters) == {"flue_gas_flow", "air_flow"}
    assert all(spec.missing.policy == "error" for spec in model.parameters.values())


def test_control_parameter_must_not_be_written_to_a_cell():
    payload = chained_payload()
    payload["bindings"]["energy_saving_calc"]["inputs"]["include_air_preheater"] = {
        "sheet": "工艺计算", "cell": "C20"
    }
    with pytest.raises(ValueError, match="drives the workflow or feeds a workbook"):
        ModelDefinition.model_validate(payload)


def test_condition_must_reference_a_known_parameter():
    payload = chained_payload()
    payload["workflow"]["steps"][1]["when"] = {"parameter": "not_a_parameter", "equals": True}
    with pytest.raises(ValueError, match="unknown parameter"):
        ModelDefinition.model_validate(payload)


def test_unbound_parameter_that_is_not_a_condition_is_still_rejected():
    payload = chained_payload()
    del payload["workflow"]["steps"][1]["when"]
    with pytest.raises(ValueError, match="without an input binding"):
        ModelDefinition.model_validate(payload)


def test_conditional_step_may_not_produce_a_required_result():
    """可跳过的步骤产出必需结果 → 跳过之后任务就会缺数据，必须在校验期拦下。"""
    payload = {
        "schema_version": "2.0",
        "model": {"id": "conditional-result-test", "version": 1, "name": "t"},
        "parameters": {
            "amount": {"display_name": "数量", "description": "d", "type": "number",
                       "required": True, "missing": {"policy": "error"}},
            "run_step_two": {"display_name": "开关", "description": "d", "type": "boolean",
                             "required": False, "missing": {"policy": "default", "value": True}},
        },
        "results": {
            "first_result": {"display_name": "r1", "type": "number"},
            "second_result": {"display_name": "r2", "type": "number"},
        },
        "artifacts": {
            "first": {"type": "excel", "template": "a.xlsm"},
            "second": {"type": "excel", "template": "b.xlsm"},
        },
        "bindings": {
            "first": {"inputs": {"amount": {"sheet": "S", "cell": "A1"}},
                      "outputs": {"first_result": {"sheet": "S", "cell": "A2"}}},
            "second": {"inputs": {"first_result": {"sheet": "S", "cell": "B1"}},
                       "outputs": {"second_result": {"sheet": "S", "cell": "B2"}}},
        },
        "workflow": {"steps": [
            {"id": "first", "type": "excel_calculation", "artifact": "first", "depends_on": []},
            {"id": "second", "type": "excel_calculation", "artifact": "second",
             "depends_on": ["first"], "when": {"parameter": "run_step_two", "equals": True}},
        ]},
        "handoffs": [{"source": "first.first_result", "target": "second.first_result"}],
    }
    with pytest.raises(ValueError, match="conditional but produces required result"):
        ModelDefinition.model_validate(payload)

    # 把它标成可选结果就应该通过
    payload["results"]["second_result"]["required"] = False
    ModelDefinition.model_validate(payload)


# --------------------------------------------------------------------------- #
# 运行层面
# --------------------------------------------------------------------------- #

def test_default_run_executes_both_workbooks(tmp_path, recording_executor):
    model = load_model_definition(CHAINED_CONFIG)
    artifacts, stages = run(
        model, {"heat_load": 240, "include_air_preheater": True}, tmp_path, recording_executor
    )
    assert artifacts == ["energy_saving_calc", "air_preheater"]
    assert stages == ["energy_saving_calc_running", "air_preheater_running"]


def test_saying_no_skips_the_second_workbook(tmp_path, recording_executor):
    model = load_model_definition(CHAINED_CONFIG)
    artifacts, stages = run(
        model, {"heat_load": 240, "include_air_preheater": False}, tmp_path, recording_executor
    )
    assert artifacts == ["energy_saving_calc"]
    # 跳过也要上报阶段名，前端能看出它是被跳过的而不是悄悄没跑
    assert stages == ["energy_saving_calc_running", "air_preheater_skipped"]


def test_legacy_snapshot_without_the_control_parameter_still_runs_both(tmp_path, recording_executor):
    """本次改动之前入队的旧任务快照里没有这个参数，不能因此被静默跳过第二步。"""
    model = load_model_definition(CHAINED_CONFIG)
    artifacts, stages = run(model, {"heat_load": 240}, tmp_path, recording_executor)
    assert artifacts == ["energy_saving_calc", "air_preheater"]
    assert stages == ["energy_saving_calc_running", "air_preheater_running"]


def test_standalone_model_runs_only_the_air_preheater(tmp_path, recording_executor):
    model = load_model_definition(STANDALONE_CONFIG)
    artifacts, stages = run(
        model, {"flue_gas_flow": 50036, "air_flow": 47082}, tmp_path, recording_executor
    )
    assert artifacts == ["air_preheater"]
    assert stages == ["air_preheater_running"]


# --------------------------------------------------------------------------- #
# 接口层面
# --------------------------------------------------------------------------- #

def test_api_exposes_control_flag_and_step_condition():
    from fastapi.testclient import TestClient

    from app.api import app

    with TestClient(app) as client:
        models = {item["model_id"]: item for item in client.get("/api/v1/models").json()}

    chained = models["model_energy_saving_calc"]
    assert chained["parameters"]["include_air_preheater"]["control"] is True
    assert chained["parameters"]["heat_load"]["control"] is False
    step = next(s for s in chained["workflow"]["steps"] if s["artifact"] == "air_preheater")
    assert step["when"] == {"parameter": "include_air_preheater", "equals": True}

    standalone = models["model_air_preheater"]
    assert standalone["results"] == {}
    assert [s["artifact"] for s in standalone["workflow"]["steps"]] == ["air_preheater"]
    assert all(not spec["control"] for spec in standalone["parameters"].values())
