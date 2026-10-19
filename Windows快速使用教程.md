# Windows 快速使用教程

本教程带你在 Windows 上启动 API 和 Microsoft Excel Worker，并通过自然语言请求完成计算、在界面预览/下载多页 PDF 结果及下载 Word 报告。可编辑的 XLS/XLSM 计算副本只保存在后台私有目录，不提供给用户下载。

> **运行前请先知道：**系统只使用 Windows 桌面版 Microsoft Excel，不支持 Linux 或 LibreOffice。Worker 需要在已登录的 Windows 用户会话里运行。当前模型已配置两本 Excel 串行计算及业务参数映射；下面的自然语言示例是技术联调输入，正式使用前应由业务人员确认模型公式及验收结果。

---

## 1. 准备环境

确认以下事项：

- Windows 10/11。
- Python 3.10 或更新版本。
- Microsoft Excel 桌面版已安装并能正常打开。
- Excel Worker 必须运行在已登录的 Windows 桌面会话中；Mac SSH 启动的 Worker 属于非交互会话，Excel COM 无法正常打开工作簿。
- 工作簿需要的宏、UDF、XLL 或 COM Add-in 已由管理员/业务方配置并完成测试。当前工作簿包含 `FIND_TEM`，若 Excel 中没有该自定义函数，计算会失败。
- 已取得 DeepSeek API Key。自然语言参数提取需要 Key；Key 不要提交到代码仓库。

在 VS Code 打开项目根目录，打开 PowerShell 终端。以下命令均从项目根目录运行。

如果你通过 SSH 从 Mac 操作 Windows，先在每个新的 Windows PowerShell 终端执行下面的命令，统一控制台为 UTF-8，避免 JSON 内容正确但终端显示中文乱码：

```powershell
chcp 65001
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
```

这只影响终端输入/显示编码，不改变 API 数据。API 与模型配置内部使用 Unicode/UTF-8。项目 API 也会在 JSON 响应头中声明 `charset=utf-8`，以兼容 Windows PowerShell 5.1；修改后请重启正在运行的 Uvicorn 进程。

### 首次安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如 PowerShell 阻止激活脚本，可只为当前终端运行一次：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

以后再次打开项目时，只需激活已创建的环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

---

## 2. 检查模型和模板配置

配置分三层，按这个顺序看：**①** [config/model_registry.json](config/model_registry.json)（有哪些模型可被路由到）→ **②** [config/models/\<id\>/model.json](config/models/model_energy_saving_calc/model.json)（业务参数、流程、报告）→ **③** [config/templates/\<id\>.json](config/templates/energy_saving_calc.json)（单元格在这本工作簿的哪个位置）。

串行模型 `model_energy_saving_calc` 使用 Mapping schema 2.0，串行执行两个 Excel artifact：

1. [节能减排计算.xlsm](templates/节能减排计算.xlsm)：由 `energy_saving_calc` artifact 执行。
2. [空气预热器工艺计算程序.xlsm](templates/空气预热器工艺计算程序.xlsm)：由 `air_preheater` artifact 执行。

第二步是**可选**的：控制参数 `include_air_preheater` 默认 `true`（照常串联），用户说「不用做空气预热器」时置为 `false`，该步骤会被跳过。另有一个独立模型 `model_air_preheater`，只跑空气预热器、由用户直接给出烟气流量与空气流量。

业务参数和 Excel Cell 映射分开定义。用户输入的业务参数 key 为 `heat_load`、`fuel_hydrogen_content`、`target_exhaust_temperature`；这些 key 不含单元格位置。**Sheet/Cell 只在 `config/templates/*.json` 里出现一次**，模型侧只写「业务 ID → 槽位名」。`air_preheater` 中实际工作表名是 `空气预热器-H`；配置使用工作簿里精确存在的名称。

Business Model 的用户输入参数与 Excel Cell 分离。用户只需描述业务概念，不需要输入参数 ID 或 Excel 单元格地址：

| 用户自然语言概念 | Business Model 参数 ID | 单位 | Excel Adapter Binding |
|---|---|---|---|
| 热负荷 / 热负载 | `heat_load` | MW | `energy_saving_calc` → `工艺计算!B11` |
| 燃料气组份中的氢气含量 | `fuel_hydrogen_content` | % | `energy_saving_calc` → `工艺计算!T11` |
| 目标排烟温度 | `target_exhaust_temperature` | °C | `energy_saving_calc` → `预热器计算!G74` |

