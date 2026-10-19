from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import get_settings
from .llm import DeepSeekClient, LLMOutputInvalid, LLMUnavailable
from .model_catalog import ModelCatalogRegistry, ModelCatalogUnavailable
from .model_registry import ModelDefinitionRegistry, ModelDefinitionUnavailable, ModelNotFound
from .model_router import InvalidModelRoute, ModelRouter
from .security import ApiAuth, host_header_is_local, load_api_auth
from .service import ConflictError, NotFoundError, ParameterService
from .storage import Storage


def _load_api_auth() -> ApiAuth:
    try:
        return load_api_auth(settings.api_auth)
    except ValueError as exc:
        # 配置写错就起不来，绝不静默降级成「其实没有口令」
        raise RuntimeError(f"EXCEL_API_AUTH 配置有误：{exc}") from exc


settings = get_settings()
#: 访问控制。测试可以直接替换这个模块级变量。
API_AUTH = _load_api_auth()
model_catalog = ModelCatalogRegistry(settings.model_registry)
model_registry = ModelDefinitionRegistry(
    catalog=model_catalog,
    definitions_root=settings.model_definitions_root,
    templates_root=settings.template_catalog_root,
)
storage = Storage(settings.database_path)
app = FastAPI(title="AI + Excel Windows Worker", version="0.1.0")
static_dir = Path(__file__).resolve().parent / "static"


def _default_model_id() -> str:
    """没有显式指定 model_id 时使用的兜底模型（第一个已启用模型）。

    仅用于 /health 与偏好接口这类「没有会话上下文」的调用；
    所有计算派发都必须使用会话里记录的 model_id，不能回退到这里。
    """
    try:
        enabled = model_catalog.get_enabled()
    except ModelCatalogUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not enabled:
        raise HTTPException(status_code=503, detail="没有已启用的计算模型")
    return enabled[0].id


def _definition_for(model_id: str):
    try:
        return model_registry.get(model_id)
    except ModelNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ModelDefinitionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _default_model():
    return _definition_for(_default_model_id())


def _enabled_definitions():
    try:
        entries = model_catalog.get_enabled()
    except ModelCatalogUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return [_definition_for(entry.id) for entry in entries]


@app.on_event("startup")
def validate_enabled_model_definitions() -> None:
    """Reject enabled catalog entries with missing or mismatched definitions."""
    try:
        entries = model_catalog.get_enabled()
        for entry in entries:
            model_registry.get(entry.id)
    except (ModelCatalogUnavailable, ModelDefinitionUnavailable) as exc:
        raise RuntimeError(f"已启用模型定义校验失败：{exc}") from exc


def _service_for(model_definition=None) -> ParameterService:
    llm = DeepSeekClient(settings)
    # 把定义 registry 交给路由器，让它能看到每个候选模型的参数提法
    router = ModelRouter(llm, model_catalog, model_registry)
    return ParameterService(storage, model_definition, llm, router, model_registry)


@app.get("/", include_in_schema=False)
def chat_home():
    return FileResponse(static_dir / "index.html", media_type="text/html; charset=utf-8")


@app.middleware("http")
async def declare_json_utf8(request: Request, call_next):
    """PowerShell 5.1 needs an explicit charset to decode application/json correctly."""
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.lower().startswith("application/json") and "charset=" not in content_type.lower():
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


def _force_request_header(request: Request, name: str, value: str) -> None:
    """用服务端决定的值替换请求头。

    直接改 ASGI scope，端点里那些 `Header(alias="X-User-ID")` 依赖会照常读到它，
    因此不需要改动任何端点。
    """
    key = name.lower().encode("latin-1")
    headers = [(k, v) for k, v in request.scope["headers"] if k.lower() != key]
    headers.append((key, value.encode("latin-1")))
    request.scope["headers"] = headers


@app.middleware("http")
async def enforce_api_access(request: Request, call_next):
    """访问口令校验 + 「远程可达却没有口令」的兜底拦截。

    这条是最外层的（最后注册 = 最外层），所以它自己返回的响应不会经过 charset
    中间件，需要显式带上 charset。
    """
    auth = API_AUTH
    if auth:
        username = auth.authenticate(request.headers.get("authorization"))
        if username is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "需要访问口令（HTTP Basic Auth）"},
                headers={"WWW-Authenticate": 'Basic realm="Excel Engine", charset="UTF-8"'},
                media_type="application/json; charset=utf-8",
            )
        # 身份由口令决定；客户端自带的 X-User-ID 一律覆盖掉——
        # 否则拿到口令的人只要改个请求头就能读别人的数据。
        _force_request_header(request, "x-user-id", username)
    elif not host_header_is_local(request.url.hostname):
        # 启动时看不出来的那种情况：隧道从本机连过来，绑的仍是 127.0.0.1，
        # 但 Host 是外网域名。没有口令就不放行。
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "拒绝未认证的远程访问：请求来自非本机主机名，但服务没有配置访问口令。"
                    '请设置环境变量 EXCEL_API_AUTH="用户名:口令" 后重启 API。'
                )
            },
            media_type="application/json; charset=utf-8",
        )
    return await call_next(request)


