from pathlib import Path
from types import SimpleNamespace

from app.domain import CalculationIntent, ModelRoute, ModelDefinition
from app.service import ParameterService
from app.storage import Storage

from test_multiple_model_resolution import (
    BOILER_ID,
    THERMAL_ID,
    artifact_template,
    boiler_definition_payload,
    build_resolver,
    ordered_artifact_templates,
    thermal_config,
)


class DispatchRouter:
    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []
        self.explicit = []

    def route(self, message):
        self.calls.append(message)
        return self.routes.pop(0)

    def validate_explicit(self, model_id):
        self.explicit.append(model_id)


class DispatchLLM:
    def __init__(self, values_by_model):
        self.values_by_model = {key: list(value) for key, value in values_by_model.items()}
        self.extracted_models = []

    def extract_parameters(self, model, message, current_values):
        self.extracted_models.append((model.model.id, set(model.parameters)))
        values = self.values_by_model[model.model.id].pop(0)
        return {"parameters": values, "clarification": None}


def service_for(tmp_path, model_id=None, routes=(), outputs=None):
    resolver, catalog = build_resolver(tmp_path)
    model = resolver.get(model_id) if model_id else None
    storage = Storage(tmp_path / "jobs.sqlite3")
    router = DispatchRouter(routes)
    llm = DispatchLLM(outputs or {})
    service = ParameterService(storage, model, llm, router, resolver)
    return service, storage, router, llm, resolver, catalog


def make_complete(service, storage, user_id, session_id, message):
    draft = service.process_message(user_id, session_id, message)
    assert draft["needs_confirmation"] is True
    job = service.confirm(user_id, session_id)
    return draft, job


def defaulted_parameters(definition) -> dict:
    """带 missing.policy="default" 的参数（例如“是否继续空气预热器计算”）会被服务层自动补上默认值。"""
    return {
        key: spec.missing.value
        for key, spec in definition.parameters.items()
        if spec.missing.policy == "default"
    }


def assert_definition_matches_config(definition, payload):
    """定义必须与配置对账。

    参数、结果、模板与 handoff 清单都会随业务扩展而变，测试不抄写它们，
    改为直接与配置文件比对——这样「加参数/加工作簿」不会变成改测试的活儿，
    同时仍能抓住加载器丢字段或两个模型串味这类真实缺陷。
    """
    assert definition.model.id == payload["model"]["id"]
    assert set(definition.parameters) == set(payload["parameters"])
    assert set(definition.results) == set(payload["results"])
    assert [step.artifact for step in definition.ordered_steps()] == [
        step["artifact"] for step in payload["workflow"]["steps"]
    ]
    assert {
        definition.artifacts[step.artifact].template for step in definition.ordered_steps()
    } == set(ordered_artifact_templates(payload))
    assert [(handoff.source, handoff.target) for handoff in definition.handoffs] == [
        (handoff["source"], handoff["target"]) for handoff in payload["handoffs"]
    ]


def assert_thermal_definition(definition):
    assert_definition_matches_config(definition, thermal_config())


def assert_boiler_definition(definition):
    assert_definition_matches_config(definition, boiler_definition_payload())
    assert "heat_load" not in definition.parameters


def test_thermal_first_calculation_resolves_parameters_workflow_artifacts_and_handoffs(tmp_path):
    service, storage, router, llm, resolver, _ = service_for(
        tmp_path,
        routes=[ModelRoute(intent="calculate", model_id=THERMAL_ID)],
        outputs={THERMAL_ID: [{"heat_load": 240}]},
    )
    session = service.create_session("alice")

    draft, job = make_complete(
        service, storage, "alice", session["session_id"],
        "帮我算一下空气预热器，热负荷240MW",
    )
    persisted = storage.get_session(session["session_id"], "alice")
    definition = resolver.get(persisted["model_id"])

    assert router.calls == ["帮我算一下空气预热器，热负荷240MW"]
    assert persisted["model_id"] == THERMAL_ID
    assert draft["parameters"] == {**defaulted_parameters(definition), "heat_load": 240.0}
    assert llm.extracted_models[0][0] == THERMAL_ID
    assert_thermal_definition(definition)
    assert job["model_id"] == THERMAL_ID
    assert job["model_snapshot"]["model"]["id"] == THERMAL_ID
    assert job["model_snapshot"]["artifacts"] == definition.model_dump(mode="json")["artifacts"]


