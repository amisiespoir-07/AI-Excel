# Excel Mapping 配置说明（schema 2.0）

## 1. 配置边界

`model.json` 将业务定义与 Excel Adapter 分层：

- `model` / `parameters` / `results`：业务模型。自然语言和校验只使用业务参数、业务结果 ID，不含 Sheet、Cell 或模板文件名。
- `artifacts`：工作流执行资源。本 MVP 使用 Excel artifact；可继续扩展 Python/C++/CSV/Word/PDF 等 type，但不代表这些执行器目前已实现。
- `bindings`：Excel Adapter 独有的 Sheet/Cell 读写映射。
- `workflow`：按依赖顺序执行 artifact。
- `handoffs`：artifact 间只传递业务 result ID。

Model Registry 与 Calculation Model Definition 分开维护：`EXCEL_MODEL_REGISTRY`（默认 `config/model_registry.json`）列出可路由模型；`EXCEL_MODEL_DEFINITIONS_ROOT`（默认 `config/models`）按 `model_id` 查找 `<model_id>/model.json`。例如 `model_energy_saving_calc` 对应 `config/models/model_energy_saving_calc/model.json`。每个启用的 Registry ID 必须有 Definition，且 Definition 的 `model.id` 必须与 Registry ID 一致。

模型定义只有一个来源：`config/models/<model_id>/model.json`——**没有旧单文件配置的兜底分支**，未注册的 ID 会直接报错，不会悄悄换成别的模型。Definition、Registry 与模板适配层的变更都按文件签名热重载；新任务仍保存配置快照。

## 2. 顶层结构

```json
{
  "schema_version": "2.0",
  "model": {},
  "parameters": {},
  "results": {},
  "artifacts": {},
  "bindings": {},
  "workflow": {},
  "handoffs": [],
  "report": {},
  "examples": [],
  "hints": []
}
```

## 3. Business Model

### `model`

- `id`：模型 ID，会话和任务绑定此 ID。
- `version`：业务配置版本。发布修改后递增。
- `name`、`description`：模型用途信息，给 UI/LLM 使用。

### `parameters`

参数使用稳定的业务 ID，例如 `heat_load`、`fuel_hydrogen_content`、`target_exhaust_temperature`。不要使用 `input_b11`、`B11` 等 Cell-derived 名称。

```json
"heat_load": {
  "display_name": "热负荷",
  "description": "工艺计算所需的热负荷。用户也可能说热负载。",
  "type": "number",
  "unit": "MW",
  "required": true,
  "input": {"aliases": ["热负荷", "热负载", "heat load"]},
  "validation": {"minimum": null, "maximum": null},
  "missing": {"policy": "error"}
}
```

- `input.aliases` 只放用户可能使用的自然语言，例如“热负载”，不能放 Excel 内部名称。
- `validation` 为后端确定性范围校验；单位换算不要交给 LLM 猜。
- `required=true` 时 `missing.policy` 必须为 `error`。
- 可选参数可用 `template_default`（缺省时不改模板值）或 `default`（必须另给非空 `value`）。
- 若改参数 ID，必须同步改 `bindings` 的输入 key。程序不需要按参数名称增加 Python 分支。上线更名时如确需兼容历史用户偏好/旧任务，应保留旧配置/旧任务快照，不能把 Excel Cell 名重新加到自然语言 alias。

### `results`

结果用业务 ID 命名，与 Excel Cell 解耦：

```json
"preheater_result_s": {
  "display_name": "空气预热器计算结果 S",
  "type": "number",
  "required": true
}
```

### `artifacts`

Artifact 是计算资源，不作为业务参数模型：

```json
"energy_saving_calc": {
  "type": "excel",
  "display_name": "工艺计算模型",
  "template": "节能减排计算.xlsm"
}
```

## 4. Excel Adapter `bindings`（本部署整段省略）

Binding key 引用 Business Model 的参数 ID 或结果 ID。Excel 工作表名和单元格地址**只允许出现在 `config/templates/<id>.json` 里**（见 4.1）。

> **随包的两个模型都没有 `bindings`。** 这不是遗漏，是刻意的：它们的**业务 ID 和槽位名完全同名**
> （`heat_load`、`flue_gas_flow`、`air_flow`、`thermal_result`…），交给加载期按同名自动接入即可。
> 代价是业务 ID 从此等于槽位名——换工作簿时若槽位命名变了，业务 ID 得跟着改。

