from __future__ import annotations

import json
import math
import re
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from .legacy import get_legacy_migrations


CELL_REF = re.compile(r"^\$?[A-Z]{1,3}\$?[1-9][0-9]*$", re.IGNORECASE)
EXCEL_DERIVED_ID = re.compile(r"^(?:input_)?[A-Z]{1,3}[0-9]+$", re.IGNORECASE)
#: 计算模型 ID（注册表条目 / config/models/<id>/ 目录名 / model.id）的格式。
#: 下划线与连字符都允许，和 TEMPLATE_REF_PATTERN 保持一致——
#: 三套 ID 约定不一致时，改个名字就会在启动自检里炸掉。
MODEL_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
#: 执行器标识（工作流步骤类型 / artifact 类型）的通用格式。
#: 取值是**开放**的：新增一种计算或文件类型不需要改动本模块。
ENGINE_TYPE_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
#: 模板适配文件（config/templates/<id>.json）的 ID 格式。
TEMPLATE_REF_PATTERN = r"^[a-z][a-z0-9_-]{1,63}$"

#: 步骤类型 -> 它必须引用的 artifact 类型。用于配置加载期的快速反馈。
#: 运行期的权威判断在 app/executors.py 的 StepExecutor.artifact_types，
#: 两者的一致性由测试守护。
STEP_ARTIFACT_TYPES: dict[str, str] = {"excel_calculation": "excel"}


def register_step_artifact_type(step_type: str, artifact_type: str) -> None:
    """供执行器在导入时登记「某步骤类型必须引用某 artifact 类型」。"""
    STEP_ARTIFACT_TYPES[step_type] = artifact_type


# 历史 ID 翻译表不在这里：它属于「历史数据兼容」，已挪到
# config/legacy_migrations.json，由 app/legacy.py 读取。


class CalculationIntent(str, Enum):
    CALCULATE = "calculate"
    CLARIFICATION_REQUIRED = "clarification_required"


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: CalculationIntent
    model_id: StrictStr | None = None
    candidate_models: list[StrictStr] = Field(default_factory=list)
    clarification: StrictStr | None = None

    @model_validator(mode="after")
    def validate_route(self) -> "ModelRoute":
        if self.intent == CalculationIntent.CALCULATE:
            if not self.model_id:
                raise ValueError("calculate routes require model_id")
            if not MODEL_ID.fullmatch(self.model_id):
                raise ValueError("model_id must be a registered model identifier")
            if self.candidate_models or self.clarification is not None:
                raise ValueError("calculate routes cannot include clarification fields")
        else:
            if self.model_id is not None:
                raise ValueError("clarification routes cannot select model_id")
            if not self.clarification or not self.clarification.strip():
                raise ValueError("clarification routes require clarification text")
            if any(not MODEL_ID.fullmatch(model_id) for model_id in self.candidate_models):
                raise ValueError("candidate_models must contain registered model identifiers")
        return self


class ParameterExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    parameters: dict[str, Any]
    clarification: StrictStr | None = None


class ModelRegistryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: StrictStr
    name: StrictStr
    description: StrictStr
    keywords: list[StrictStr] = Field(default_factory=list)
    category: StrictStr
    enabled: StrictBool = True

    @model_validator(mode="after")
    def validate_id(self) -> "ModelRegistryEntry":
        if not MODEL_ID.fullmatch(self.id):
            raise ValueError("registry model id must be a slug")
        return self


class ModelRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    models: list[ModelRegistryEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ModelRegistry":
        ids = [model.id for model in self.models]
        if len(ids) != len(set(ids)):
            raise ValueError("model registry contains duplicate model IDs")
        return self


class ModelInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = Field(ge=1)
    name: str
    description: str = ""


class ModelExample(BaseModel):
    """给界面用的示例提问卡片（纯文案，不参与计算）。

    界面不再硬编码任何业务示例：每个模型自带自己的引导文案，加模型/改参数
    不需要动前端代码。
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str = ""
    prompt: str


class ParameterInputDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aliases: list[str] = Field(default_factory=list)


class ParameterValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def validate_range(self) -> "ParameterValidation":
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("validation.minimum cannot exceed validation.maximum")
        return self


class MissingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Literal["error", "template_default", "default"]
    value: Any = None

    @model_validator(mode="after")
    def validate_value(self) -> "MissingPolicy":
        if self.policy == "default" and self.value is None:
            raise ValueError("missing.value is required when missing.policy is 'default'")
        if self.policy != "default" and self.value is not None:
            raise ValueError("missing.value is only valid when missing.policy is 'default'")
        return self


class ParameterDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    description: str
    type: Literal["number", "integer", "string", "boolean"] = "number"
    unit: str | None = None
    required: bool
    input: ParameterInputDefinition = Field(default_factory=ParameterInputDefinition)
    validation: ParameterValidation = Field(default_factory=ParameterValidation)
    missing: MissingPolicy

    @model_validator(mode="after")
    def validate_missing_policy(self) -> "ParameterDefinition":
        if self.required != (self.missing.policy == "error"):
            raise ValueError(
                "required must be true exactly when missing.policy is 'error'; "
                "optional parameters must use template_default or default"
            )
        return self

    @property
    def aliases(self) -> list[str]:
        return self.input.aliases

    @property
    def minimum(self) -> float | None:
        return self.validation.minimum

    @property
    def maximum(self) -> float | None:
        return self.validation.maximum

    @property
    def default(self) -> Any:
        return self.missing.value if self.missing.policy == "default" else None


class ResultDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    type: Literal["number", "integer", "string", "boolean"] = "number"
    unit: str | None = None
    required: bool = True


class ArtifactDefinition(BaseModel):
    """工作流执行资源。

    ``type`` 是开放标识（``excel`` / ``python`` / ``csv`` …），由对应的执行器解释；
    ``options`` 是留给执行器的自由配置块，本模块不解释它的内容。

    Excel artifact 有两种写法，二选一：

    * ``template``：内联写法，模板文件名 + 在 ``bindings`` 里直接写 Sheet/Cell；
    * ``template_ref``：引用 ``config/templates/<id>.json`` 里的**模板适配层**，
      Sheet/Cell 只在那个文件里出现一次，``bindings`` 改写为「业务 ID -> 槽位名」。
      加载时会被展开成内联写法，所以运行期与校验规则完全不变。
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=ENGINE_TYPE_PATTERN)
    display_name: str | None = None
    template: str | None = None
    template_ref: str | None = Field(default=None, pattern=TEMPLATE_REF_PATTERN)
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_template(self) -> "ArtifactDefinition":
        if self.type == "excel" and bool(self.template) == bool(self.template_ref):
            raise ValueError(
                "Excel artifacts need exactly one of 'template' (inline cells) "
                "or 'template_ref' (template catalog)"
            )
        return self


class ExcelCellBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet: str
    cell: str

    @model_validator(mode="after")
    def validate_cell(self) -> "ExcelCellBinding":
        if not self.sheet.strip():
            raise ValueError("Excel binding sheet cannot be empty")
        if not CELL_REF.fullmatch(self.cell.strip()):
            raise ValueError(f"invalid Excel cell reference: {self.cell}")
        return self

    @property
    def cell_ref(self) -> str:
        return f"{self.sheet}!{self.cell}"


class ExcelArtifactBindings(BaseModel):
    """Excel adapter 专属绑定：只有这里出现 Sheet 与 Cell。"""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, ExcelCellBinding] = Field(default_factory=dict)
    outputs: dict[str, ExcelCellBinding] = Field(default_factory=dict)


class GenericArtifactBindings(BaseModel):
    """非 Excel 执行器的绑定。

    形状由执行器自行约定（核心流程只把 ``inputs`` 的键当作业务 ID 引用，原样把整个对象
    交给执行器）。执行器需要更严格的形状时，可以注册自己的绑定模型。
    """

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)


#: artifact 类型 -> 绑定模型。执行器可在导入时注册自己的绑定模型。
ARTIFACT_BINDING_MODELS: dict[str, type[BaseModel]] = {"excel": ExcelArtifactBindings}


def register_artifact_binding_model(artifact_type: str, model: type[BaseModel]) -> None:
    ARTIFACT_BINDING_MODELS[artifact_type] = model


def binding_model_for(artifact_type: str) -> type[BaseModel]:
    return ARTIFACT_BINDING_MODELS.get(artifact_type, GenericArtifactBindings)


#: 向后兼容别名。
ArtifactBindings = ExcelArtifactBindings