def test_thermal_followup_uses_locked_model_and_same_workflow_artifacts(tmp_path):
    service, storage, router, llm, resolver, _ = service_for(
        tmp_path,
        routes=[ModelRoute(intent="calculate", model_id=THERMAL_ID)],
        outputs={THERMAL_ID: [{"heat_load": 240}, {"heat_load": 260}]},
    )
    session = service.create_session("alice")
    service.process_message("alice", session["session_id"], "空气预热器热负荷240MW")

    changed = service.process_message("alice", session["session_id"], "把热负荷改成260MW")
    locked = storage.get_session(session["session_id"], "alice")
    definition = resolver.get(locked["model_id"])

    assert len(router.calls) == 1
    assert locked["model_id"] == THERMAL_ID
    assert changed["parameters"]["heat_load"] == 260.0
    assert llm.extracted_models[-1][0] == THERMAL_ID
    assert_thermal_definition(definition)


def test_boiler_first_calculation_uses_only_boiler_definition(tmp_path):
    boiler_values = {
        "fuel_consumption": 1000,
        "lower_heating_value": 4000,
        "steam_output": 500,
    }
    service, storage, router, llm, resolver, _ = service_for(
        tmp_path,
        routes=[ModelRoute(intent="calculate", model_id=BOILER_ID)],
        outputs={BOILER_ID: [boiler_values]},
    )
    session = service.create_session("alice")

    draft, job = make_complete(
        service, storage, "alice", session["session_id"],
        "帮我计算锅炉热效率，燃料消耗1000，低位发热量4000，蒸汽产量500",
    )
    persisted = storage.get_session(session["session_id"], "alice")
    definition = resolver.get(persisted["model_id"])

    assert router.calls
    assert persisted["model_id"] == BOILER_ID
    assert set(draft["parameters"]) == set(boiler_values)
    assert llm.extracted_models[-1] == (BOILER_ID, set(boiler_values))
    assert_boiler_definition(definition)
    thermal_templates = {
        artifact_template(artifact) for artifact in thermal_config()["artifacts"].values()
    }
    assert all(
        artifact.template not in thermal_templates
        for artifact in definition.artifacts.values()
    )
    assert job["model_id"] == BOILER_ID
    assert job["model_snapshot"]["model"]["id"] == BOILER_ID


def test_parallel_sessions_keep_parameters_workflows_and_artifacts_isolated(tmp_path):
    service, storage, router, llm, resolver, _ = service_for(
        tmp_path,
        routes=[
            ModelRoute(intent="calculate", model_id=THERMAL_ID),
            ModelRoute(intent="calculate", model_id=BOILER_ID),
        ],
        outputs={
            THERMAL_ID: [{"heat_load": 240}],
            BOILER_ID: [{"fuel_consumption": 1000, "lower_heating_value": 4000, "steam_output": 500}],
        },
    )
    thermal_session = service.create_session("alice")
    boiler_session = service.create_session("alice")

    thermal = service.process_message("alice", thermal_session["session_id"], "算空气预热器热负荷240MW")
    boiler = service.process_message("alice", boiler_session["session_id"], "算锅炉热效率")
    thermal_state = storage.get_session(thermal_session["session_id"], "alice")
    boiler_state = storage.get_session(boiler_session["session_id"], "alice")

    assert thermal_state["model_id"] == THERMAL_ID
    assert boiler_state["model_id"] == BOILER_ID
    assert set(thermal["parameters"]) == {"heat_load"} | set(
        defaulted_parameters(resolver.get(THERMAL_ID))
    )
    assert set(boiler["parameters"]) == set(boiler_definition_payload()["parameters"])
    assert_thermal_definition(resolver.get(thermal_state["model_id"]))
    assert_boiler_definition(resolver.get(boiler_state["model_id"]))
    assert [item[0] for item in llm.extracted_models] == [THERMAL_ID, BOILER_ID]
    assert len(router.calls) == 2