当前只有热负荷是必填参数。氢气含量和目标排烟温度可选：用户没有在本次会话提供时，Worker 会复制 Excel 模板并保留相应单元格的原值，不写入空值，也不让大模型猜数字。例如仅输入“热负荷为240MW”即可确认并计算；氢气含量与排烟温度使用 `节能减排计算.xlsm` 模板副本中的值。需要覆盖默认值时，在自然语言中明确提供并在确认卡片检查。

Workflow 保证先执行 `energy_saving_calc`、再执行 `air_preheater`。业务 handoff 使用结果 ID：`energy_saving_calc.flue_gas_flow` → `air_preheater.flue_gas_flow`，`energy_saving_calc.air_flow` → `air_preheater.air_flow`。具体 Excel Cell 仅存在 Adapter Binding：第二个 artifact 的输入分别绑定到 `空气预热器-H!AA14` 和 `空气预热器-H!AO14`。两个原始模板均不会被覆盖。

百分数按百分点原样传入计算：用户说 `15.3%`，业务参数值是 `15.3`，不换算成 `0.153`。这是根据当前 Excel 参数约定设置的；如果 Excel 实际要求小数比例，必须先调整 Adapter 转换规则并重新验证。

`model.json` 使用 schema 2.0 顶层：`model`、`parameters`、`results`、`artifacts`、`bindings`、`workflow`、`handoffs`。API 会在每次模型/会话/参数/确认请求时检查配置文件变化，合法更新自动加载；已有任务使用创建时保存的模型快照。新增或调整 Cell 应只改 `bindings`；替换模板只改 `artifacts`；自然语言语义在 `parameters` 维护。参数/结果 ID 均使用业务名，不能用 Cell 地址命名。

模板必须位于 `EXCEL_TEMPLATE_ROOT` 指定的根目录内；默认优先使用项目下的 `templates` 文件夹（若该文件夹不存在，则兼容使用项目根目录）。模板路径在 `artifacts.<artifact_id>.template` 配置；输入/输出 Cell 只在 `bindings.<artifact_id>` 配置，LLM/Business Model 不包含 Excel 地址。

---

## 3. 启动 API

打开第一个 PowerShell 终端，激活虚拟环境并设置 DeepSeek Key。以下命令会在当前终端会话中保存密钥：

```powershell
.\.venv\Scripts\Activate.ps1
$secure = Read-Host "DeepSeek API Key" -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
python -m app.serve
```

看到启动信息后，不要关闭这个终端。默认只监听本机地址 `127.0.0.1`：

```
API 监听 127.0.0.1:8000  ·  口令认证：未启用（只有本机能访问）
```

> **为什么用 `app.serve` 而不是直接写 `uvicorn app.api:app --host ...`？**
> 因为「绑到哪个地址」必须和「有没有设口令」一起检查。用 `app.serve` 启动时，
> 一旦绑到非本机地址却没设口令，它会**直接拒绝启动**并告诉你怎么设——
> 这样就不会出现「以为有口令、其实在裸奔」。详见第 7 节。

在浏览器打开：

- 聊天交互界面：<http://127.0.0.1:8000/>
- 健康状态：<http://127.0.0.1:8000/health>
- 接口操作页面：<http://127.0.0.1:8000/docs>

若 `/health` 返回 `status: ok`，API 已就绪。这个状态只检查 API 启动，不代表 Excel 已能打开工作簿。

聊天界面会在浏览器中创建/恢复对话，显示参数提取草案；只有点击“确认并开始计算”后才会创建 Excel 任务。任务完成后会显示两本工作簿各自导出的多页 PDF 和 Word 报告。点击 PDF 或 Word 卡片的“预览”会在右侧打开 PDF 预览；点击下载箭头可分别下载 PDF 或原始 DOCX。Word 预览 PDF 由 Worker 使用已安装的 Microsoft Word 生成，因此需要在已登录的 Windows 桌面用户会话中完成 Word 首次启动/授权。可编辑 Excel 计算副本仅存放在服务器 `.runtime\private-workbooks\{job_id}` 中。若通过 Mac SSH 端口转发访问，使用转发后的本机地址，例如 `http://127.0.0.1:18000/`；Worker 仍需在已登录的 Windows 桌面会话运行。

---

## 4. 启动 Windows Excel Worker

在 Windows 桌面会话打开第二个 PowerShell 终端，激活同一个虚拟环境，再运行 Worker。不要在 Mac SSH 终端中启动 Worker；API 可以继续在 SSH 终端运行。

