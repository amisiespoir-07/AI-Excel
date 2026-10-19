"""Microsoft Excel 步骤执行器。

本模块是 Excel 专属逻辑的唯一归属地：它把「一个步骤要做什么」实现为一个注册到
``app.executors`` 的执行器。执行编排（步骤顺序、输入解析、产物收集）在
``app.workflow`` 里，与本模块无关。
"""

from __future__ import annotations

import math
import os
import shutil
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

from .domain import ModelDefinition, json_safe
from .executors import ExecutionError, StepExecutor, StepOutcome, StepRequest, register_executor


class ExcelExecutionError(ExecutionError):
    pass


_EXCEL_APP_RESOURCE = "microsoft-excel-application"

#: 已知的 COM 激活失败 HRESULT -> (错误码, 面向运维的排查指引)。
#: 这些错误只给裸 HRESULT 是无法排查的：看起来都像「服务器运行失败」，
#: 但真正原因（Office 未激活、DCOM 权限、加载项卡死）差别很大。
_COM_ERROR_CODES: dict[int, tuple[str, str]] = {
    # 0x80080005 CO_E_SERVER_EXEC_FAILURE
    -2146959355: (
        "EXCEL_COM_ACTIVATION_FAILED",
        "Excel 进程已经启动，但没有向 DCOM 注册，自动化无法建立连接。"
        "请依次排查：①「事件查看器 → Windows 日志 → 系统」中来源 DistributedCOM、"
        "事件 ID 10010 的记录；② 用 cscript //nologo OSPP.VBS /dstatus 确认 Office 授权状态"
        "（显示 ---NOTIFICATIONS--- 或 ---UNLICENSED--- 时需先重新激活）；"
        "③ 确认启动 Worker 的账户有权限启动 Excel，必要时改用管理员身份启动。",
    ),
    # 0x80070005 E_ACCESSDENIED
    -2147024891: (
        "EXCEL_COM_ACCESS_DENIED",
        "当前账户没有权限启动或访问 Excel 的 COM 服务器。"
        "请在 dcomcnfg 的「DCOM 配置 → Microsoft Excel Application → 属性 → 安全」中，"
        "确认「启动和激活权限」与「访问权限」包含当前运行 Worker 的账户。",
    ),
    # 0x80010105 RPC_E_SERVERFAULT
    -2147417851: (
        "EXCEL_COM_SERVER_FAULT",
        "Excel 进程在启动过程中异常退出。请手动打开一次 Excel 确认它可用，"
        "并检查是否有损坏的 COM 加载项（可用 EXCEL.EXE /safe 验证）。",
    ),
    # 0x800706BA RPC 服务器不可用
    -2147023174: (
        "EXCEL_RPC_UNAVAILABLE",
        "无法连接 Excel 的 RPC 服务，通常说明 Excel 进程被强制结束或系统 RPC 服务异常。"
        "请确认没有残留的 EXCEL.EXE 进程后重试。",
    ),
}


def classify_com_error(exc: BaseException) -> tuple[str, str] | None:
    """从 COM 异常里取出 HRESULT，映射成可操作的错误码与排查指引。"""
    hresult = getattr(exc, "hresult", None)
    if hresult is None:
        args = getattr(exc, "args", ())
        hresult = args[0] if args else None
    if not isinstance(hresult, int):
        return None
    return _COM_ERROR_CODES.get(hresult)


def _start_excel_app():
    """启动一个隐藏的 Excel 实例；失败时抛出带排查指引的 ExcelExecutionError。"""
    require_interactive_windows_session()
    try:
        import xlwings as xw
    except ImportError as exc:
        raise ExcelExecutionError("XLWINGS_NOT_INSTALLED", "未安装 xlwings，请安装 requirements.txt") from exc
    try:
        return xw.App(visible=False, add_book=False)
    except Exception as exc:
        classified = classify_com_error(exc)
        if classified is not None:
            code, guidance = classified
            raise ExcelExecutionError(code, f"{guidance}（原始错误：{exc}）") from exc
        raise ExcelExecutionError("EXCEL_START_FAILED", f"无法启动 Microsoft Excel：{exc}") from exc


