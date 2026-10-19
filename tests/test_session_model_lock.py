import json

from app.domain import ModelRoute
from app.service import ParameterService
from app.storage import Storage

from test_multiple_model_resolution import (
    BOILER_ID,
    ROOT,
    THERMAL_ID,
    build_resolver,
)


def defaulted_parameter_values() -> dict:
    """带 missing.policy="default" 的参数（例如“是否继续空气预热器计算”）会被服务层自动补上默认值。"""
    payload = json.loads(
        (ROOT / "config" / "models" / THERMAL_ID / "model.json").read_text(encoding="utf-8")
    )
    return {
        key: spec["missing"]["value"]
        for key, spec in payload["parameters"].items()
        if spec["missing"]["policy"] == "default"
    }


class CountingRouter:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []
        self.explicit = []

    def route(self, message):
        self.calls.append(message)
        return self.routes.pop(0)

    def validate_explicit(self, model_id):
        self.explicit.append(model_id)


class RecordingLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.models = []

    def extract_parameters(self, model, message, current_values):
        self.models.append((model.model.id, set(model.parameters)))
        return {"parameters": self.outputs.pop(0), "clarification": None}


def build_service(tmp_path, model_id=None, routes=(), outputs=()):
    resolver, _ = build_resolver(tmp_path)
    model = resolver.get(model_id) if model_id else None
    storage = Storage(tmp_path / "sessions.sqlite3")
    router = CountingRouter(routes)
    llm = RecordingLLM(outputs)
    service = ParameterService(storage, model, llm, router, resolver)
    return service, storage, router, llm, resolver


def test_new_unbound_session_routes_once_and_persists_model_id(tmp_path):
    service, storage, router, llm, _ = build_service(
        tmp_path,
        routes=[ModelRoute(intent="calculate", model_id=THERMAL_ID)],
        outputs=[{"heat_load": 240}],
    )
    session = service.create_session("alice")
    assert session["model_id"] is None

    service.process_message("alice", session["session_id"], "帮我计算空气预热器，热负荷240MW")

    stored = storage.get_session(session["session_id"], "alice")
    assert router.calls == ["帮我计算空气预热器，热负荷240MW"]
    assert stored["model_id"] == THERMAL_ID
    assert llm.models[0][0] == THERMAL_ID


def test_parameter_edits_continue_locked_model_without_rerouting(tmp_path):
    service, storage, router, llm, _ = build_service(
        tmp_path,
        routes=[ModelRoute(intent="calculate", model_id=THERMAL_ID)],
        outputs=[{"heat_load": 240}, {"heat_load": 250}, {"fuel_hydrogen_content": 15.3}],
    )
    session = service.create_session("alice")

    service.process_message("alice", session["session_id"], "帮我计算空气预热器，热负荷240MW")
    updated = service.process_message("alice", session["session_id"], "热负荷改成250MW")
    updated = service.process_message("alice", session["session_id"], "氢气含量15.3%")

    stored = storage.get_session(session["session_id"], "alice")
    assert len(router.calls) == 1
    assert stored["model_id"] == THERMAL_ID
    assert updated["parameters"] == {
        **defaulted_parameter_values(),
        "heat_load": 250.0,
        "fuel_hydrogen_content": 15.3,
    }
    assert [model_id for model_id, _ in llm.models] == [THERMAL_ID] * 3


def test_existing_model_lock_ignores_mock_router_that_would_choose_other_model(tmp_path):
    service, storage, router, _, _ = build_service(
        tmp_path,
        model_id=THERMAL_ID,
        routes=[ModelRoute(intent="calculate", model_id=BOILER_ID)],
        outputs=[{"heat_load": 250}],
    )
    session = service.create_session("alice")
    storage.update_session(session["session_id"], "alice", {"heat_load": 240.0}, pending=True)

    result = service.process_message("alice", session["session_id"], "热负荷改成250MW")

    assert router.calls == []
    assert result["parameters"]["heat_load"] == 250.0
    assert storage.get_session(session["session_id"], "alice")["model_id"] == THERMAL_ID


def test_boiler_session_uses_boiler_definition_without_router(tmp_path):
    service, storage, router, llm, _ = build_service(
        tmp_path,
        model_id=BOILER_ID,
        routes=[ModelRoute(intent="calculate", model_id=THERMAL_ID)],
        outputs=[{"fuel_consumption": 1200, "lower_heating_value": 42000, "steam_output": 900}],
    )
    session = service.create_session("alice")

    result = service.process_message("alice", session["session_id"], "燃料消耗改成1200")

    assert router.calls == []
    assert llm.models[0] == (
        BOILER_ID, {"fuel_consumption", "lower_heating_value", "steam_output"}
    )
    assert result["parameters"]["fuel_consumption"] == 1200.0
    assert storage.get_session(session["session_id"], "alice")["model_id"] == BOILER_ID


def test_explicit_switch_routes_once_resets_old_parameters_and_locks_new_model(tmp_path):
    service, storage, router, llm, _ = build_service(
        tmp_path,
        model_id=THERMAL_ID,
        routes=[ModelRoute(intent="calculate", model_id=BOILER_ID)],
        outputs=[
            {"fuel_consumption": 1200, "lower_heating_value": 42000, "steam_output": 900},
            {"fuel_consumption": 1250},
        ],
    )
    session = service.create_session("alice")
    storage.update_session(
        session["session_id"], "alice", {"heat_load": 240.0}, pending=False
    )

    switched = service.process_message(
        "alice", session["session_id"], "不要算空气预热器了，改算锅炉热效率"
    )
    continued = service.process_message(
        "alice", session["session_id"], "燃料消耗改成1250"
    )

    stored = storage.get_session(session["session_id"], "alice")
    assert len(router.calls) == 1
    assert stored["model_id"] == BOILER_ID
    assert "heat_load" not in switched["parameters"]
    assert continued["parameters"]["fuel_consumption"] == 1250.0
    assert [model_id for model_id, _ in llm.models] == [BOILER_ID, BOILER_ID]


def test_explicit_model_id_switches_existing_session_and_clears_old_parameters(tmp_path):
    service, storage, router, llm, _ = build_service(
        tmp_path,
        model_id=THERMAL_ID,
        outputs=[{"fuel_consumption": 1200, "lower_heating_value": 42000, "steam_output": 900}],
    )
    session = service.create_session("alice")
    storage.update_session(session["session_id"], "alice", {"heat_load": 240.0}, pending=True)

    result = service.process_message(
        "alice", session["session_id"], "锅炉热效率", explicit_model_id=BOILER_ID
    )

    stored = storage.get_session(session["session_id"], "alice")
    assert router.calls == []
    assert router.explicit == [BOILER_ID]
    assert stored["model_id"] == BOILER_ID
    assert "heat_load" not in result["parameters"]
    assert llm.models[0][0] == BOILER_ID


def test_legacy_unbound_session_routes_and_binds(tmp_path):
    resolver, _ = build_resolver(tmp_path)
    storage = Storage(tmp_path / "legacy.sqlite3")
    # Empty model_id is the persisted representation used by the compatible nullable-session path.
    session = storage.create_session("alice", None)
    router = CountingRouter([ModelRoute(intent="calculate", model_id=THERMAL_ID)])
    llm = RecordingLLM([{"heat_load": 240}])
    service = ParameterService(storage, None, llm, router, resolver)

    result = service.process_message("alice", session["session_id"], "空气预热器热负荷240MW")

    assert len(router.calls) == 1
    assert result["parameters"]["heat_load"] == 240.0
    assert storage.get_session(session["session_id"], "alice")["model_id"] == THERMAL_ID