```powershell
.\.venv\Scripts\Activate.ps1
$secure = Read-Host "DeepSeek API Key（用于报告结果说明，可直接回车跳过）" -AsSecureString
if ($secure.Length -gt 0) {
    $env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
}
python -m app.worker
```

该终端会保持运行并等待 API 创建任务。没有设置 Key 时，Excel 任务仍可运行，但报告中的 AI 说明将使用本地固定说明。

**不要把 API 或 Worker 作为 Windows Service 在无人登录的会话中运行。**Office COM 自动化依赖交互式 Windows 用户环境。

如果需要通过 SSH 管理 Worker，可在 Windows“任务计划程序”创建任务：在“常规”页选择**仅当用户登录时运行**；在“操作”页将程序设为项目虚拟环境中的 `python.exe`，参数设为 `-m app.worker`，起始位置设为项目根目录；然后由已登录的 Windows 桌面用户启动该任务。不要选择“无论用户是否登录都要运行”。

---

## 5. 发起一次自然语言计算

可以在 VS Code 的第三个 PowerShell 终端、另一个终端，或 `/docs` 页面调用接口。以下 PowerShell 示例使用默认用户 `local-user`。

### 5.1 创建会话

```powershell
$base = "http://127.0.0.1:8000"
$session = Invoke-RestMethod `
  -Method Post `
  -Uri "$base/api/v1/sessions" `
  -ContentType "application/json" `
  -Body '{"model_id":"model_energy_saving_calc"}'

$session | Format-List
```

记下返回的 `session_id`：

```powershell
$sessionId = $session.session_id
```

### 5.2 发送参数

可以直接用自然语言说明业务条件，不需要提及参数键或单元格地址。例如：

```powershell
$body = @{
  message = "热负荷为240MW。其他未指定参数沿用 Excel 模板默认值。"
} | ConvertTo-Json -Compress
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)

$draft = Invoke-RestMethod `
  -Method Post `
  -Uri "$base/api/v1/sessions/$sessionId/messages" `
  -ContentType "application/json; charset=utf-8" `
  -Body $bodyBytes

$draft | ConvertTo-Json -Depth 10
```

检查返回值：

- `needs_confirmation` 为 `true`：检查 `confirmation_summary` 中的参数值正确后继续。
- 上面的示例预期只提取业务参数 `heat_load=240`；`fuel_hydrogen_content`、`target_exhaust_temperature` 不应被模型补值，确认卡片会只列出明确给出的热负荷。Excel Worker 对可选输入保留模板单元格值。
- 右侧参数栏中，未提供的可选项会显示“Excel 默认”。Worker 按 `bindings` 把 `heat_load` 写入当前配置的目标位置；未提供的可选参数绑定位置保持模板副本原值。
- `needs_confirmation` 为 `false`：检查 `clarification` 和 `missing_required`，再发送一条消息补齐缺少的参数。
- 返回 503：检查 API 终端是否设置 `DEEPSEEK_API_KEY` 以及网络/API 配置。
- 返回 422：消息提取结果不是已登记参数，或参数不符合配置中的类型/范围。

### 5.3 用户确认并创建任务

只有确认参数无误后才运行：

```powershell
$job = Invoke-RestMethod `
  -Method Post `
  -Uri "$base/api/v1/sessions/$sessionId/confirm" `
  -ContentType "application/json"