class CreateSessionRequest(BaseModel):
    model_id: str | None = None


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=10000)
    model_id: str | None = None


class PreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, Any]
    model_id: str | None = None


def _user_id(x_user_id: str = Header(default="local-user", alias="X-User-ID")) -> str:
    value = x_user_id.strip()
    if not value or len(value) > 128:
        raise HTTPException(status_code=400, detail="X-User-ID 无效")
    return value


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "session_id": job["session_id"],
        "model_id": job["model_id"],
        "status": job["status"],
        "stage": job["stage"],
        "attempts": job["attempts"],
        "results": job["results"],
        "error": job["error"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "artifacts": [
            {
                "artifact_id": item["artifact_id"],
                "kind": item["kind"],
                "file_name": item["file_name"],
                "size_bytes": item["size_bytes"],
                "download_path": f"/api/v1/jobs/{job['job_id']}/artifacts/{item['artifact_id']}",
                **(
                    {
                        "preview_path": f"/api/v1/jobs/{job['job_id']}/artifacts/{item['artifact_id']}?preview=true",
                        "preview_name": Path(item["preview_path"]).name if item.get("preview_path") else item["file_name"],
                    }
                    if item["kind"] == "pdf" or item.get("preview_path") else {}
                ),
            }
            for item in job["artifacts"]
            if item["kind"] in {"pdf", "word"}
        ],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    active_model = _default_model()
    return {
        "status": "ok",
        "platform": os.name,
        "excel_engine": "Microsoft Excel via xlwings (Windows only)",
        "model_id": active_model.model.id,
        "model_name": active_model.model.name,
        "model_version": active_model.model.version,
    }


def _parameter_payload(active_model) -> dict[str, Any]:
    """参数清单。``control`` 标记出只驱动流程、不写入单元格的参数
    （例如「是否继续空气预热器计算」），界面据此换一种展示方式。"""
    control_ids = active_model.control_parameter_ids
    return {
        key: {
            "description": spec.description,
            "display_name": spec.display_name,
            "aliases": spec.input.aliases,
            "type": spec.type,
            "unit": spec.unit,
            "required": spec.required,
            "control": key in control_ids,
            "missing": spec.missing.model_dump(mode="json"),
            "validation": spec.validation.model_dump(mode="json"),
        }
        for key, spec in active_model.parameters.items()
    }


@app.get("/api/v1/models")
def list_models() -> list[dict[str, Any]]:
    definitions = _enabled_definitions()
    return [
        {
            "model_id": active_model.model.id,
            "schema_version": active_model.schema_version,
            "version": active_model.model.version,
            "name": active_model.model.name,
            "description": active_model.model.description,
            "parameters": _parameter_payload(active_model),
            "results": {
                key: {
                    "display_name": result.display_name,
                    "type": result.type,
                    "unit": result.unit,
                    "required": result.required,
                }
                for key, result in active_model.results.items()
            },
            "artifacts": {
                key: {
                    "type": artifact.type,
                    "display_name": artifact.display_name or key,
                }
                for key, artifact in active_model.artifacts.items()
            },
            "workflow": {
                "steps": [step.model_dump(mode="json") for step in active_model.workflow.steps]
            },
            # 界面引导文案由模型定义提供，前端不再硬编码任何业务示例。
            "examples": [example.model_dump(mode="json") for example in active_model.examples],
            "hints": list(active_model.hints),
        }
        for active_model in definitions
    ]


@app.post("/api/v1/sessions", status_code=201)
def create_session(payload: CreateSessionRequest, user_id: str = Header(default="local-user", alias="X-User-ID")):
    model_definition = _definition_for(payload.model_id) if payload.model_id else None
    return _service_for(model_definition).create_session(user_id.strip() or "local-user")


@app.post("/api/v1/sessions/{session_id}/messages")
def send_message(
    session_id: str,
    payload: MessageRequest,
    user_id: str = Header(default="local-user", alias="X-User-ID"),
):
    try:
        session = storage.get_session(session_id, user_id.strip() or "local-user")
        if session is None:
            raise NotFoundError("会话不存在")
        # 显式指定的模型必须有效；会话自带的绑定则允许「已失效」。
        #
        # 配置改名或下线模型都会让老会话的绑定悬空，那属于**正常的配置变更**，
        # 不该让这个会话永久失败——在这里报错的话，每条消息都死在这一行，
        # 连「改算/切换到…」这类切换语都走不到（切换语在 process_message 里才判断）。
        # 悬空时传 None 下去，由 process_message 当成「未绑定」重新路由。
        model_definition = None
        if payload.model_id:
            model_definition = _definition_for(payload.model_id)
        elif session["model_id"]:
            try:
                model_definition = model_registry.get(session["model_id"])
            except ModelNotFound:
                model_definition = None
            except ModelDefinitionUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _service_for(model_definition).process_message(
            user_id.strip() or "local-user", session_id, payload.message, payload.model_id
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LLMOutputInvalid as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "invalid_llm_output", "message": str(exc)},
        ) from exc
    except LLMUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (InvalidModelRoute, ModelCatalogUnavailable) as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_model", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/sessions/{session_id}/confirm", status_code=202)
