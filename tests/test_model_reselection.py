"""会话锁定模型的重新评估 + 参数来源标注。

背景（真实故障）：用户在一个**昨天就绑定好串联模型**的会话里说
「预热器工艺计算，烟气量6500kg/h」，期望切到独立的空气预热器计算，结果：

* 路由器**根本没被调用**——会话已绑定、消息里又没有「切换/改算」这类词，
  于是整句话被当成对当前模型的参数补充；
* 确认卡片还把会话里上次的旧值（299MW / 15% / 88°C）列了出来，用户根本没提过它们。

这两条都在这里守着。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.domain import ModelRoute
from app.model_router import ModelRouter
from app.service import ParameterService
from app.storage import Storage

from test_multiple_model_resolution import (
    BOILER_ID,
    THERMAL_ID,
    build_resolver,
)


class ScriptedLLM:
    """按模型返回预设的抽取结果，并记录实际调用顺序。"""

    def __init__(self, values_by_model, route_to=None):
        self.values_by_model = {key: list(value) for key, value in values_by_model.items()}
        self.route_to = route_to
        self.models: list[str] = []
        self.route_calls: list[str] = []

    def route_model(self, message, candidates):
        assert self.route_to is not None, "这条用例不应该触发路由"
        self.route_calls.append(message)
        return {"intent": "calculate", "model_id": self.route_to}

    def extract_parameters(self, model, message, current_values):
        self.models.append(model.model.id)
        queue = self.values_by_model.setdefault(model.model.id, [{}])
        return {"parameters": queue.pop(0) if queue else {}, "clarification": None}


class ScriptedRouter:
    def __init__(self, routes=()):
        self.routes = list(routes)
        self.calls: list[str] = []

    def route(self, message):
        self.calls.append(message)
        return self.routes.pop(0)

    def validate_explicit(self, model_id):
        pass


def build_service(tmp_path, *, bound_model_id, llm, routes=()):
    resolver, _ = build_resolver(tmp_path)
    storage = Storage(tmp_path / "sessions.sqlite3")
    router = ScriptedRouter(routes)
    service = ParameterService(storage, None, llm, router, resolver)
    session = storage.create_session("alice", bound_model_id)
    return service, storage, router, session


# --------------------------------------------------------------------------- #
# 重新路由
# --------------------------------------------------------------------------- #

def test_bound_session_reroutes_when_the_round_extracts_nothing(tmp_path: Path):
    """用户改用另一种计算的提法时，必须重新问路由器，而不是硬套当前模型。"""
    llm = ScriptedLLM({
        # 当前（串联）模型在这句话里什么都抽不到
        THERMAL_ID: [{}],
        BOILER_ID: [{"fuel_consumption": 1200}],
    })
    service, storage, router, session = build_service(
        tmp_path,
        bound_model_id=THERMAL_ID,
        llm=llm,
        routes=[ModelRoute(intent="calculate", model_id=BOILER_ID)],
    )

    draft = service.process_message("alice", session["session_id"], "帮我算锅炉热效率")

    assert router.calls == ["帮我算锅炉热效率"]
    assert storage.get_session(session["session_id"], "alice")["model_id"] == BOILER_ID
    assert draft["parameters"]["fuel_consumption"] == 1200.0
    # 先用旧模型抽一次（判断这句话属不属于它），换模型后必须按新模型重抽
    assert llm.models == [THERMAL_ID, BOILER_ID]


def test_bound_session_keeps_its_model_when_the_round_extracts_something(tmp_path: Path):
    """正常的参数跟进不能被重新路由：会话连续性必须保留。"""
    llm = ScriptedLLM({THERMAL_ID: [{"heat_load": 240}, {"heat_load": 260}]})
    service, storage, router, session = build_service(
        tmp_path,
        bound_model_id=THERMAL_ID,
        llm=llm,
        routes=[ModelRoute(intent="calculate", model_id=BOILER_ID)],
    )

    service.process_message("alice", session["session_id"], "热负荷240MW")
    updated = service.process_message("alice", session["session_id"], "把热负荷改成260MW")

    assert router.calls == []
    assert storage.get_session(session["session_id"], "alice")["model_id"] == THERMAL_ID
    assert updated["parameters"]["heat_load"] == 260.0
    assert llm.models == [THERMAL_ID, THERMAL_ID]


def test_switching_room_clears_old_parameters(tmp_path: Path):
    """换模型时旧参数必须清空，不能把 A 模型的参数带进 B 模型。"""
    llm = ScriptedLLM({THERMAL_ID: [{"heat_load": 240}, {}], BOILER_ID: [{"fuel_consumption": 999}]})
    service, storage, router, session = build_service(
        tmp_path,
        bound_model_id=THERMAL_ID,
        llm=llm,
        routes=[ModelRoute(intent="calculate", model_id=BOILER_ID)],
    )

    service.process_message("alice", session["session_id"], "热负荷240MW")
    switched = service.process_message("alice", session["session_id"], "帮我算锅炉热效率")

    assert "heat_load" not in switched["parameters"]
    assert switched["parameters"]["fuel_consumption"] == 999.0


def test_router_clarification_is_honoured_for_bound_sessions_too(tmp_path: Path):
    """路由器拿不准时应该反问用户，而不是默默沿用当前模型。"""
    llm = ScriptedLLM({THERMAL_ID: [{}]})
    service, storage, router, session = build_service(
        tmp_path,
        bound_model_id=THERMAL_ID,
        llm=llm,
        routes=[ModelRoute(
            intent="clarification_required",
            candidate_models=[THERMAL_ID, BOILER_ID],
            clarification="请确认要做哪一种计算",
        )],
    )

    draft = service.process_message("alice", session["session_id"], "随便算点什么")

    assert draft["needs_confirmation"] is False
    assert draft["clarification"] == "请确认要做哪一种计算"
    assert draft["candidate_models"] == [THERMAL_ID, BOILER_ID]
    # 会话模型保持不变
    assert storage.get_session(session["session_id"], "alice")["model_id"] == THERMAL_ID


def test_zero_extraction_without_a_router_keeps_the_current_model(tmp_path: Path):
    """没有路由器时（例如测试或精简部署）不应因为零抽取而崩掉。"""
    llm = ScriptedLLM({THERMAL_ID: [{}]})
    resolver, _ = build_resolver(tmp_path)
    storage = Storage(tmp_path / "s.sqlite3")
    service = ParameterService(storage, None, llm, None, resolver)
    session = storage.create_session("alice", THERMAL_ID)

    draft = service.process_message("alice", session["session_id"], "嗯")

    assert draft["missing_required"] == ["heat_load"]
    assert storage.get_session(session["session_id"], "alice")["model_id"] == THERMAL_ID


# --------------------------------------------------------------------------- #
# 参数来源标注
# --------------------------------------------------------------------------- #

def test_confirmation_marks_where_each_value_came_from(tmp_path: Path):
    llm = ScriptedLLM({THERMAL_ID: [{"heat_load": 240}, {}]})
    service, storage, router, session = build_service(
        tmp_path,
        bound_model_id=THERMAL_ID,
        llm=llm,
        routes=[ModelRoute(intent="calculate", model_id=THERMAL_ID)],
    )

    first = service.process_message("alice", session["session_id"], "热负荷240MW")
    sources = {item["key"]: item["source"] for item in first["confirmation_summary"]}
    assert sources["heat_load"] == "input"
    assert sources["include_air_preheater"] == "default"

    # 第二轮什么都没抽到：旧值变成「沿用上次」，绝不能再标成「本轮」
    second = service.process_message("alice", session["session_id"], "嗯")

    assert second["parameter_sources"]["heat_load"] == "session"
    assert second["parameter_sources"]["include_air_preheater"] == "session"
    assert all(
        item["source"] == "session" for item in second["confirmation_summary"]
    )


def test_parameter_sources_are_exposed_through_the_api(tmp_path: Path, monkeypatch):
    import app.api as api_module

    llm = ScriptedLLM({THERMAL_ID: [{"heat_load": 240}]})
    resolver, catalog = build_resolver(tmp_path)
    storage = Storage(tmp_path / "api.sqlite3")
    monkeypatch.setattr(api_module, "storage", storage)
    monkeypatch.setattr(api_module, "model_registry", resolver)
    monkeypatch.setattr(api_module, "model_catalog", catalog)
    monkeypatch.setattr(api_module, "DeepSeekClient", lambda settings: llm)

    with TestClient(api_module.app) as client:
        session = client.post("/api/v1/sessions", json={"model_id": THERMAL_ID}).json()
        response = client.post(
            f"/api/v1/sessions/{session['session_id']}/messages",
            json={"message": "热负荷240MW"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["model_id"] == THERMAL_ID
    assert body["parameter_sources"]["heat_load"] == "input"
    assert {item["key"]: item["source"] for item in body["confirmation_summary"]}[
        "heat_load"
    ] == "input"


def test_missing_parameter_response_also_reports_the_bound_model(tmp_path: Path):
    """缺必填参数时前端也要知道是哪个模型，否则右侧面板会显示错的参数表。"""
    llm = ScriptedLLM({THERMAL_ID: [{}]})
    service, storage, router, session = build_service(
        tmp_path,
        bound_model_id=THERMAL_ID,
        llm=llm,
        routes=[ModelRoute(intent="calculate", model_id=THERMAL_ID)],
    )

    draft = service.process_message("alice", session["session_id"], "嗯")

    assert draft["model_id"] == THERMAL_ID
    assert draft["needs_confirmation"] is False
    assert draft["missing_required"] == ["heat_load"]


def test_switched_session_reports_the_new_model(tmp_path: Path):
    llm = ScriptedLLM({THERMAL_ID: [{}], BOILER_ID: [{"fuel_consumption": 1200}]})
    service, _, _, session = build_service(
        tmp_path,
        bound_model_id=THERMAL_ID,
        llm=llm,
        routes=[ModelRoute(intent="calculate", model_id=BOILER_ID)],
    )

    draft = service.process_message("alice", session["session_id"], "帮我算锅炉热效率")

    assert draft["model_id"] == BOILER_ID


# --------------------------------------------------------------------------- #
# 悬空绑定（配置改名 / 模型下线后的老会话）
# --------------------------------------------------------------------------- #

def test_dangling_session_binding_recovers_without_the_api_layer(tmp_path: Path):
    """会话绑定的模型已不存在时，必须当成「未绑定」重新路由，而不是报错卡死。"""
    llm = ScriptedLLM({BOILER_ID: [{"fuel_consumption": 1200}]}, route_to=BOILER_ID)
    resolver, catalog = build_resolver(tmp_path)
    storage = Storage(tmp_path / "dangling.sqlite3")
    service = ParameterService(storage, None, llm, ModelRouter(llm, catalog, resolver), resolver)

    session = storage.create_session("alice", THERMAL_ID)
    session_id = session["session_id"]
    storage.bind_session_model(session_id, "alice", "model-that-was-renamed", clear_parameters=True)

    draft = service.process_message("alice", session_id, "帮我算锅炉热效率")

    assert llm.route_calls == ["帮我算锅炉热效率"]
    assert draft["model_id"] == BOILER_ID
    assert draft["parameters"]["fuel_consumption"] == 1200.0
    # 悬空绑定必须被换成有效的模型，否则下一条消息又要重来一遍
    assert storage.get_session(session_id, "alice")["model_id"] == BOILER_ID


def test_dangling_session_binding_recovers_through_the_api(tmp_path: Path, monkeypatch):
    """真实事故回归：失败点原本在 API 层，请求还没进 service 就 404 了。

    所以这条必须走 HTTP——只测 service 会漏掉真正的现场。
    """
    import app.api as api_module

    resolver, catalog = build_resolver(tmp_path)
    storage = Storage(tmp_path / "api-dangling.sqlite3")
    llm = ScriptedLLM({BOILER_ID: [{"fuel_consumption": 1200}]}, route_to=BOILER_ID)

    monkeypatch.setattr(api_module, "storage", storage)
    monkeypatch.setattr(api_module, "model_registry", resolver)
    monkeypatch.setattr(api_module, "model_catalog", catalog)
    monkeypatch.setattr(api_module, "DeepSeekClient", lambda settings: llm)

    session = storage.create_session("alice", THERMAL_ID)
    session_id = session["session_id"]
    storage.bind_session_model(session_id, "alice", "model-that-was-renamed", clear_parameters=True)

    with TestClient(api_module.app) as client:
        response = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "帮我算锅炉热效率"},
            headers={"X-User-ID": "alice"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_id"] == BOILER_ID
    assert body["parameters"]["fuel_consumption"] == 1200.0
    assert storage.get_session(session_id, "alice")["model_id"] == BOILER_ID


def test_explicitly_requested_unknown_model_is_still_rejected(tmp_path: Path, monkeypatch):
    """悬空**绑定**要自愈，但显式指定的未知模型仍然必须报错，不能悄悄换成别的。"""
    import app.api as api_module

    resolver, catalog = build_resolver(tmp_path)
    storage = Storage(tmp_path / "api-explicit.sqlite3")
    llm = ScriptedLLM({THERMAL_ID: [{"heat_load": 240}]}, route_to=THERMAL_ID)

    monkeypatch.setattr(api_module, "storage", storage)
    monkeypatch.setattr(api_module, "model_registry", resolver)
    monkeypatch.setattr(api_module, "model_catalog", catalog)
    monkeypatch.setattr(api_module, "DeepSeekClient", lambda settings: llm)

    session = storage.create_session("alice", THERMAL_ID)

    with TestClient(api_module.app) as client:
        response = client.post(
            f"/api/v1/sessions/{session['session_id']}/messages",
            json={"message": "热负荷240MW", "model_id": "no-such-model"},
            headers={"X-User-ID": "alice"},
        )

    assert response.status_code == 404
    assert "no-such-model" in response.text


def test_confirming_a_dangling_session_explains_the_situation(tmp_path: Path, monkeypatch):
    """会话模型失效后点「确认」时，要给可操作的出路，而不是甩一个裸 model_id。"""
    import app.api as api_module

    resolver, catalog = build_resolver(tmp_path)
    storage = Storage(tmp_path / "api-confirm.sqlite3")
    llm = ScriptedLLM({THERMAL_ID: [{"heat_load": 240}]}, route_to=THERMAL_ID)

    monkeypatch.setattr(api_module, "storage", storage)
    monkeypatch.setattr(api_module, "model_registry", resolver)
    monkeypatch.setattr(api_module, "model_catalog", catalog)
    monkeypatch.setattr(api_module, "DeepSeekClient", lambda settings: llm)

    session = storage.create_session("alice", THERMAL_ID)
    session_id = session["session_id"]
    # 先让会话拿到一份待确认的完整参数，再把绑定弄失效（模拟改名的结果）
    api_module._service_for(resolver.get(THERMAL_ID)).process_message(
        "alice", session_id, "热负荷240MW"
    )
    storage.bind_session_model(session_id, "alice", "model-that-was-renamed", clear_parameters=False)

    with TestClient(api_module.app) as client:
        response = client.post(
            f"/api/v1/sessions/{session_id}/confirm", headers={"X-User-ID": "alice"}
        )

    assert response.status_code == 409
    assert "新建计算" in response.text
