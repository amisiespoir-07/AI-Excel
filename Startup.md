
## 1. 准备环境

确认以下事项：

- Windows 10/11。
- Python 3.10.1 或更新版本。
- Microsoft Excel 桌面版已安装并能正常打开。
- Excel Worker 必须运行在已登录的 Windows 桌面会话中。

---
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


## 3. 启动 API

打开第一个 PowerShell 终端，激活虚拟环境并设置 DeepSeek Key。以下命令会在当前终端会话中保存密钥：

```powershell
.\.venv\Scripts\Activate.ps1
$env:EXCEL_API_AUTH = "tester:换成你自己的24位以上随机口令"
$env:EXCEL_API_HOST = "0.0.0.0"
$secure = Read-Host "DeepSeek API Key" -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", $secure).Password
python -m app.serve
```

看到启动信息后，不要关闭这个终端。默认只监听本机地址 `127.0.0.1`：

```
API 监听 127.0.0.1:8000  ·  口令认证：启用
```


在浏览器打开：

- 聊天交互界面：<http://127.0.0.1:8000/>
- 健康状态：<http://127.0.0.1:8000/health>


若 `/health` 返回 `status: ok`，API 已就绪。这个状态只检查 API 启动，不代表 Excel 已能打开工作簿。


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