def verify_excel_startable() -> str:
    """启动并立即退出一次 Excel，确认 COM 自动化可用；返回 Excel 版本字符串。

    Worker 在真正处理任务之前调用它。这样「Excel 起不来」会以**秒级**失败并带明确原因，
    而不是让任务卡在 COM 的长超时里、让前端一直转圈。
    """
    app = _start_excel_app()
    try:
        return str(app.version)
    finally:
        try:
            app.quit()
        except Exception:
            pass



def require_interactive_windows_session() -> int:
    """Excel Desktop COM automation must not run from SSH/service Session 0."""
    if os.name != "nt":
        raise ExcelExecutionError(
            "WINDOWS_EXCEL_REQUIRED",
            "Excel Worker 仅支持 Windows + 已安装的 Microsoft Excel。",
        )
    session_id = wintypes.DWORD()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_id_to_session_id = kernel32.ProcessIdToSessionId
    process_id_to_session_id.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    process_id_to_session_id.restype = wintypes.BOOL
    succeeded = process_id_to_session_id(
        os.getpid(), ctypes.byref(session_id)
    )
    if not succeeded:
        raise ExcelExecutionError(
            "WINDOWS_SESSION_UNKNOWN",
            f"无法识别当前 Windows 会话（WinError {ctypes.get_last_error()}）。",
        )
    if session_id.value == 0 or os.environ.get("SSH_CONNECTION"):
        raise ExcelExecutionError(
            "INTERACTIVE_SESSION_REQUIRED",
            "Excel COM 不能在 SSH/Session 0 非交互会话中运行。"
            "请在已登录的 Windows 桌面会话中启动 Worker，或使用任务计划程序的“仅当用户登录时运行”。",
        )
    return session_id.value


def _safe_template_path(template_root: Path, template: str) -> Path:
    root = template_root.resolve()
    candidate = (root / template).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ExcelExecutionError("INVALID_TEMPLATE_PATH", "模板路径超出允许的模板目录") from exc
    if candidate.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ExcelExecutionError("UNSUPPORTED_WORKBOOK", "只支持 .xlsx 与 .xlsm 工作簿")
    if not candidate.is_file():
        raise ExcelExecutionError("TEMPLATE_NOT_FOUND", f"找不到 Excel 模板：{template}")
    return candidate


