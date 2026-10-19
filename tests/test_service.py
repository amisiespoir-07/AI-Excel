from pathlib import Path

import pytest

from app.domain import ModelRoute, load_model_definition
from app.llm import LLMOutputInvalid
from app.service import ParameterService
from app.storage import Storage


ROOT = Path(__file__).resolve().parents[1]


def thermal_model():
    return load_model_definition(ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json")


def defaulted_parameters(model) -> dict:
    """带 missing.policy="default" 的参数（例如“是否继续空气预热器计算”）会被服务层自动补上默认值。"""
    return {
        key: spec.missing.value
        for key, spec in model.parameters.items()
        if spec.missing.policy == "default"
    }


class FakeLLM:
    def __init__(self, parameters):
        self.parameters = parameters

    def extract_parameters(self, model, user_message, current_values):
        return {"parameters": self.parameters, "clarification": ""}


class FakeRouter:
    def __init__(self, route):
        self.result = route
        self.calls = []

    def route(self, message):
        self.calls.append(message)
        return self.result

    def validate_explicit(self, model_id):
        assert model_id == "model_energy_saving_calc"


def test_confirmed_parameters_create_queued_job_and_user_input_wins(tmp_path: Path):
    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")
    storage.set_preferences("alice", {"heat_load": 10})
    storage.set_preferences("alice", {"heat_load": 20}, model_id=model.model_id)
    service = ParameterService(storage, model, FakeLLM({
        "heat_load": 200,
        "fuel_hydrogen_content": 15.3,
        "target_exhaust_temperature": 85,
    }))
    session = service.create_session("alice")

    draft = service.process_message("alice", session["session_id"], "请按参数计算")
    assert draft["needs_confirmation"] is True
    assert draft["parameters"]["heat_load"] == 200.0
    assert draft["parameters"] == {
        **defaulted_parameters(model),
        "heat_load": 200.0,
        "fuel_hydrogen_content": 15.3,
        "target_exhaust_temperature": 85.0,
    }
    assert draft["missing_required"] == []

    job = service.confirm("alice", session["session_id"])
    assert job["status"] == "QUEUED"
    assert job["parameters"] == draft["parameters"]
    assert job["model_snapshot"]["model"]["id"] == model.model.id


def test_heat_load_alone_is_enough_optional_inputs_use_template_defaults(tmp_path: Path):
    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")
    service = ParameterService(storage, model, FakeLLM({"heat_load": 240}))
    session = service.create_session("alice")

    draft = service.process_message("alice", session["session_id"], "只提供一个参数")
    assert draft["needs_confirmation"] is True
    assert draft["missing_required"] == []
    expected = {**defaulted_parameters(model), "heat_load": 240.0}
    assert draft["parameters"] == expected
    assert {item["key"] for item in draft["confirmation_summary"]} == set(expected)

    job = service.confirm("alice", session["session_id"])
    assert job["parameters"] == expected


def test_missing_required_heat_load_prevents_confirmation(tmp_path: Path):
    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")
    service = ParameterService(storage, model, FakeLLM({}))
    session = service.create_session("alice")

    draft = service.process_message("alice", session["session_id"], "按 Excel 默认参数计算")
    assert draft["needs_confirmation"] is False
    assert draft["missing_required"] == ["heat_load"]


def test_natural_language_alias_is_normalized_and_user_value_wins(tmp_path: Path):
    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")
    storage.set_preferences("alice", {"heat_load": 100})
    service = ParameterService(storage, model, FakeLLM({"热负载": 240}))
    session = service.create_session("alice")

    draft = service.process_message("alice", session["session_id"], "热负荷 240MW")
    assert draft["needs_confirmation"] is True
    expected = {**defaulted_parameters(model), "heat_load": 240.0}
    assert draft["parameters"] == expected

    job = service.confirm("alice", session["session_id"])
    assert job["parameters"] == expected
    assert job["model_snapshot"]["schema_version"] == "2.0"


def test_bound_session_skips_router_and_extracts_with_bound_model(tmp_path: Path):
    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")
    llm = FakeLLM({"heat_load": 240, "fuel_hydrogen_content": 15.3})
    router = FakeRouter(ModelRoute(intent="calculate", model_id=model.model.id))
    service = ParameterService(storage, model, llm, router)
    session = service.create_session("alice")

    draft = service.process_message(
        "alice", session["session_id"], "帮我算空气预热器，热负荷240MW，氢气含量15.3%"
    )

    assert router.calls == []
    assert draft["parameters"] == {
        **defaulted_parameters(model),
        "heat_load": 240.0,
        "fuel_hydrogen_content": 15.3,
    }


def test_router_clarification_does_not_extract_or_change_session(tmp_path: Path):
    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")
    llm = FakeLLM({"heat_load": 999})
    router = FakeRouter(ModelRoute(
        intent="clarification_required",
        candidate_models=[model.model.id],
        clarification="请确认需要进行哪一种计算",
    ))
    service = ParameterService(storage, model, llm, router)
    session = storage.create_session("alice", None)
    storage.update_session(session["session_id"], "alice", {"heat_load": 100.0}, pending=True)

    response = service.process_message("alice", session["session_id"], "帮我算一下预热器")

    unchanged = storage.get_session(session["session_id"], "alice")
    assert response["clarification"] == "请确认需要进行哪一种计算"
    assert response["needs_confirmation"] is False
    assert unchanged["parameters"] == {"heat_load": 100.0}
    assert unchanged["pending_confirmation"] is False
    assert unchanged["model_id"] is None


def test_invalid_extraction_response_does_not_mutate_session(tmp_path: Path):
    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    storage = Storage(tmp_path / "jobs.sqlite3")

    class InvalidLLM(FakeLLM):
        def extract_parameters(self, model, user_message, current_values):
            raise LLMOutputInvalid("invalid JSON")

    llm = InvalidLLM({})
    router = FakeRouter(ModelRoute(intent="calculate", model_id=model.model.id))
    service = ParameterService(storage, model, llm, router)
    session = service.create_session("alice")
    storage.update_session(session["session_id"], "alice", {"heat_load": 120.0}, pending=True)

    with pytest.raises(LLMOutputInvalid):
        service.process_message("alice", session["session_id"], "更新参数")

    unchanged = storage.get_session(session["session_id"], "alice")
    assert unchanged["parameters"] == {"heat_load": 120.0}
    assert unchanged["pending_confirmation"] is False