def test_model_switch_clears_old_parameters_and_uses_new_definition_workflow_artifacts(tmp_path):
    service, storage, router, llm, resolver, _ = service_for(
        tmp_path,
        routes=[
            ModelRoute(intent="calculate", model_id=THERMAL_ID),
            ModelRoute(intent="calculate", model_id=BOILER_ID),
        ],
        outputs={
            THERMAL_ID: [{"heat_load": 240}],
            BOILER_ID: [{"fuel_consumption": 1000, "lower_heating_value": 4000, "steam_output": 500}],
        },
    )
    session = service.create_session("alice")
    service.process_message("alice", session["session_id"], "空气预热器热负荷240MW")

    switched = service.process_message(
        "alice", session["session_id"], "现在改为计算锅炉热效率"
    )
    state = storage.get_session(session["session_id"], "alice")
    definition = resolver.get(state["model_id"])

    assert len(router.calls) == 2
    assert state["model_id"] == BOILER_ID
    assert "heat_load" not in state["parameters"]
    assert set(switched["parameters"]) == {
        "fuel_consumption", "lower_heating_value", "steam_output"
    }
    assert llm.extracted_models[-1][0] == BOILER_ID
    assert_boiler_definition(definition)


def test_explicit_model_id_bypasses_router_and_uses_boiler_definition(tmp_path):
    boiler_values = {
        "fuel_consumption": 1000,
        "lower_heating_value": 4000,
        "steam_output": 500,
    }
    service, storage, router, llm, resolver, _ = service_for(
        tmp_path,
        outputs={BOILER_ID: [boiler_values]},
    )
    session = service.create_session("alice")

    result = service.process_message(
        "alice", session["session_id"], "请计算这项业务", explicit_model_id=BOILER_ID
    )
    state = storage.get_session(session["session_id"], "alice")
    definition = resolver.get(state["model_id"])

    assert router.calls == []
    assert router.explicit == [BOILER_ID]
    assert state["model_id"] == BOILER_ID
    assert set(result["parameters"]) == set(boiler_values)
    assert llm.extracted_models[-1][0] == BOILER_ID
    assert_boiler_definition(definition)


def test_dangling_binding_triggers_an_explicit_reroute_not_a_silent_fallback(tmp_path):
    """会话绑定的模型不存在时，必须**显式重新路由**，而不是悄悄换成别的定义。

    这是接手一次配置改名后的老会话时的正确恢复路径：不是静默兜底，
    而是把这句话重新交给路由器判定（路由器选谁就是谁），并把会话重新绑定。
    """
    service, storage, router, llm, _, _ = service_for(
        tmp_path,
        routes=[ModelRoute(intent="calculate", model_id=BOILER_ID)],
        outputs={BOILER_ID: [{"fuel_consumption": 1200}]},
    )
    session = storage.create_session("alice", "not-existing-model")

    draft = service.process_message("alice", session["session_id"], "随便计算")

    # 路由器被明确咨询过——这就是「不是静默兜底」的证据
    assert router.calls == ["随便计算"]
    assert draft["model_id"] == BOILER_ID
    assert storage.get_session(session["session_id"], "alice")["model_id"] == BOILER_ID