class TemplateCatalogEntry(BaseModel):
    """一个 Excel 工作簿的**适配层**：这个工作簿在哪里读写什么。

    它只描述物理位置，不含任何业务语义（参数名、别名、校验、必填、缺省策略都在模型里）。
    槽位名是中性词（例如 ``flue_gas_flow``），同一个槽位在一个模型里可能是用户参数，
    在另一个模型里可能是上游 handoff 的结果——这正是两个模型复用同一个工作簿所需要的能力。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=TEMPLATE_REF_PATTERN)
    template: str
    display_name: str | None = None
    inputs: dict[str, ExcelCellBinding] = Field(default_factory=dict)
    outputs: dict[str, ExcelCellBinding] = Field(default_factory=dict)


def default_template_catalog_root() -> Path:
    from .config import get_settings

    return get_settings().template_catalog_root


def load_template_catalog_entry(path: Path) -> TemplateCatalogEntry:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle, object_pairs_hook=_reject_duplicate_json_keys)
    entry = TemplateCatalogEntry.model_validate(payload)
    if entry.id != path.stem:
        raise ValueError(
            f"template catalog ID {entry.id!r} must match its file name {path.stem!r}"
        )
    return entry


def template_catalog_signature(templates_root: Path) -> tuple:
    """模板适配层目录的签名。

    模型定义的缓存键必须把它算进去——否则改了一个单元格映射，
    却因为模型文件本身没变而继续用旧定义，那是最容易踩的坑。
    """
    try:
        paths = sorted(Path(templates_root).glob("*.json"))
    except OSError:
        return ()
    signature = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def compose_template_bindings(payload: dict[str, Any], templates_root: Path) -> dict[str, Any]:
    """把 ``template_ref`` 引用的模板适配层展开成内联的 ``template`` + Sheet/Cell 绑定。

    展开结果与「内联写法」完全等价，所以运行期、校验和任务快照都不需要任何改动。
    模型仍然通过 ``bindings`` 声明自己用哪些槽位，只是把 ``{sheet, cell}`` 换成槽位名；
    如果某个 artifact 完全没有声明 bindings，就按槽位名同名全量接入。
    """
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        return payload

    templates_root = Path(templates_root)
    payload.setdefault("bindings", {})
    for artifact_id, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            continue
        reference = artifact.get("template_ref")
        if not reference:
            continue

        catalog_path = templates_root / f"{reference}.json"
        if not catalog_path.is_file():
            raise ValueError(
                f"artifact {artifact_id!r} references template {reference!r}, "
                f"but {catalog_path} does not exist"
            )
        try:
            entry = load_template_catalog_entry(catalog_path)
        except Exception as exc:
            raise ValueError(
                f"template catalog {catalog_path.name} cannot be loaded: {exc}"
            ) from exc

        artifact["template"] = entry.template
        # 展开后不再保留引用：这样校验后的模型（以及任务快照）只带内联信息，
        # 不依赖 config/templates，重新校验快照也不会撞上「两种写法互斥」的规则。
        artifact.pop("template_ref", None)
        if not artifact.get("display_name") and entry.display_name:
            artifact["display_name"] = entry.display_name

        declared = payload["bindings"].get(artifact_id)
        if declared is None:
            # 没声明就用全部槽位（槽位名即业务 ID）
            payload["bindings"][artifact_id] = {
                "inputs": {name: cell.model_dump(mode="json") for name, cell in entry.inputs.items()},
                "outputs": {name: cell.model_dump(mode="json") for name, cell in entry.outputs.items()},
            }
            continue
        if not isinstance(declared, dict):
            raise ValueError(f"binding {artifact_id!r} must be an object")

        for direction, slots in (("inputs", entry.inputs), ("outputs", entry.outputs)):
            resolved: dict[str, Any] = {}
            for business_id, slot in (declared.get(direction) or {}).items():
                if not isinstance(slot, str):
                    raise ValueError(
                        f"binding {artifact_id!r}.{direction}.{business_id!r} must be a slot name "
                        f"of template {reference!r}; Sheet/Cell only belong in {catalog_path.name}. "
                        "If this model really needs different cells, add a separate template file."
                    )
                if slot not in slots:
                    raise ValueError(
                        f"binding {artifact_id!r}.{direction}.{business_id!r} references "
                        f"unknown slot {slot!r} of template {reference!r}; "
                        f"available: {sorted(slots)}"
                    )
                resolved[business_id] = slots[slot].model_dump(mode="json")
            declared[direction] = resolved

    return payload


class StepCondition(BaseModel):
    """步骤执行条件：某个参数等于某个值时才执行这一步。

    被引用的参数是「控制参数」——它不写入任何单元格，只驱动流程。
    这样「默认串联、用户可以说不用」就能纯粹靠配置表达。
    """

    model_config = ConfigDict(extra="forbid")

    parameter: str
    equals: Any = True


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    #: 步骤类型，对应某个已注册执行器的 ``step_type``。
    #: 这里是开放取值——新增执行器不需要改动本模块。
    type: str = Field(pattern=ENGINE_TYPE_PATTERN)
    artifact: str
    depends_on: list[str] = Field(default_factory=list)
    #: 可选执行条件；不满足时运行器跳过这一步。
    when: StepCondition | None = None


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[WorkflowStep] = Field(min_length=1)


class Handoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str


class ReportSection(BaseModel):
    """内置报告版式中的一个小节。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["parameters", "results", "summary"]
    title: str


def _default_report_sections() -> list["ReportSection"]:
    return [
        ReportSection(kind="parameters", title="一、输入参数"),
        ReportSection(kind="results", title="二、计算结果"),
        ReportSection(kind="summary", title="三、结果说明"),
    ]


