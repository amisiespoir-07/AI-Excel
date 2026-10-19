import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.api import app


client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
MODEL_CONFIG_PATH = ROOT / "config" / "models" / "model_energy_saving_calc" / "model.json"


def configured_model() -> dict:
    """API 暴露的参数/结果清单必须与配置文件对账，而不是在测试里抄一遍。"""
    return json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))


def test_chat_page_and_static_assets_are_served():
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "messageInput" in page.text
    assert "/static/chat.css" in page.text
    assert "/static/chat.js" in page.text

    css = client.get("/static/chat.css")
    script = client.get("/static/chat.js")
    assert css.status_code == 200
    assert "composer" in css.text
    assert script.status_code == 200
    assert "confirmCurrent" in script.text
    assert "pollJob" in script.text
    assert "openPdfPreview" in script.text
    assert "data-preview-url" in script.text
    assert "artifact.kind === \"pdf\"" in script.text
    assert "Boolean(artifact.preview_path)" in script.text
    assert "data-download-kind" in script.text
    assert "下载 Word" in script.text
    assert "报告预览 · 原始文件仍可单独下载" in script.text
    assert "function updateWorkflow()" in script.text
    assert "Object.entries(job.results)" not in script.text
    assert 'id="workflowSteps"' in page.text
    assert "activeModel?.workflow?.steps" in script.text
    assert "artifactDefinitions[step.artifact]?.display_name" in script.text
    assert 'id="activeParameters"' in page.text
    assert "data-input-value=\"${escapeHTML(key)}\"" in script.text
    assert "spec.display_name || key" in script.text
    assert "ACTIVE MODEL" not in page.text
    assert "百分数目前按 Excel 模板约定" not in page.text
    assert "state.currentParameters = draft.parameters" in script.text
    assert "contextPanel" in page.text
    assert "artifact-download" in css.text
    assert "pdf-frame" in css.text
    assert "仅保存在后台" in page.text


def test_root_ui_keeps_api_available():
    assert client.get("/health").status_code == 200
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    model = response.json()[0]
    assert model["schema_version"] == "2.0"
    assert set(model["parameters"]) == set(configured_model()["parameters"])
    assert "input_b11" not in model["parameters"]
    assert set(model["results"]) == set(configured_model()["results"])
    assert "bindings" not in model
    assert "B11" not in response.text
    assert "T11" not in response.text
    assert "S63" not in response.text


def test_onboarding_text_comes_from_the_model_definition_not_the_markup():
    """界面上的业务示例必须来自模型定义，不能写死在 HTML/JS 里。"""
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    model = response.json()[0]

    assert [example["title"] for example in model["examples"]] == [
        "空气预热器工艺计算",
        "查看所需计算参数",
    ]
    assert all(example["prompt"] for example in model["examples"])
    assert model["hints"] == ["热负荷 200 MW", "氢气含量 15.3%", "排烟温度 85°C"]
    # 引导文案同样不得泄露单元格地址
    assert "B11" not in response.text

    page = client.get("/")
    assert 'id="suggestionGrid"' in page.text
    assert 'id="exampleChips"' in page.text
    assert 'id="exampleCaption"' in page.text
    # 页面里不应该再有任何业务示例文案
    assert "空气预热器工艺计算" not in page.text
    assert "查看所需计算参数" not in page.text
    assert "热负荷" not in page.text
    assert "排烟温度" not in page.text

    script = client.get("/static/chat.js")
    assert "function renderModelExamples()" in script.text
    assert "activeModel.examples" in script.text
    assert "activeModel.hints" in script.text
    assert "热负荷" not in script.text


def test_models_without_examples_simply_render_nothing():
    """没有配置 examples/hints 的模型不应该回退到硬编码文案。"""
    from pathlib import Path

    from app.domain import ModelDefinition, load_model_definition

    root = Path(__file__).resolve().parents[1]
    base = load_model_definition(root / "config" / "models" / "model_energy_saving_calc" / "model.json")
    payload = base.model_dump(mode="json")
    payload.pop("examples", None)
    payload.pop("hints", None)

    derived = ModelDefinition.model_validate(payload)
    assert derived.examples == []
    assert derived.hints == []


def test_frontend_polling_is_bounded_and_explains_known_failures():
    """任务卡住或服务不可用时，前端不能无限转圈。"""
    script = client.get("/static/chat.js").text
    assert "POLL_MAX_MS" in script
    assert "POLL_MAX_FAILURES" in script
    assert "function formatDuration(" in script
    assert "JOB_ERROR_HINTS" in script
    assert "data-refresh-job" in script
    assert "已停止自动刷新" in script
    # 已知错误码要给出可操作提示，而不是把裸 HRESULT 甩给用户
    assert "EXCEL_COM_ACTIVATION_FAILED" in script
    assert "DCOM" in script

    css = client.get("/static/chat.css").text
    assert ".job-stale" in css
    assert ".error-hint" in css


def test_frontend_marks_control_parameters_and_optional_steps():
    """控制参数要显示成开关，可选步骤要标出来——否则用户看不懂「模板默认」是什么。"""
    script = client.get("/static/chat.js").text
    assert "spec.control" in script
    assert "step.optional" in script
    assert "可选 · " in script
    assert "按默认" in script


def test_confirmation_card_marks_where_each_value_came_from():
    """卡片必须区分「本轮你给的」和「沿用上次的」，否则用户会对旧值点确认。"""
    script = client.get("/static/chat.js").text
    assert "PARAMETER_SOURCE_LABELS" in script
    assert "沿用上次" in script
    assert "本轮" in script
    assert "parameter-source" in script
    assert "draft.parameter_sources" in script
    assert "本轮我没有从你的描述里识别出新的参数值" in script

    css = client.get("/static/chat.css").text
    assert ".parameter-source" in css
    assert ".parameter-source.source-input" in css


def test_frontend_never_hardcodes_how_many_workbooks_run():
    """运行几本工作簿由模型定义决定；写死「两本」在独立模型下就是错的。"""
    script = client.get("/static/chat.js").text
    for hardcoded in ("依次运行两本", "两本 Excel"):
        assert hardcoded not in script


def test_right_panel_follows_the_session_model():
    """路由切到另一个模型后，右侧面板必须跟着换，否则用户以为没切换成功。"""
    script = client.get("/static/chat.js").text
    assert "refreshModelDefinition(draft.model_id)" in script
    assert "models.find(" in script
    assert "activeModel = models[0];" not in script, "面板不能写死成第一个模型"