$job | ConvertTo-Json -Depth 10
$jobId = $job.job_id
```

确认前不要运行这一步；确认接口创建的任务会实际启动 Excel 计算。

### 5.4 查看任务状态

```powershell
$jobStatus = Invoke-RestMethod -Method Get -Uri "$base/api/v1/jobs/$jobId"
$jobStatus | ConvertTo-Json -Depth 10
```

常见状态：

- `QUEUED`：任务在本机队列中等待 Worker。
- `RUNNING`：Worker 正在启动 Excel 或计算。
- `SUCCEEDED`：计算和 Word 报告完成，响应包含 `artifacts`。
- `FAILED`：执行失败；查看 `error.code` 和 `error.message`，同时检查 Worker 终端日志。

可以再次执行上述 GET 命令刷新状态。

成功任务的 `results` 使用业务 result ID。`flue_gas_flow` 和 `air_flow` 分别 handoff 到第二个 artifact 的 `空气预热器-H!AA14`、`空气预热器-H!AO14`。用户可见的 `artifacts` 包含与模板同名（仅扩展名改为 `.pdf`）的两份 PDF 和一份 Word 报告，例如 `节能减排计算.pdf`、`空气预热器工艺计算程序.pdf`；不会包含任何 XLS/XLSM。

| 业务结果 ID | handoff 目标 |
|---|---:|
| `flue_gas_flow` | `空气预热器-H!AA14` |
| `air_flow` | `空气预热器-H!AO14` |

如果状态为 `FAILED`，查看响应里的 `error.code`、`error.message`，并检查 Worker 终端。若看到 `INTERACTIVE_SESSION_REQUIRED`，说明 Worker 是从 SSH/非交互会话启动的；应在 Windows 桌面会话中重新启动 Worker。

### 5.5 预览和下载 PDF / Word

聊天界面中，点击任一 PDF 产物卡片主体会在右侧预览；点击卡片右侧向下箭头下载该 PDF。Word 报告通过下载按钮获取。也可用 API 响应里的产物 ID 下载：

```powershell
$jobStatus.artifacts | Format-Table kind, file_name, artifact_id

New-Item -ItemType Directory -Force -Path ".\downloads" | Out-Null
foreach ($artifact in $jobStatus.artifacts) {
    $downloadUrl = "$base/api/v1/jobs/$jobId/artifacts/$($artifact.artifact_id)"
    $savePath = Join-Path ".\downloads" $artifact.file_name
    Invoke-WebRequest -Uri $downloadUrl -OutFile $savePath
    Write-Host "已下载：$savePath"
}
```

用户可见的 PDF/Word 位于 `.runtime\artifacts\{job_id}`；可编辑 XLS/XLSM 位于独立的 `.runtime\private-workbooks\{job_id}`，不登记为下载产物，API 会拒绝旧版本遗留的 Excel 下载记录。

### 5.6 核对后台 Excel A → Excel B 的数据传递

普通用户只下载 PDF，不能通过 API 下载 XLS/XLSM。管理员如需核对结果，可在 Windows 主机后台私有目录中读取两份**计算结果副本**，比较第一本中的 `S63`、`U63` 与第二本中的 `AA14`、`AO14`。两组值应分别相同；此检查脚本仅读取后台文件，不会改动工作簿。请在项目主机执行，不能在下载目录中执行。

```powershell
@'
import openpyxl
from pathlib import Path

import os
from app.config import get_settings
job_id = "替换为任务ID"
private_dir = get_settings().private_workbook_root / job_id
source_path = private_dir / "calculation-result.xlsm"
target_path = private_dir / "air_preheater-result.xlsm"
source_wb = openpyxl.load_workbook(source_path, read_only=True, data_only=True, keep_vba=True)
target_wb = openpyxl.load_workbook(target_path, read_only=True, data_only=True, keep_vba=True)
checks = [
  (source_wb["\u9884\u70ed\u5668\u8ba1\u7b97"]["S63"].value, target_wb["\u7a7a\u6c14\u9884\u70ed\u5668-H"]["AA14"].value, "S63 -> AA14"),
  (source_wb["\u9884\u70ed\u5668\u8ba1\u7b97"]["U63"].value, target_wb["\u7a7a\u6c14\u9884\u70ed\u5668-H"]["AO14"].value, "U63 -> AO14"),
]
for source, target, label in checks:
  print(f"{label}: source={source}, target={target}, match={source == target}")
  assert source == target, f"数据传递不一致：{label}"