class ReportDefinition(BaseModel):
    """报告交付配置。

    ``template`` 为空时使用内置版式（小节顺序由 ``sections`` 决定）；
    指定 Word 模板后改为占位符驱动，此时 ``sections`` 被忽略。
    """

    model_config = ConfigDict(extra="forbid")

    #: 关闭后不生成 Word 报告，也不登记 word 产物
    enabled: bool = True
    #: 报告产物文件名
    file_name: str = "calculation-report.docx"
    #: Word 模板路径（相对报告模板根目录），支持 {{占位符}} 与表格标记
    template: str | None = None
    #: 是否调用大模型生成结果说明；关闭时使用本地固定说明
    ai_summary: bool = True
    #: 是否额外导出 PDF 预览（需要 Microsoft Word）
    pdf_preview: bool = True
    #: 报告标题，默认「<模型名>计算报告」
    title: str | None = None
    #: 报告结尾的固定说明
    note: str = "说明：本报告中的计算数值直接读取自计算引擎的原始输出；AI 说明不改变计算结果。"
    #: 内置版式的小节顺序（仅在未指定 template 时生效）
    sections: list[ReportSection] = Field(default_factory=_default_report_sections)


class ModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"]
    model: ModelInfo
    parameters: dict[str, ParameterDefinition]
    results: dict[str, ResultDefinition]
    artifacts: dict[str, ArtifactDefinition]
    #: 每条绑定在加载时按对应 artifact 的类型规范化成具体绑定模型
    #: （Excel 走 ExcelArtifactBindings，其它走执行器注册的模型）。
    bindings: dict[str, Any]
    workflow: WorkflowDefinition
    handoffs: list[Handoff] = Field(default_factory=list)
    report: ReportDefinition = Field(default_factory=ReportDefinition)
    #: 界面引导卡片；为空时界面不显示示例区
    examples: list[ModelExample] = Field(default_factory=list)
    #: 界面上的短语提示（例如「热负荷 200 MW」）；为空时不显示
    hints: list[str] = Field(default_factory=list)

    @property
    def model_id(self) -> str:
        """Compatibility accessor for sessions and persisted jobs."""
        return self.model.id

    @property
    def version(self) -> int:
        return self.model.version

    @property
    def description(self) -> str:
        return self.model.description

    def canonical_parameter_id(self, name: str) -> str | None:
        if name in self.parameters:
            return name
        for parameter_id, definition in self.parameters.items():
            if name in definition.aliases:
                return parameter_id
        return None

    @property
    def bound_parameter_ids(self) -> set[str]:
        """真正会被写入某个 artifact 输入的参数。"""
        bound: set[str] = set()
        for binding in self.bindings.values():
            for input_id in binding.inputs:
                if input_id in self.parameters:
                    bound.add(input_id)
        return bound

    @property
    def control_parameter_ids(self) -> set[str]:
        """只驱动流程、不写入任何单元格的参数（被步骤条件引用）。"""
        return set(self.parameters) - self.bound_parameter_ids

    def step_applies(self, step: "WorkflowStep", parameters: dict[str, Any]) -> bool:
        """这一步在当前参数下是否应该执行。

        条件参数缺失时回退到配置里的默认值——否则在本次改动之前入队的旧任务快照
        （里面没有这个新参数）会被**静默跳过**，那是很危险的默认行为。
        """
        condition = step.when
        if condition is None:
            return True
        if condition.parameter in parameters:
            return parameters[condition.parameter] == condition.equals
        spec = self.parameters.get(condition.parameter)
        fallback = (
            spec.missing.value
            if spec is not None and spec.missing.policy == "default"
            else None
        )
        return fallback == condition.equals

    def ordered_steps(self) -> list[WorkflowStep]:
        by_id = {step.id: step for step in self.workflow.steps}
        visited: set[str] = set()
        visiting: set[str] = set()
        ordered: list[WorkflowStep] = []

        def visit(step_id: str) -> None:
            if step_id in visiting:
                raise ValueError(f"workflow dependency cycle at {step_id!r}")
            if step_id in visited:
                return
            visiting.add(step_id)
            step = by_id[step_id]
            for dependency in step.depends_on:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)
            ordered.append(step)

        for step in self.workflow.steps:
            visit(step.id)
        return ordered

    @model_validator(mode="after")
    def validate_mapping(self) -> "ModelDefinition":
        # 按 artifact 类型把每条绑定规范化成对应的绑定模型，
        # 这样核心流程与各执行器都能拿到有结构的对象，而不是裸 dict。
        normalized_bindings: dict[str, Any] = {}
        for artifact_id, raw_binding in self.bindings.items():
            artifact = self.artifacts.get(artifact_id)
            if artifact is None:
                # 保持原样，交给后面的 bindings/artifacts 一致性检查报出更准确的错误。
                normalized_bindings[artifact_id] = raw_binding
                continue
            try:
                normalized_bindings[artifact_id] = binding_model_for(artifact.type).model_validate(
                    raw_binding
                )
            except Exception as exc:
                raise ValueError(
                    f"binding {artifact_id!r} is not valid for artifact type {artifact.type!r}: {exc}"
                ) from exc
        self.bindings = normalized_bindings

        parameter_names: dict[str, str] = {}
        for parameter_id, parameter in self.parameters.items():
            if EXCEL_DERIVED_ID.fullmatch(parameter_id):
                raise ValueError(f"business parameter ID must not expose an Excel cell: {parameter_id!r}")
            if parameter.missing.policy == "default":
                validate_parameters(self, {parameter_id: parameter.missing.value}, require_all=False)
            for name in [parameter_id, *parameter.aliases]:
                if not name or not name.strip():
                    raise ValueError(f"parameter aliases cannot be empty: {parameter_id}")
                if name != parameter_id and EXCEL_DERIVED_ID.fullmatch(name):
                    raise ValueError(f"parameter alias must not expose an Excel cell: {name!r}")
                previous = parameter_names.get(name)
                if previous is not None and previous != parameter_id:
                    raise ValueError(
                        f"parameter name/alias {name!r} is declared by both {previous!r} and {parameter_id!r}"
                    )
                parameter_names[name] = parameter_id

        if len(self.workflow.steps) != len({step.id for step in self.workflow.steps}):
            raise ValueError("workflow step IDs must be unique")
        steps_by_id = {step.id: step for step in self.workflow.steps}
        artifacts_in_workflow: dict[str, WorkflowStep] = {}
        for step in self.workflow.steps:
            if step.artifact not in self.artifacts:
                raise ValueError(f"workflow step {step.id!r} references unknown artifact {step.artifact!r}")
            if step.artifact in artifacts_in_workflow:
                raise ValueError(f"artifact {step.artifact!r} is used by multiple workflow steps")
            artifacts_in_workflow[step.artifact] = step
            required_artifact_type = STEP_ARTIFACT_TYPES.get(step.type)
            if (
                required_artifact_type is not None
                and self.artifacts[step.artifact].type != required_artifact_type
            ):
                raise ValueError(
                    f"step {step.id!r} of type {step.type!r} requires an artifact of type "
                    f"{required_artifact_type!r}, got {self.artifacts[step.artifact].type!r}"
                )
            for dependency in step.depends_on:
                if dependency not in steps_by_id:
                    raise ValueError(f"workflow step {step.id!r} depends on unknown step {dependency!r}")
                if dependency == step.id:
                    raise ValueError(f"workflow step {step.id!r} cannot depend on itself")
            if step.when is not None:
                condition_parameter = step.when.parameter
                if condition_parameter not in self.parameters:
                    raise ValueError(
                        f"workflow step {step.id!r} has a condition on unknown parameter "
                        f"{condition_parameter!r}"
                    )
                if condition_parameter in self.bound_parameter_ids:
                    raise ValueError(
                        f"condition parameter {condition_parameter!r} is also written to a cell; "
                        "a parameter drives the workflow or feeds a workbook, not both"
                    )

        if set(self.bindings) != set(self.artifacts):
            missing = sorted(set(self.artifacts) - set(self.bindings))
            extra = sorted(set(self.bindings) - set(self.artifacts))
            raise ValueError(f"bindings/artifacts mismatch; missing={missing}, extra={extra}")
        if set(artifacts_in_workflow) != set(self.artifacts):
            raise ValueError("every configured artifact must be used by exactly one workflow step")

        produced: dict[str, str] = {}
        bound_parameters: set[str] = set()
        for artifact_id, binding in self.bindings.items():
            if self.artifacts[artifact_id].type != "excel":
                # 非 Excel 执行器自行解释绑定形状（Sheet/Cell 规则不适用），
                # 但「引用的业务 ID 必须存在」「每个参数/结果都必须被消费」的规则同样适用。
                for input_id in binding.inputs:
                    if input_id not in self.parameters and input_id not in self.results:
                        raise ValueError(
                            f"binding {artifact_id!r} references unknown parameter/result input {input_id!r}"
                        )
                    if input_id in self.parameters:
                        bound_parameters.add(input_id)
                for result_id in binding.outputs:
                    if EXCEL_DERIVED_ID.fullmatch(result_id):
                        raise ValueError(
                            f"business result ID must not expose an Excel cell: {result_id!r}"
                        )
                    if result_id not in self.results:
                        raise ValueError(
                            f"binding {artifact_id!r} references unknown result {result_id!r}"
                        )
                    if result_id in produced:
                        raise ValueError(f"result {result_id!r} is produced by multiple artifacts")
                    produced[result_id] = artifact_id
                continue
            input_cells = [(v.sheet.casefold(), v.cell.replace("$", "").upper()) for v in binding.inputs.values()]
            if len(input_cells) != len(set(input_cells)):
                raise ValueError(f"artifact {artifact_id!r} maps multiple input IDs to the same Excel cell")
            output_cells = [(v.sheet.casefold(), v.cell.replace("$", "").upper()) for v in binding.outputs.values()]
            if len(output_cells) != len(set(output_cells)):
                raise ValueError(f"artifact {artifact_id!r} maps multiple result IDs from the same Excel cell")
            for input_id in binding.inputs:
                if input_id not in self.parameters and input_id not in self.results:
                    raise ValueError(f"binding {artifact_id!r} references unknown parameter/result input {input_id!r}")
                if input_id in self.parameters:
                    bound_parameters.add(input_id)
            for result_id in binding.outputs:
                if EXCEL_DERIVED_ID.fullmatch(result_id):
                    raise ValueError(f"business result ID must not expose an Excel cell: {result_id!r}")
                if result_id not in self.results:
                    raise ValueError(f"binding {artifact_id!r} references unknown result {result_id!r}")
                if result_id in produced:
                    raise ValueError(f"result {result_id!r} is produced by multiple artifacts")
                produced[result_id] = artifact_id

        if set(produced) != set(self.results):
            missing_results = sorted(set(self.results) - set(produced))
            raise ValueError(f"business results without an output binding: {missing_results}")

        # 可跳过的步骤不能产出必需结果：跳过之后任务就会缺少必需数据。
        for step in self.workflow.steps:
            if step.when is None:
                continue
            for result_id in self.bindings[step.artifact].outputs:
                if self.results[result_id].required:
                    raise ValueError(
                        f"workflow step {step.id!r} is conditional but produces required result "
                        f"{result_id!r}; mark it optional or drop the condition"
                    )

        # 控制参数只驱动流程、不写入单元格；其余参数必须都有输入绑定。
        unbound_parameters = set(self.parameters) - bound_parameters
        condition_parameters = {
            step.when.parameter for step in self.workflow.steps if step.when is not None
        }
        if unbound_parameters - condition_parameters:
            raise ValueError(
                "business parameters without an input binding: "
                f"{sorted(unbound_parameters - condition_parameters)}"
            )

        step_order = {step.id: index for index, step in enumerate(self.ordered_steps())}
        artifact_step = {step.artifact: step for step in self.workflow.steps}
        handoff_targets: set[str] = set()
        for handoff in self.handoffs:
            source_artifact, sep_source, source_result = handoff.source.rpartition(".")
            target_artifact, sep_target, target_input = handoff.target.rpartition(".")
            if not sep_source or not sep_target:
                raise ValueError("handoff source/target must use artifact_id.business_result_id format")
            if source_artifact not in self.artifacts or target_artifact not in self.artifacts:
                raise ValueError(f"handoff {handoff.source!r} -> {handoff.target!r} references unknown artifact")
            if source_result not in self.results:
                raise ValueError(f"handoff source references unknown result {source_result!r}")
            if self.bindings[source_artifact].outputs.get(source_result) is None:
                raise ValueError(f"handoff source {handoff.source!r} is not bound as an output")
            if self.bindings[target_artifact].inputs.get(target_input) is None:
                raise ValueError(f"handoff target {handoff.target!r} is not bound as an input")
            if target_input in self.parameters:
                raise ValueError(f"handoff target {handoff.target!r} conflicts with a user parameter")
            if target_input not in self.results:
                raise ValueError(f"handoff target references unknown business result {target_input!r}")
            if handoff.target in handoff_targets:
                raise ValueError(f"handoff target {handoff.target!r} is mapped more than once")
            handoff_targets.add(handoff.target)

            source_step = artifact_step[source_artifact]
            target_step = artifact_step[target_artifact]
            if step_order[source_step.id] >= step_order[target_step.id]:
                raise ValueError(f"handoff source artifact {source_artifact!r} must run before {target_artifact!r}")
            if source_step.id not in target_step.depends_on:
                raise ValueError(f"workflow step {target_step.id!r} must directly depend on {source_step.id!r}")

        for artifact_id, binding in self.bindings.items():
            for input_id in binding.inputs:
                if input_id in self.parameters:
                    continue
                if f"{artifact_id}.{input_id}" not in handoff_targets:
                    raise ValueError(f"input {artifact_id}.{input_id} is not mapped by a handoff")

        return self