def test_task_snapshot_dispatches_boiler_definition_not_global_thermal(tmp_path, monkeypatch):
    import app.worker as worker

    boiler_values = {
        "fuel_consumption": 1000,
        "lower_heating_value": 4000,
        "steam_output": 500,
    }
    service, storage, router, _, resolver, _ = service_for(
        tmp_path,
        model_id=BOILER_ID,
        outputs={BOILER_ID: [boiler_values]},
    )
    session = service.create_session("alice")
    service.process_message("alice", session["session_id"], "锅炉计算")
    job = service.confirm("alice", session["session_id"])
    assert job["model_id"] == BOILER_ID

    observed = {}

    def fake_execute_model(model, parameters, **kwargs):
        observed["model"] = model
        observed["parameters"] = parameters
        observed["workflow"] = [step.artifact for step in model.ordered_steps()]
        observed["templates"] = [model.artifacts[step.artifact].template for step in model.ordered_steps()]
        observed["bindings"] = model.bindings
        return {"boiler_efficiency": 88.5}, []

    class FakeLLM:
        def __init__(self, settings):
            pass

        def summarize_results(self, model, parameters, results):
            observed["report_model_id"] = model.model.id
            return "mock summary"

    def fake_generate_report(path, model, parameters, results, summary, job_id):
        observed["report_model_id"] = model.model.id
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock docx")
        return path

    def fake_export_word_preview(path, preview_path):
        preview_path.write_bytes(b"%PDF-mock")
        return preview_path

    monkeypatch.setattr(worker, "execute_model", fake_execute_model)
    monkeypatch.setattr(worker, "DeepSeekClient", FakeLLM)
    monkeypatch.setattr(worker, "generate_report", fake_generate_report)
    monkeypatch.setattr(worker, "export_word_preview", fake_export_word_preview)
    monkeypatch.setattr(worker, "require_interactive_windows_session", lambda: 1)

    artifact_root = tmp_path / "artifacts"
    private_root = tmp_path / "private"
    settings = SimpleNamespace(
        artifact_root=artifact_root,
        private_workbook_root=private_root,
        template_root=tmp_path / "templates",
    )
    (settings.template_root).mkdir()

    assert worker.run_once(storage, settings, "fake-worker") is True
    persisted_job = storage.get_job(job["job_id"], "alice")

    assert observed["model"].model.id == BOILER_ID
    assert set(observed["parameters"]) == set(boiler_values)
    assert observed["workflow"] == ["boiler_calculation"]
    assert observed["templates"] == ["boiler-efficiency.xlsx"]
    assert set(observed["bindings"]) == {"boiler_calculation"}
    assert observed["report_model_id"] == BOILER_ID
    assert persisted_job["model_snapshot"]["model"]["id"] == BOILER_ID
    assert persisted_job["model_id"] == BOILER_ID
    assert (
        resolver.get(THERMAL_ID).artifacts["energy_saving_calc"].template
        == artifact_template(thermal_config()["artifacts"]["energy_saving_calc"])
    )


def test_thermal_job_snapshot_keeps_two_artifacts_and_business_handoffs(tmp_path):
    service, storage, router, _, resolver, _ = service_for(
        tmp_path,
        model_id=THERMAL_ID,
        outputs={THERMAL_ID: [{"heat_load": 240}]},
    )
    session = service.create_session("alice")
    service.process_message("alice", session["session_id"], "热负荷240MW")
    job = service.confirm("alice", session["session_id"])
    model = ModelDefinition.model_validate(job["model_snapshot"])

    assert job["model_id"] == THERMAL_ID
    assert_definition_matches_config(model, thermal_config())
    assert resolver.get(job["model_id"]).model.id == model.model.id


def test_worker_rejects_task_model_id_that_conflicts_with_snapshot(tmp_path, monkeypatch):
    import app.worker as worker

    service, storage, _, _, _, _ = service_for(
        tmp_path,
        model_id=BOILER_ID,
        outputs={BOILER_ID: [{
            "fuel_consumption": 1000,
            "lower_heating_value": 4000,
            "steam_output": 500,
        }]},
    )
    session = service.create_session("alice")
    service.process_message("alice", session["session_id"], "锅炉计算")
    job = service.confirm("alice", session["session_id"])
    with storage._connection() as db:
        db.execute("UPDATE jobs SET model_id=? WHERE job_id=?", (THERMAL_ID, job["job_id"]))

    def forbidden_executor(*args, **kwargs):
        raise AssertionError("Executor must not run for a mismatched task snapshot")

    monkeypatch.setattr(worker, "execute_model", forbidden_executor)
    settings = SimpleNamespace(
        artifact_root=tmp_path / "artifacts",
        private_workbook_root=tmp_path / "private",
        template_root=tmp_path / "templates",
    )

    assert worker.run_once(storage, settings, "fake-worker") is True
    failed = storage.get_job(job["job_id"], "alice")
    assert failed["status"] == "FAILED"
    assert THERMAL_ID in failed["error"]["message"]
    assert BOILER_ID in failed["error"]["message"]
    assert "Snapshot ID" in failed["error"]["message"]