source_wb.close()
target_wb.close()
print("双 Excel 数据传递检查通过。")
'@ | python -
```

如果检查报“文件不存在”，请确认任务 ID 正确、后台私有目录存在，且当前 PowerShell 工作目录是项目根目录。不要把该私有目录开放为静态文件目录或共享下载目录。

---

## 6. 运行单元测试

不需要启动 Excel 或调用 DeepSeek，即可运行本地测试：

```powershell
python -m pytest -q
```

测试覆盖模型定义、参数校验、偏好优先级、SQLite 任务队列、用户隔离和 A→B handoff 配置验证；这些测试不代表真实 Excel 模板已经通过计算验收。

---

## 7. 常见问题

### API 报“未配置 DEEPSEEK_API_KEY”或 503

密钥只在设置它的那个 PowerShell 进程中有效。API 和 Worker 是两个独立终端；需要 AI 结果说明时，Worker 终端也要设置密钥。不要把密钥写进 `config/model.json` 或提交到 Git。

### Worker 一直等待，没有任务

确认 API 和 Worker 使用同一项目目录、同一个 `EXCEL_DATABASE` 路径。默认情况下都使用项目下 `.runtime\jobs.sqlite3`。再确认 `/confirm` 已成功返回 `job_id`。

### Excel 报“不能访问文件”或 COM 自动化失败

确认 Excel 已安装并能在当前 Windows 用户桌面手动启动；关闭可能已打开的同名结果文件；确认模板文件存在且当前用户可读写 `.runtime` 目录。首次运行前，在桌面 Excel 中处理首次启动、授权、更新链接和信任提示。COM 错误时 Worker 会将任务标记为失败，不会退回 LibreOffice。

若依然失败，请先在这个 Windows 登录会话中分别手动打开 [templates/calculate.xlsm](templates/calculate.xlsm) 和 [templates/FH-C-028-202306-空气预热器工艺计算程序.xlsm](templates/FH-C-028-202306-空气预热器工艺计算程序.xlsm)，确认没有密码保护、弹窗阻塞或加载项错误，再查看 Worker 终端中完整错误。某些 UDF/宏必须在 Worker 所用用户的 Excel 配置中注册。

若错误码是 `PDF_EXPORT_FAILED` 且包含 `-2147024809`，这是 Excel COM 收到无效 PDF 导出参数导致的。已修正 Worker：不再向可选打印页码参数传入 `None`，由 Excel 默认导出整本工作簿。重启 Worker 后新建一条计算任务；旧的失败任务不会自动重试。

### `FIND_TEM` 相关公式报错

`FIND_TEM` 是自定义 Excel 函数，不是 Python 自动提供的函数。请确认对应 XLL/XLAM/COM 加载项或 VBA 函数已在 Microsoft Excel 中安装、启用并通过模板方验证。系统不会静默用其他软件模拟计算。

### 想让其他电脑访问

默认只监听本机，而且 `X-User-ID` **不是登录凭据**——它只是一个普通请求头。所以规则是：

> **远程可达 + 没有口令 = 不允许。**

这条规则由代码强制，不靠人记得：

| 情况 | 结果 |
|---|---|
| 绑 `127.0.0.1`（默认） | 正常启动，无需口令 |
| 绑 `0.0.0.0` / 局域网 IP，**没设口令** | **拒绝启动**，并打印怎么设口令 |
| 绑 `0.0.0.0`，**设了口令** | 正常启动，所有请求要求 HTTP Basic Auth |
| 没设口令，但请求的 Host 不是本机名（隧道 / 端口转发） | 每个请求返回 `503` 并说明原因 |

设口令（多个账号用逗号分隔，用户名会作为身份，**不能互相冒充**）：

```powershell
$env:EXCEL_API_AUTH = "tester:一串24位以上的随机口令"
$env:EXCEL_API_HOST = "0.0.0.0"
.\.venv\Scripts\python.exe -m app.serve
```

浏览器会弹出原生登录框，用户名/口令就是上面配的那组。**前端不需要任何改动。**

⚠️ 三个必须记住的点：

1. **口令必须走 HTTPS。** 本进程本身不提供 HTTPS；经「公网IP:端口」暴露时口令是明文过网的。请改为经由隧道（cloudflared 等自带 HTTPS）或反向代理访问。
2. **共享一个口令 = 所有测试者共用一个身份**，他们互相可见。要隔离就一人一组（用户名不同即可）。
3. **别忘了机器的前提**：Excel COM 需要已登录的交互式桌面会话。注销或睡眠后任务会失败。

要临时给别人一个网址测试，最省事且最容易关掉的是隧道（不改路由器、不开入站端口、自带 HTTPS、`Ctrl+C` 即彻底关闭）：

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

---

## 8. 当前原型的边界

1. 两本 Excel 及 `S63 → AA14`、`U63 → AO14` 映射已配置；请按第 5.6 节验证实际结果。
2. 当前没有甲方 Word 模板，报告使用程序生成的标准 Word 文档。
3. 当前自然语言参数映射为热负荷（MW）、燃料气氢气含量（%）和目标排烟温度（°C）；需由业务方确认真实参数口径、百分比表示、范围和验收基准。
4. 当前 API 的 `X-User-ID` 仅用于本机逻辑隔离，不是多用户认证系统。
5. Excel COM 计算依赖实际 Windows/Office/宏/加载项环境；需要在目标电脑用基准案例验收。