def _legacy_mapping_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """One-time adapter for already queued v1 task snapshots and older config files."""
    if "schema_version" in payload:
        return payload
    if "model_id" not in payload or "workbooks" not in payload:
        return payload

    # 历史 ID 翻译表来自 config/legacy_migrations.json，不在代码里写死。
    migrations = get_legacy_migrations()

    parameters: dict[str, Any] = {}
    parameter_id_map: dict[str, str] = {}
    for old_id, spec in payload.get("parameters", {}).items():
        new_id = migrations.translate_parameter(old_id)
        parameter_id_map[old_id] = new_id
        old_aliases = [
            alias for alias in spec.get("aliases", [])
            if alias not in migrations.parameter_ids and not migrations.is_legacy_input_alias(alias)
        ]
        missing_policy = "error" if spec.get("required", True) else (
            "default" if spec.get("default") is not None else "template_default"
        )
        definition = {
            "display_name": spec.get("display_name") or spec.get("description", old_id).split("。", 1)[0],
            "description": spec.get("description", ""),
            "type": spec.get("type", "number"),
            "unit": spec.get("unit"),
            "required": spec.get("required", True),
            "input": {"aliases": old_aliases},
            "validation": {"minimum": spec.get("minimum"), "maximum": spec.get("maximum")},
            "missing": {"policy": missing_policy},
        }
        if missing_policy == "default":
            definition["missing"]["value"] = spec["default"]
        parameters[new_id] = definition

    artifacts: dict[str, Any] = {}
    bindings: dict[str, Any] = {}
    results: dict[str, Any] = {}
    artifact_id_map: dict[str, str] = {}
    result_id_map: dict[str, str] = {}
    legacy_workbooks = payload.get("workbooks", [])
    for workbook in legacy_workbooks:
        artifact_id = migrations.translate_artifact(workbook["workbook_id"])
        artifact_id_map[workbook["workbook_id"]] = artifact_id
        artifacts[artifact_id] = {"type": "excel", "template": workbook["template"]}
        inputs: dict[str, Any] = {}
        raw_inputs = workbook.get("inputs", {})
        if isinstance(raw_inputs, list):
            for legacy_input in raw_inputs:
                old_input_id = legacy_input["input_id"]
                old_parameter_id = legacy_input.get("parameter")
                input_id = (
                    parameter_id_map.get(old_parameter_id, old_parameter_id)
                    if old_parameter_id else migrations.translate_handoff_input(old_input_id)
                )
                item = {"cell_ref": f"{legacy_input['sheet']}!{legacy_input['cell']}"}
                inputs[input_id] = item
        elif isinstance(raw_inputs, dict):
            for old_input_id, legacy_input in raw_inputs.items():
                input_id = parameter_id_map.get(old_input_id, old_input_id)
                if old_input_id in migrations.handoff_inputs:
                    input_id = migrations.handoff_inputs[old_input_id]
                else:
                    input_id = migrations.translate_handoff_input(input_id)
                if "cell_ref" in legacy_input:
                    sheet, separator, cell = legacy_input["cell_ref"].rpartition("!")
                    if not separator:
                        raise ValueError(f"invalid legacy cell_ref {legacy_input['cell_ref']!r}")
                    inputs[input_id] = {"sheet": sheet, "cell": cell}
                else:
                    inputs[input_id] = {
                        "cell_ref": f"{legacy_input['sheet']}!{legacy_input['cell']}"
                    }
                    if "missing_value_policy" in legacy_input:
                        inputs[input_id]["missing_value_policy"] = legacy_input["missing_value_policy"]
        outputs: dict[str, Any] = {}
        for legacy_output in workbook.get("outputs", []):
            old_result_id = legacy_output["output_id"]
            result_id = migrations.translate_result(old_result_id)
            result_id_map[f"{workbook['workbook_id']}.{old_result_id}"] = f"{artifact_id}.{result_id}"
            results.setdefault(result_id, {
                "display_name": result_id.replace("_", " ").title(),
                "type": "number",
                "unit": legacy_output.get("unit"),
                "required": legacy_output.get("required", True),
            })
            outputs[result_id] = {"sheet": legacy_output["sheet"], "cell": legacy_output["cell"]}
        bindings[artifact_id] = {"inputs": inputs, "outputs": outputs}

    for legacy_handoff in payload.get("handoffs", []):
        target_artifact_old, _, target_input_old = legacy_handoff["target"].rpartition(".")
        target_artifact = artifact_id_map.get(target_artifact_old, target_artifact_old)
        target_input = migrations.translate_handoff_input(target_input_old)
        results.setdefault(target_input, {
            "display_name": target_input.replace("_", " ").title(),
            "type": "number",
            "unit": None,
            "required": True,
        })
        if target_input_old in bindings.get(target_artifact, {}).get("inputs", {}):
            bindings[target_artifact]["inputs"][target_input] = bindings[target_artifact]["inputs"].pop(target_input_old)

    dependencies_by_artifact: dict[str, set[str]] = {
        artifact_id: set() for artifact_id in artifacts
    }
    for legacy_handoff in payload.get("handoffs", []):
        source_artifact_old, _, _ = legacy_handoff["source"].rpartition(".")
        target_artifact_old, _, _ = legacy_handoff["target"].rpartition(".")
        source_artifact = artifact_id_map.get(source_artifact_old, source_artifact_old)
        target_artifact = artifact_id_map.get(target_artifact_old, target_artifact_old)
        if source_artifact in dependencies_by_artifact and target_artifact in dependencies_by_artifact:
            dependencies_by_artifact[target_artifact].add(source_artifact)

    workflow_steps: list[dict[str, Any]] = []
    for workbook in legacy_workbooks:
        old_artifact_id = workbook["workbook_id"]
        artifact_id = artifact_id_map[old_artifact_id]
        workflow_steps.append({
            "id": artifact_id,
            "type": "excel_calculation",
            "artifact": artifact_id,
            "depends_on": sorted(dependencies_by_artifact[artifact_id]),
        })

    handoffs: list[dict[str, str]] = []
    for legacy_handoff in payload.get("handoffs", []):
        source = result_id_map.get(legacy_handoff["source"], legacy_handoff["source"])
        target_artifact_old, _, target_input_old = legacy_handoff["target"].rpartition(".")
        target_artifact = artifact_id_map.get(target_artifact_old, target_artifact_old)
        target_input = migrations.translate_handoff_input(target_input_old)
        handoffs.append({"source": source, "target": f"{target_artifact}.{target_input}"})

    return {
        "schema_version": "2.0",
        "model": {
            "id": payload["model_id"],
            "version": payload.get("version", 1),
            "name": payload.get("description", payload["model_id"]),
            "description": payload.get("description", ""),
        },
        "parameters": parameters,
        "results": results,
        "artifacts": artifacts,
        "bindings": bindings,
        "workflow": {"steps": workflow_steps},
        "handoffs": handoffs,
    }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_model_definition(path: Path, templates_root: Path | None = None) -> ModelDefinition:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle, object_pairs_hook=_reject_duplicate_json_keys)
    payload = _legacy_mapping_to_v2(payload)
    payload = compose_template_bindings(
        payload,
        templates_root if templates_root is not None else default_template_catalog_root(),
    )
    return ModelDefinition.model_validate(payload)