def confirm_session(session_id: str, user_id: str = Header(default="local-user", alias="X-User-ID")):
    try:
        owner = user_id.strip() or "local-user"
        session = storage.get_session(session_id, owner)
        if session is None:
            raise NotFoundError("会话不存在")
        if not session["model_id"]:
            raise ConflictError("会话尚未绑定计算模型")
        # 绑定的模型可能已被改名/下线。给出可操作的出路，而不是把裸 model_id
        # 甩给用户——那看起来像系统坏了，其实只是一次普通的配置变更。
        try:
            model_definition = model_registry.get(session["model_id"])
        except ModelNotFound as exc:
            raise ConflictError(
                f"该会话绑定的计算模型已不存在（{session['model_id']}）。"
                "请点击「新建计算」重新开始，或直接在会话里说明要做哪种计算。"
            ) from exc
        except ModelDefinitionUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return _public_job(
            _service_for(model_definition).confirm(owner, session_id)
        )
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, user_id: str = Header(default="local-user", alias="X-User-ID")):
    job = storage.get_job(job_id, user_id.strip() or "local-user")
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _public_job(job)


@app.get("/api/v1/jobs/{job_id}/artifacts/{artifact_id}")
def download_artifact(
    job_id: str,
    artifact_id: str,
    preview: bool = Query(default=False),
    user_id: str = Header(default="local-user", alias="X-User-ID"),
):
    owner = user_id.strip() or "local-user"
    job = storage.get_job(job_id, owner)
    artifact = storage.get_artifact(artifact_id, owner)
    if job is None or artifact is None or artifact["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="文件不存在")
    if artifact["kind"] not in {"pdf", "word"}:
        # Also blocks legacy job records that exposed editable Excel workbooks.
        raise HTTPException(status_code=404, detail="该文件类型不允许下载")
    path = Path(artifact["file_path"]).resolve()
    try:
        path.relative_to(settings.artifact_root.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="文件不存在") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已过期或不存在")
    if preview:
        if artifact["kind"] == "pdf":
            preview_path = path
        elif artifact["kind"] == "word" and artifact.get("preview_path"):
            preview_path = Path(artifact["preview_path"]).resolve()
            try:
                preview_path.relative_to(settings.artifact_root.resolve())
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="预览文件不存在") from exc
        else:
            raise HTTPException(status_code=415, detail="该文件没有可用的在线预览")
        if not preview_path.is_file():
            raise HTTPException(status_code=404, detail="预览文件已过期或不存在")
        with preview_path.open("rb") as preview_file:
            if preview_file.read(5) != b"%PDF-":
                raise HTTPException(status_code=415, detail="预览文件不是有效 PDF")
        return FileResponse(
            preview_path,
            filename=preview_path.name,
            media_type="application/pdf",
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store"},
        )

    media_type = "application/pdf" if artifact["kind"] == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return FileResponse(
        path,
        filename=artifact["file_name"],
        media_type=media_type,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/api/v1/preferences")
def get_preferences(
    model_id: str | None = Query(default=None, description="要查询的模型偏好；省略时使用兜底模型"),
    user_id: str = Header(default="local-user", alias="X-User-ID"),
):
    target = model_id or _default_model_id()
    result = _service_for(_definition_for(target)).get_preferences(user_id.strip() or "local-user")
    return {**result, "model_id": target}


@app.put("/api/v1/preferences")
def put_preferences(
    payload: PreferencesRequest,
    user_id: str = Header(default="local-user", alias="X-User-ID"),
):
    try:
        target = payload.model_id or _default_model_id()
        result = _service_for(_definition_for(target)).save_preferences(
            user_id.strip() or "local-user", payload.values, model_id=payload.model_id
        )
        return {**result, "model_id": target}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


app.mount("/static", StaticFiles(directory=static_dir), name="static")