### 4.1 模板适配层 `config/templates/<id>.json`

每个 Excel 工作簿一个文件，是这个工作簿的**唯一**单元格真相来源：

```json
{
  "id": "energy_saving_calc",
  "template": "节能减排计算.xlsm",
  "display_name": "工艺计算模型",
  "inputs": {
    "heat_load": {"sheet": "工艺计算", "cell": "B11"}
  },
  "outputs": {
    "thermal_result": {"sheet": "工艺计算", "cell": "B22"}
  }
}
```

槽位名用**中性物理量词**（烟气流量、空气流量、热负荷），不含业务角色。同一个槽位在一个模型里可能是用户参数、在另一个模型里可能是上游 handoff 的结果——这正是两本工作簿能被多个模型复用的原因。

### 4.2 模型侧引用

同一本工作簿在**两个模型里扮演不同角色**，但名字一致，所以两边都不用写映射：

```json
// model_energy_saving_calc：heat_load 等是用户参数；flue_gas_flow / air_flow 是上游结果
"artifacts": {
  "energy_saving_calc": {"type": "excel", "template_ref": "energy_saving_calc"},
  "air_preheater":       {"type": "excel", "template_ref": "air_preheater"}
}
// 没有 bindings —— 按槽位同名自动接入
```

```json
// model_air_preheater：同样两个槽位，这次是用户直接给的参数
"artifacts": {"air_preheater": {"type": "excel", "template_ref": "air_preheater"}}
// 还是没有 bindings
```

**角色由「所在模型」决定，而不是由名字决定**：`flue_gas_flow` 在串联模型里不在 `parameters` 中，
所以运行器把它当 handoff 输入去上游找；在独立模型里它是参数，就用用户给的值。
（判定逻辑见 `app/workflow.py: resolve_inputs`。）

规则：

- `template_ref` 与内联的 `template` **二选一**，Excel artifact 必须正好有一个；
- 要写 `bindings` 时，值必须是**槽位名（字符串）**，不允许再内联 `{sheet, cell}`；
  确实需要别的单元格时，请另建一个模板适配文件；
- 某个 artifact **完全不写 `bindings`** 时，按槽位名同名全量接入；
- **省略是按 artifact 整体生效的，不能按行省略**：只要声明了，那个 artifact 的每一行都要写全；
- 加载期会把适配层展开成内联写法，因此运行期、校验规则和任务快照都不感知这一层，
  旧任务快照也仍然自包含（快照里带的是展开后的 `{sheet, cell}`）。

### 4.3 改单元格 / 换模板

- 挪单元格：只改 `config/templates/<id>.json` 里那一处，所有引用它的模型同时生效。
- 换工作簿：改 `config/templates/<id>.json` 的 `template`，然后在目标 Excel/Windows/UDF 环境做兼容性验收。
- 模板块**参与热重载**：改完适配文件不必重启 API，下一个请求就会用新映射。

本 Mapping Validation 会检查：未知参数/结果、非法 Cell 地址、同 artifact 重复输入/输出 Cell、参数无输入 binding、结果无输出 binding、artifact/binding/workflow 缺失或重复、错误 handoff 和 workflow 循环、未知槽位、缺失的模板适配文件，以及「每个参数只能驱动流程或写入单元格，不能两者兼有」。

## 5. Workflow 与业务 Handoff

```json
"workflow": {
  "steps": [
    {"id":"energy_saving_calc", "type":"excel_calculation", "artifact":"energy_saving_calc", "depends_on":[]},
    {"id":"air_preheater", "type":"excel_calculation", "artifact":"air_preheater", "depends_on":["energy_saving_calc"]}
  ]
}
```

A→B 传递只引用业务结果 ID：

```json
{"source":"energy_saving_calc.preheater_result_s", "target":"air_preheater.preheater_result_s"}
```

Excel Adapter 实际落点由 Binding 决定：`energy_saving_calc.preheater_result_s` 绑定到 `预热器计算!S63`；`air_preheater.preheater_result_s` 绑定到 `空气预热器-H!AA14`。U 结果同理从 `U63` 传至 `AO14`。Worker 按 workflow dependency 顺序运行 artifact，并以 Excel 实际输出执行 handoff。

## 6. 当前模型 ID 对照