def migrate_model_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    return _legacy_mapping_to_v2(payload)


def normalize_legacy_parameter_values(payload: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """Map old task parameter IDs to v2 business IDs for queued-job compatibility."""
    if "schema_version" in payload:
        return values
    migrations = get_legacy_migrations()
    return {
        migrations.translate_parameter(key): value
        for key, value in values.items()
    }


def validate_parameters(
    model: ModelDefinition,
    values: dict[str, Any],
    *,
    require_all: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    normalized: dict[str, Any] = {}
    canonical_inputs = {key for key in values if model.canonical_parameter_id(key) == key}
    for key, value in values.items():
        parameter_id = model.canonical_parameter_id(key)
        if parameter_id is None:
            continue
        if parameter_id in canonical_inputs and key != parameter_id:
            continue
        normalized[parameter_id] = value

    unknown = sorted(set(values) - {
        key for key in values if model.canonical_parameter_id(key) is not None
    })
    if unknown:
        raise ValueError(f"未知业务参数: {', '.join(unknown)}")

    validated: dict[str, Any] = {}
    for key, value in normalized.items():
        spec = model.parameters[key]
        if spec.type in ("number", "integer"):
            if isinstance(value, bool):
                raise ValueError(f"参数 {key} 必须是{ '整数' if spec.type == 'integer' else '数字'}")
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"参数 {key} 必须是有效数字") from exc
            if not math.isfinite(number):
                raise ValueError(f"参数 {key} 必须是有限数值")
            if spec.type == "integer" and not number.is_integer():
                raise ValueError(f"参数 {key} 必须是整数")
            if spec.type == "integer":
                number = int(number)
            if spec.minimum is not None and number < spec.minimum:
                raise ValueError(f"参数 {key} 不能小于 {spec.minimum}")
            if spec.maximum is not None and number > spec.maximum:
                raise ValueError(f"参数 {key} 不能大于 {spec.maximum}")
            validated[key] = number
        elif spec.type == "string":
            if not isinstance(value, str):
                raise ValueError(f"参数 {key} 必须是文本")
            validated[key] = value
        elif spec.type == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"参数 {key} 必须是布尔值")
            validated[key] = value

    missing = [key for key, spec in model.parameters.items()
               if spec.missing.policy == "error" and key not in validated] if require_all else []
    return validated, missing


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return str(value)
