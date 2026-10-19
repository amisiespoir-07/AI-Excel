"""守护「执行器插件化」这条扩展缝。

本文件要证明的核心事实：**新增一种计算/文件类型时，只需要写一个执行器模块并注册它**
（再在 app/builtin_executors.py 加一行 import），``app/workflow.py`` 与 ``app/worker.py``
完全不需要改动。下面的测试用自定义执行器走完整流程，全程只使用公开的注册接口。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import builtin_executors  # noqa: F401  确保内建执行器已注册
from app.domain import ModelDefinition, STEP_ARTIFACT_TYPES
from app.executors import (
    ExecutionError,
    StepOutcome,
    UnsupportedArtifactType,
    UnsupportedStepType,
    get_executor,
    register_executor,
    registered_step_types,
    unregister_executor,
)
from app.workflow import execute_model

CUSTOM_STEP_TYPE = "python_calculation"
CUSTOM_ARTIFACT_TYPE = "python"


def custom_model_payload(
    *,
    step_type: str = CUSTOM_STEP_TYPE,
    artifact_type: str = CUSTOM_ARTIFACT_TYPE,
    options: dict | None = None,
) -> dict:
    """一个完全不含 Excel 的两步模型：第一步算 doubled，handoff 给第二步算 quadrupled。"""
    return {
        "schema_version": "2.0",
        "model": {"id": "custom-step-test", "version": 1, "name": "自定义步骤测试"},
        "parameters": {
            "amount": {
                "display_name": "数量",
                "description": "参与计算的业务数量。",
                "type": "number",
                "required": True,
                "input": {"aliases": ["数量"]},
                "validation": {"minimum": None, "maximum": None},
                "missing": {"policy": "error"},
            }
        },
        "results": {
            "doubled": {"display_name": "两倍值", "type": "number", "required": True},
            "quadrupled": {"display_name": "四倍值", "type": "number", "required": True},
        },
        "artifacts": {
            "first": {"type": artifact_type, "display_name": "第一步", "options": options or {}},
            "second": {"type": artifact_type, "display_name": "第二步"},
        },
        "bindings": {
            "first": {"inputs": {"amount": "amount"}, "outputs": {"doubled": "doubled"}},
            "second": {"inputs": {"doubled": "doubled"}, "outputs": {"quadrupled": "quadrupled"}},
        },
        "workflow": {
            "steps": [
                {"id": "first", "type": step_type, "artifact": "first", "depends_on": []},
                {"id": "second", "type": step_type, "artifact": "second", "depends_on": ["first"]},
            ]
        },
        "handoffs": [{"source": "first.doubled", "target": "second.doubled"}],
    }


class RecordingExecutor:
    """一个最小的自定义执行器：只认识业务 ID，不碰任何文件格式。"""

    step_type = CUSTOM_STEP_TYPE
    artifact_types = frozenset({CUSTOM_ARTIFACT_TYPE})

    def __init__(self, resource_key: str = "shared-resource"):
        self.requests = []
        self.resource_key = resource_key

    def run(self, request):
        self.requests.append(request)
        # 证明上下文资源在同一条工作流的各步骤之间共享
        request.context.resources.setdefault(self.resource_key, f"opened-by-{request.step.id}")
        amount = request.input_values.get("amount")
        if amount is not None:
            results = {"doubled": amount * 2}
        else:
            results = {"quadrupled": request.input_values["doubled"] * 2}
        return StepOutcome(
            results=results,
            artifacts=[
                {
                    "kind": "csv",
                    "file_name": f"{request.step.id}.csv",
                    "file_path": str(request.artifact_directory / f"{request.step.id}.csv"),
                    "size_bytes": 0,
                }
            ],
        )


@pytest.fixture
def recording_executor():
    executor = RecordingExecutor()
    register_executor(executor)
    try:
        yield executor
    finally:
        unregister_executor(CUSTOM_STEP_TYPE)


def test_excel_executor_is_registered_and_agrees_with_domain_declaration():
    excel = get_executor("excel_calculation")
    assert excel.artifact_types == frozenset({"excel"})
    # domain 在加载期给出的「步骤类型 -> artifact 类型」约束必须与执行器一致，
    # 否则加载期反馈会与运行期行为互相矛盾。
    for step_type, artifact_type in STEP_ARTIFACT_TYPES.items():
        assert artifact_type in get_executor(step_type).artifact_types, step_type
    assert "excel_calculation" in registered_step_types()


def test_custom_executor_runs_a_two_step_workflow_without_core_changes(tmp_path, recording_executor):
    model = ModelDefinition.model_validate(custom_model_payload())
    stages = []

    results, artifacts = execute_model(
        model,
        {"amount": 5},
        template_root=tmp_path / "templates",
        artifact_directory=tmp_path / "artifacts",
        private_workbook_directory=tmp_path / "private",
        on_stage=stages.append,
    )

    assert results == {"doubled": 10.0, "quadrupled": 20.0}
    assert [request.step.id for request in recording_executor.requests] == ["first", "second"]
    first, second = recording_executor.requests
    # 输入解析是核心流程提供的：第一步拿用户参数，第二步拿上游 handoff 的业务结果
    assert first.input_values == {"amount": 5}
    assert second.input_values == {"doubled": 10.0}
    assert second.known_results == {"doubled": 10.0}
    # 同一条工作流共享上下文资源
    assert second.context.resources["shared-resource"] == "opened-by-first"
    # 产物被原样收集
    assert [item["file_name"] for item in artifacts] == ["first.csv", "second.csv"]
    assert stages == ["first_running", "second_running"]


def test_workflow_cleans_up_executor_resources_even_when_a_step_fails(tmp_path):
    cleaned = []

    class FailingExecutor:
        step_type = "failing_calculation"
        artifact_types = frozenset({"python"})

        def run(self, request):
            request.context.add_cleanup(lambda: cleaned.append(request.step.id))
            raise ExecutionError("BOOM", "故意失败")

    register_executor(FailingExecutor())
    try:
        model = ModelDefinition.model_validate(
            custom_model_payload(step_type="failing_calculation")
        )
        with pytest.raises(ExecutionError, match="故意失败"):
            execute_model(
                model,
                {"amount": 1},
                template_root=tmp_path / "templates",
                artifact_directory=tmp_path / "artifacts",
                private_workbook_directory=tmp_path / "private",
            )
        assert cleaned == ["first"]
    finally:
        unregister_executor("failing_calculation")


def test_unregistered_step_type_fails_with_actionable_code(tmp_path):
    model = ModelDefinition.model_validate(custom_model_payload(step_type="not_registered"))
    with pytest.raises(UnsupportedStepType) as excinfo:
        execute_model(
            model,
            {"amount": 1},
            template_root=tmp_path / "templates",
            artifact_directory=tmp_path / "artifacts",
            private_workbook_directory=tmp_path / "private",
        )
    assert excinfo.value.code == "UNSUPPORTED_STEP_TYPE"
    assert "not_registered" in str(excinfo.value)


def test_executor_rejecting_artifact_type_reports_supported_types(tmp_path, recording_executor):
    model = ModelDefinition.model_validate(custom_model_payload(artifact_type="csv"))
    with pytest.raises(UnsupportedArtifactType) as excinfo:
        execute_model(
            model,
            {"amount": 1},
            template_root=tmp_path / "templates",
            artifact_directory=tmp_path / "artifacts",
            private_workbook_directory=tmp_path / "private",
        )
    assert excinfo.value.code == "UNSUPPORTED_ARTIFACT_TYPE"
    assert "csv" in str(excinfo.value)


def test_private_directory_must_not_live_inside_artifact_directory(tmp_path, recording_executor):
    model = ModelDefinition.model_validate(custom_model_payload())
    artifact_directory = tmp_path / "artifacts"
    with pytest.raises(ExecutionError) as excinfo:
        execute_model(
            model,
            {"amount": 1},
            template_root=tmp_path / "templates",
            artifact_directory=artifact_directory,
            private_workbook_directory=artifact_directory / "private",
        )
    assert excinfo.value.code == "PRIVATE_WORKBOOK_PATH_EXPOSED"


def test_executor_options_are_passed_through_untouched(tmp_path, recording_executor):
    model = ModelDefinition.model_validate(
        custom_model_payload(options={"script": "calc.py", "timeout_seconds": 30})
    )
    execute_model(
        model,
        {"amount": 1},
        template_root=tmp_path / "templates",
        artifact_directory=tmp_path / "artifacts",
        private_workbook_directory=tmp_path / "private",
    )
    # 核心流程不认识 options 的内容，只原样交给执行器
    assert recording_executor.requests[0].artifact.options == {
        "script": "calc.py",
        "timeout_seconds": 30,
    }


def test_duplicate_step_type_registration_is_rejected(recording_executor):
    with pytest.raises(ValueError, match="已由"):
        register_executor(RecordingExecutor())


def test_binding_shape_is_enforced_per_artifact_type():
    # Excel artifact 仍然只能使用 Sheet/Cell 绑定
    payload = custom_model_payload()
    payload["artifacts"]["first"] = {"type": "excel", "template": "x.xlsm"}
    with pytest.raises(ValueError, match="not valid for artifact type 'excel'"):
        ModelDefinition.model_validate(payload)


def test_unknown_business_reference_is_rejected_for_generic_bindings():
    payload = custom_model_payload()
    payload["bindings"]["first"]["inputs"] = {"not_a_parameter": "not_a_parameter"}
    with pytest.raises(ValueError, match="unknown parameter/result input"):
        ModelDefinition.model_validate(payload)


def test_artifact_directory_defaults_are_created(tmp_path, recording_executor):
    model = ModelDefinition.model_validate(custom_model_payload())
    artifact_directory = tmp_path / "deep" / "artifacts"
    private_directory = tmp_path / "deep" / "private"
    execute_model(
        model,
        {"amount": 3},
        template_root=tmp_path / "templates",
        artifact_directory=artifact_directory,
        private_workbook_directory=private_directory,
    )
    assert artifact_directory.is_dir()
    assert private_directory.is_dir()
    assert not list(Path(artifact_directory).glob("*.xlsm"))


def test_output_binding_cannot_reference_unknown_result():
    payload = custom_model_payload()
    payload["bindings"]["second"]["outputs"] = {"doubled": "doubled"}
    with pytest.raises(ValueError, match="unknown result|produced by multiple artifacts"):
        ModelDefinition.model_validate(payload)