def _validate_result(value: Any, key: str, definition) -> Any:
    if value is None and definition.required:
        raise ExcelExecutionError("EMPTY_EXCEL_RESULT", f"必需的 Excel 输出 {key} 为空")
    if isinstance(value, str) and value.startswith("#"):
        raise ExcelExecutionError("EXCEL_FORMULA_ERROR", f"Excel 输出 {key} 返回错误值 {value}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ExcelExecutionError("NON_FINITE_RESULT", f"Excel 输出 {key} 不是有限数值")
    if value is not None:
        if definition.type in {"number", "integer"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ExcelExecutionError("INVALID_RESULT_TYPE", f"Excel 输出 {key} 不是数值")
            if definition.type == "integer" and int(value) != value:
                raise ExcelExecutionError("INVALID_RESULT_TYPE", f"Excel 输出 {key} 不是整数")
        elif definition.type == "string" and not isinstance(value, str):
            raise ExcelExecutionError("INVALID_RESULT_TYPE", f"Excel 输出 {key} 不是文本")
        elif definition.type == "boolean" and not isinstance(value, bool):
            raise ExcelExecutionError("INVALID_RESULT_TYPE", f"Excel 输出 {key} 不是布尔值")
    return json_safe(value)


def _excel_app(request: StepRequest):
    """取得本次工作流共享的 Excel 进程；没有就启动一个并注册收尾清理。

    整条工作流复用同一个 Excel 实例（而不是每步重启一次），既省下 Office 冷启动开销，
    也让全簿重算的行为与手工操作一致。
    """
    cached = request.context.resources.get(_EXCEL_APP_RESOURCE)
    if cached is not None:
        return cached

    app = _start_excel_app()
    app.display_alerts = False
    app.screen_updating = False
    app.enable_events = True
    request.context.resources[_EXCEL_APP_RESOURCE] = app

    def _quit_excel() -> None:
        try:
            app.quit()
        except Exception:
            pass

    request.context.add_cleanup(_quit_excel)
    return app


class ExcelCalculationExecutor:
    """打开模板副本、写入输入、全簿重算、读回输出，并把工作簿导出为 PDF。"""

    step_type = "excel_calculation"
    artifact_types = frozenset({"excel"})

    def run(self, request: StepRequest) -> StepOutcome:
        artifact_id = request.artifact_id
        step_id = request.step.id
        app = _excel_app(request)

        template_path = _safe_template_path(request.template_root, request.artifact.template or "")
        # 可编辑副本只进私有目录；公开目录只放 PDF。
        output_path = request.private_directory / f"{artifact_id}-result{template_path.suffix.lower()}"
        # Keep the source template's filename; only replace .xls/.xlsx/.xlsm
        # with .pdf so the downloaded format remains unambiguous.
        pdf_path = request.artifact_directory / f"{template_path.stem}.pdf"
        shutil.copy2(template_path, output_path)

        book = None
        try:
            book = app.books.open(
                str(output_path),
                update_links=False,
                read_only=False,
                ignore_read_only_recommended=True,
            )
            for input_id, cell_binding in request.binding.inputs.items():
                if input_id not in request.input_values:
                    # missing.policy = template_default：保留模板副本里的原值/公式
                    continue
                book.sheets[cell_binding.sheet].range(cell_binding.cell).value = request.input_values[input_id]

            # 真实 Excel 全簿重建并计算；任何 COM/加载项错误都上报，不静默换引擎。
            app.api.CalculateFullRebuild()
            artifact_results: dict[str, Any] = {}
            for result_id, cell_binding in request.binding.outputs.items():
                raw_value = book.sheets[cell_binding.sheet].range(cell_binding.cell).value
                artifact_results[result_id] = _validate_result(
                    raw_value, f"{artifact_id}.{result_id}", request.model.results[result_id]
                )
            book.save()
            try:
                request.stage(f"{step_id}_pdf_export")
                # Export the workbook's configured print areas/pages to PDF.
                # The editable XLS/XLSM stays exclusively in private storage.
                # Do not pass Python None for Excel's optional From/To page
                # arguments: COM interprets those as invalid VARIANT values
                # (E_INVALIDARG). Omitting them exports the full workbook.
                book.api.ExportAsFixedFormat(
                    0, str(pdf_path), 0, True, False
                )
            except Exception as exc:
                raise ExcelExecutionError(
                    "PDF_EXPORT_FAILED",
                    f"Microsoft Excel 导出 {artifact_id} PDF 失败：{exc}",
                ) from exc
        except ExcelExecutionError:
            raise
        except Exception as exc:
            raise ExcelExecutionError(
                "EXCEL_AUTOMATION_FAILED",
                f"Microsoft Excel 执行 artifact {artifact_id} 失败：{exc}",
            ) from exc
        finally:
            if book is not None:
                try:
                    book.close()
                except Exception:
                    pass

        if not pdf_path.is_file() or pdf_path.stat().st_size < 5:
            raise ExcelExecutionError(
                "PDF_EXPORT_EMPTY",
                f"Excel 未生成有效 PDF：{artifact_id}",
            )
        with pdf_path.open("rb") as pdf_file:
            if pdf_file.read(5) != b"%PDF-":
                raise ExcelExecutionError(
                    "PDF_EXPORT_INVALID",
                    f"Excel 导出的文件不是有效 PDF：{artifact_id}",
                )

        return StepOutcome(
            results=artifact_results,
            artifacts=[
                {
                    "kind": "pdf",
                    "file_name": pdf_path.name,
                    "file_path": str(pdf_path.resolve()),
                    "size_bytes": pdf_path.stat().st_size,
                }
            ],
        )


register_executor(ExcelCalculationExecutor())


def execute_model(
    model: ModelDefinition,
    parameters: dict[str, Any],
    template_root: Path,
    artifact_directory: Path,
    private_workbook_directory: Path,
    on_stage: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """兼容入口：等价于用全局注册的执行器运行整条工作流。

    真正的编排逻辑在 :func:`app.workflow.execute_model`。保留同名函数是为了让既有
    调用方（以及既有测试）不必改动。
    """
    from .workflow import execute_model as run_workflow

    return run_workflow(
        model,
        parameters,
        template_root,
        artifact_directory,
        private_workbook_directory,
        on_stage=on_stage,
    )


__all__ = [
    "ExcelCalculationExecutor",
    "ExcelExecutionError",
    "StepExecutor",
    "classify_com_error",
    "execute_model",
    "register_executor",
    "require_interactive_windows_session",
    "verify_excel_startable",
]
