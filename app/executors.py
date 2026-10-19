"""执行器注册表：把「工作流步骤类型」与「具体执行方式」解耦。

扩展一种新的步骤类型（Python 脚本、CSV 导出、外部程序、其它文件格式……）只需要：

1. 写一个模块，实现 :class:`StepExecutor` 协议（``step_type``、``artifact_types``、``run``）；
2. 在模块导入时调用 :func:`register_executor`；
3. 在 ``app/builtin_executors.py`` 里 import 该模块，让它随程序启动自动注册。

``app/workflow.py``（执行编排）与 ``app/worker.py``（任务循环）都不需要任何改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .domain import ArtifactDefinition, ModelDefinition, WorkflowStep


class ExecutionError(RuntimeError):
    """带错误码的执行错误。Worker 会把 ``code`` 原样写进任务记录。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class UnsupportedStepType(ExecutionError):
    def __init__(self, step_type: str, registered: tuple[str, ...] = ()):
        detail = f"已注册的步骤类型：{', '.join(registered)}" if registered else "当前没有注册任何步骤类型"
        super().__init__(
            "UNSUPPORTED_STEP_TYPE",
            f"没有为步骤类型 {step_type!r} 注册执行器。{detail}",
        )


class UnsupportedArtifactType(ExecutionError):
    def __init__(self, step_type: str, artifact_type: str, accepted: tuple[str, ...]):
        super().__init__(
            "UNSUPPORTED_ARTIFACT_TYPE",
            f"步骤类型 {step_type!r} 不支持 artifact 类型 {artifact_type!r}"
            f"（只接受：{', '.join(sorted(accepted))}）",
        )


@dataclass
class WorkflowContext:
    """一次工作流执行期间共享的上下文。

    执行器可以在 ``resources`` 里缓存昂贵资源（例如 Excel 进程），并通过
    ``add_cleanup`` 注册收尾动作；无论成功还是失败，运行器都会按注册顺序的逆序执行清理。
    """

    resources: dict[str, Any] = field(default_factory=dict)
    _cleanups: list[Callable[[], None]] = field(default_factory=list)

    def add_cleanup(self, cleanup: Callable[[], None]) -> None:
        self._cleanups.append(cleanup)

    def cleanup(self) -> None:
        while self._cleanups:
            cleanup = self._cleanups.pop()
            try:
                cleanup()
            except Exception:  # 清理失败不应掩盖真正的错误
                pass


@dataclass(frozen=True)
class StepRequest:
    """交给执行器的一次步骤执行请求。

    ``input_values`` 由工作流运行器解析完毕：来自用户参数，或来自上游 handoff 的业务结果，
    键都是业务 ID。执行器不接触配置文件，也不接触自然语言。
    """

    model: ModelDefinition
    step: WorkflowStep
    artifact_id: str
    artifact: ArtifactDefinition
    binding: Any
    input_values: dict[str, Any]
    parameters: dict[str, Any]
    known_results: dict[str, Any]
    template_root: Path
    artifact_directory: Path
    private_directory: Path
    context: WorkflowContext
    on_stage: Callable[[str], None] | None = None

    def stage(self, name: str) -> None:
        if self.on_stage is not None:
            self.on_stage(name)


@dataclass
class StepOutcome:
    """一次步骤执行的产出：新的业务结果，以及要登记的产物。"""

    results: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class StepExecutor(Protocol):
    """步骤执行器协议。"""

    #: 与 ``workflow.steps[].type`` 对应的唯一标识
    step_type: str
    #: 该执行器能处理的 artifact 类型集合
    artifact_types: frozenset[str]

    def run(self, request: StepRequest) -> StepOutcome:
        """执行一步，返回业务结果与产物。失败时抛出 :class:`ExecutionError`。"""


_EXECUTORS: dict[str, StepExecutor] = {}


def register_executor(executor: StepExecutor) -> StepExecutor:
    """注册一个执行器。重复注册同一个实例是幂等的。"""
    step_type = getattr(executor, "step_type", None)
    if not isinstance(step_type, str) or not step_type.strip():
        raise ValueError("执行器必须声明非空的 step_type")
    artifact_types = getattr(executor, "artifact_types", None)
    if not artifact_types:
        raise ValueError(f"执行器 {step_type!r} 必须声明非空的 artifact_types")
    existing = _EXECUTORS.get(step_type)
    if existing is not None and existing is not executor:
        raise ValueError(
            f"步骤类型 {step_type!r} 已由 {type(existing).__name__} 注册，"
            f"不能再由 {type(executor).__name__} 注册"
        )
    _EXECUTORS[step_type] = executor
    return executor


def get_executor(step_type: str) -> StepExecutor:
    executor = _EXECUTORS.get(step_type)
    if executor is None:
        raise UnsupportedStepType(step_type, registered_step_types())
    return executor


def registered_step_types() -> tuple[str, ...]:
    return tuple(sorted(_EXECUTORS))


def registered_executors() -> tuple[StepExecutor, ...]:
    return tuple(_EXECUTORS[key] for key in sorted(_EXECUTORS))


def unregister_executor(step_type: str) -> None:
    """注销一个执行器（供测试与热插拔场景使用）。"""
    _EXECUTORS.pop(step_type, None)


def clear_executors() -> None:
    """仅供测试使用：清空注册表。"""
    _EXECUTORS.clear()
