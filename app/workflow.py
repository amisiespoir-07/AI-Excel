"""通用工作流运行器：按模型定义的依赖顺序执行步骤，并把工作交给注册的执行器。

这里**不包含任何具体计算引擎的知识**——不认识 Excel、不认识 Word，也不认识任何业务参数名。
它只负责：

* 按 ``model.ordered_steps()`` 的拓扑顺序遍历步骤；
* 为每一步解析输入（来自用户参数，或来自上游 handoff 的业务结果）；
* 通过 ``step.type`` 从 :mod:`app.executors` 取执行器并调用；
* 收集业务结果与产物，并在结束（无论成败）时释放执行器占用的资源。

新增计算/文件类型不需要改动本模块，参见 :mod:`app.executors` 的说明。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

# 导入即注册内建执行器（excel_calculation 等）。
from . import builtin_executors  # noqa: F401
from .domain import ModelDefinition
from .executors import (
    ExecutionError,
    StepOutcome,
    StepRequest,
    UnsupportedArtifactType,
    WorkflowContext,
    get_executor,
)


def resolve_step_inputs(
    model: ModelDefinition,
    artifact_id: str,
    parameters: dict[str, Any],
    known_results: dict[str, Any],
) -> dict[str, Any]:
    """解析一个步骤实际要写入的输入值（业务 ID -> 值）。

    * 参数输入：用户给了就用用户的值；没给且策略是 ``template_default`` 就**不出现**在结果里
      （执行器据此保留模板原值）；策略是 ``default`` 就用配置默认值；否则报缺参。
    * handoff 输入：取上游产出的业务结果；上游还没产出就报错，绝不静默用空值。
    """
    binding = model.bindings[artifact_id]
    handoffs = {item.target: item.source for item in model.handoffs}
    input_values: dict[str, Any] = {}
    for input_id in binding.inputs:
        if input_id in model.parameters:
            parameter = model.parameters[input_id]
            if input_id in parameters:
                input_values[input_id] = parameters[input_id]
            elif parameter.missing.policy == "template_default":
                continue
            elif parameter.missing.policy == "default":
                input_values[input_id] = parameter.missing.value
            else:
                raise ExecutionError("MISSING_PARAMETER", f"缺少必填参数 {input_id}")
            continue

        reference = f"{artifact_id}.{input_id}"
        source = handoffs.get(reference)
        source_result = source.rsplit(".", 1)[-1] if source else None
        if not source_result or source_result not in known_results:
            raise ExecutionError("MISSING_HANDOFF", f"未找到业务结果 handoff：{reference}")
        input_values[input_id] = known_results[source_result]
    return input_values


def execute_model(
    model: ModelDefinition,
    parameters: dict[str, Any],
    template_root: Path,
    artifact_directory: Path,
    private_workbook_directory: Path,
    on_stage: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """按模型定义执行整条工作流，返回 (业务结果, 待登记产物)。

    公开产物只写 ``artifact_directory``；可编辑中间文件只写私有目录，两者不得嵌套。
    """
    artifact_directory = Path(artifact_directory).resolve()
    private_directory = Path(private_workbook_directory).resolve()
    try:
        private_directory.relative_to(artifact_directory)
    except ValueError:
        pass
    else:
        raise ExecutionError(
            "PRIVATE_WORKBOOK_PATH_EXPOSED",
            "私有工作目录不能位于用户产物目录中。",
        )
    artifact_directory.mkdir(parents=True, exist_ok=True)
    private_directory.mkdir(parents=True, exist_ok=True)

    context = WorkflowContext()
    all_results: dict[str, Any] = {}
    artifacts: list[dict[str, Any]] = []
    try:
        for step in model.ordered_steps():
            executor = get_executor(step.type)
            artifact_definition = model.artifacts[step.artifact]
            if artifact_definition.type not in executor.artifact_types:
                raise UnsupportedArtifactType(
                    step.type, artifact_definition.type, executor.artifact_types
                )
            if not model.step_applies(step, parameters):
                # 条件不满足就跳过这一步（例如用户说“不用做空气预热器”）。
                # 仍然上报一个阶段名，让前端看得出它是被跳过的，而不是悄悄没跑。
                if on_stage is not None:
                    on_stage(f"{step.id}_skipped")
                continue
            if on_stage is not None:
                on_stage(f"{step.id}_running")

            request = StepRequest(
                model=model,
                step=step,
                artifact_id=step.artifact,
                artifact=artifact_definition,
                binding=model.bindings[step.artifact],
                input_values=resolve_step_inputs(
                    model, step.artifact, parameters, all_results
                ),
                parameters=parameters,
                known_results=dict(all_results),
                template_root=Path(template_root),
                artifact_directory=artifact_directory,
                private_directory=private_directory,
                context=context,
                on_stage=on_stage,
            )
            outcome = executor.run(request)
            if not isinstance(outcome, StepOutcome):
                raise ExecutionError(
                    "EXECUTOR_CONTRACT_VIOLATION",
                    f"执行器 {step.type!r} 必须返回 StepOutcome，实际返回 {type(outcome).__name__}",
                )
            all_results.update(outcome.results)
            artifacts.extend(outcome.artifacts)
        return all_results, artifacts
    finally:
        context.cleanup()


__all__ = ["execute_model", "resolve_step_inputs"]