| 业务概念 | Business parameter/result ID | Excel binding |
|---|---|---|
| 热负荷 | `heat_load` | `energy_saving_calc` → `工艺计算!B11` |
| 燃料气氢气含量 | `fuel_hydrogen_content` | `energy_saving_calc` → `工艺计算!T11` |
| 目标排烟温度 | `target_exhaust_temperature` | `energy_saving_calc` → `预热器计算!G74` |
| 工艺计算结果 | `thermal_result` | `energy_saving_calc` → `工艺计算!B22` |
| 预热器结果 K/S/U | `preheater_result_k/s/u` | `energy_saving_calc` → `K63/S63/U63` |
| 结果 S handoff | `preheater_result_s` | `energy_saving_calc` → `air_preheater` → `空气预热器-H!AA14` |
| 结果 U handoff | `preheater_result_u` | `energy_saving_calc` → `air_preheater` → `空气预热器-H!AO14` |

`fuel_hydrogen_content` 和 `target_exhaust_temperature` 未提供时 `missing.policy=template_default`，Worker 在模板副本上不改对应输入格；热负荷缺失时不能确认任务。

## 7. 交付物与界面文案

### `report`

交付物不再写死在程序里：

```json
"report": {
  "enabled": true,
  "file_name": "calculation-report.docx",
  "template": null,
  "ai_summary": true,
  "pdf_preview": true,
  "title": null,
  "note": "说明：本报告中的计算数值直接读取自计算引擎的原始输出；AI 说明不改变计算结果。",
  "sections": [
    {"kind": "parameters", "title": "一、输入参数"},
    {"kind": "results", "title": "二、计算结果"},
    {"kind": "summary", "title": "三、结果说明"}
  ]
}
```

- `template` 为空用内置版式（小节由 `sections` 决定，`kind` 只能是 `parameters`/`results`/`summary`）。
- `template` 指向 `EXCEL_REPORT_TEMPLATE_ROOT`（默认 `templates/reports`）下的 `.docx`，改为占位符驱动。
  支持标量 `{{job_id}}` `{{model_name}}` `{{generated_at}}` `{{summary}}` 等，
  以及表格标记 `{{parameters_table}}` / `{{results_table}}`（写在某行第一格，该行作为行模板被复制）。
- `enabled` / `ai_summary` / `pdf_preview` 分别控制「是否出报告」「是否调用大模型写说明」「是否额外导出 PDF 预览」。

### `examples` / `hints`

界面引导文案由模型自带，前端不硬编码任何业务示例：

```json
"examples": [{"title": "卡片标题", "description": "副标题", "prompt": "点击后填入输入框的完整句子"}],
"hints": ["热负荷 200 MW", "排烟温度 85°C"]
```

两者为空时界面不显示引导区，也不回退到硬编码文案。

### `artifacts.<id>.options`

执行器的私有配置块，本层不解释其内容（例如 `{"script": "calc.py"}`），原样交给对应执行器。

### 开放的步骤类型

`workflow.steps[].type` 与 `artifacts.<id>.type` 都是**开放标识**（小写 slug），
不再限定为 `excel_calculation` / `excel`。新增类型的做法见
[扩展指南.md](../扩展指南.md) 第三节。

## 8. 上线前后兼容

- 当前配置为 schema 2.0；历史 SQLite 队列里保存的旧任务 Mapping 会在 Worker 迁移边界转换一次，再按 v2 Binding 执行。
- 业务代码的 LLM、校验、报告和工作流使用业务 ID；旧 `input_b11` 等兼容翻译已从代码里移出，
  改由 `app/legacy.py` 从**可选的** `config/legacy_migrations.json` 读取。
  该文件在本部署已被删除（见 [扩展指南.md](../扩展指南.md) 第 9 节）：库里没有还需要迁移的历史数据，
  机制仍完好，要恢复兼容把映射写回这个文件即可。文件缺失 = 无迁移；文件存在但内容非法 = 显式报错。
- 修改 Mapping 采用“临时文件写入 → JSON/Mapping Validation → 原子替换 → 新任务验收”。失败时 API 返回配置错误，不会静默使用无效映射。
- 新旧 Mapping 的实际 Excel 计算结果需用金样输入、基准结果及 Excel A→B 单元格对账验证；不能只凭 JSON 通过就认定计算验收通过。
