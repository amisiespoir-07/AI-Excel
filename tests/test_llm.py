from types import SimpleNamespace

import pytest

from app.domain import ModelRegistryEntry
from app.llm import DeepSeekClient, LLMOutputInvalid


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def fake_client(content):
    completions = FakeCompletions(content)
    client = DeepSeekClient(SimpleNamespace(deepseek_model="test"))
    client._client = lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return client, completions


def test_router_uses_business_only_candidate_payload_and_validates_route_schema():
    client, completions = fake_client(
        '{"intent":"calculate","model_id":"model_energy_saving_calc"}'
    )
    candidate = ModelRegistryEntry(
        id="model_energy_saving_calc",
        name="空气预热器工艺计算",
        description="用于空气预热器工艺性能计算",
        keywords=["空气预热器", "热负荷"],
        category="thermal",
        enabled=True,
    )

    route = client.route_model("帮我算空气预热器", [candidate])

    assert route.model_id == "model_energy_saving_calc"
    messages = completions.calls[0]["messages"]
    assert "Never invent a model ID" in messages[0]["content"]
    assert "Never extract calculation parameters" in messages[0]["content"]
    candidate_payload = messages[1]["content"]
    for forbidden in ("B11", "T11", "G74", "S63", "U63", "AA14", "AO14",
                      "cell_ref", "sheet", "template", "artifact", ".xlsm"):
        assert forbidden.casefold() not in candidate_payload.casefold()
    assert '"id"' in candidate_payload
    assert '"keywords"' in candidate_payload


@pytest.mark.parametrize("model_id", ["B11", "T11", "工艺计算!B11", "节能减排计算.xlsm"])
def test_router_schema_rejects_excel_cell_or_filename_as_model_id(model_id):
    client, _ = fake_client(
        '{"intent":"calculate","model_id":' + __import__("json").dumps(model_id, ensure_ascii=False) + '}'
    )
    candidate = ModelRegistryEntry(
        id="model_energy_saving_calc",
        name="空气预热器工艺计算",
        description="工艺计算",
        category="thermal",
    )
    with pytest.raises(LLMOutputInvalid):
        client.route_model("帮我计算", [candidate])


def test_router_payload_carries_parameter_hints_but_still_no_excel_details():
    """参数提法是路由判别力的来源；但它不能把工作表/单元格一起带出去。"""
    from app.model_router import RouteCandidate

    client, completions = fake_client(
        '{"intent":"calculate","model_id":"model_air_preheater"}'
    )
    candidate = RouteCandidate(
        id="model_air_preheater",
        name="空气预热器工艺计算（独立）",
        description="只做空气预热器计算",
        keywords=("烟气量",),
        category="thermal",
        parameter_hints=("烟气流量", "烟气量", "空气流量"),
    )

    client.route_model("预热器工艺计算，烟气量6500kg/h", [candidate])

    payload = completions.calls[0]["messages"][1]["content"]
    assert '"parameter_hints"' in payload
    assert "烟气量" in payload
    assert "空气流量" in payload
    for forbidden in ("B11", "AA14", "sheet", "cell", "template", ".xlsm"):
        assert forbidden.casefold() not in payload.casefold()


def test_parameter_extraction_returns_pydantic_validated_business_output():
    client, _ = fake_client(
        '{"parameters":{"heat_load":240,"fuel_hydrogen_content":15.3},"clarification":null}'
    )
    # The call only needs model metadata; load the real business definition.
    from pathlib import Path
    from app.domain import load_model_definition

    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    extracted = client.extract_parameters(model, "热负荷240MW，氢气含量15.3%", {})

    assert extracted.parameters == {"heat_load": 240, "fuel_hydrogen_content": 15.3}
    assert extracted.clarification is None


def test_parameter_extraction_rejects_invalid_json_and_extra_model_choice():
    from pathlib import Path
    from app.domain import load_model_definition

    model = load_model_definition(Path(__file__).resolve().parents[1] / "config" / "models" / "model_energy_saving_calc" / "model.json")
    for output in (
        "this is not json",
        '{"parameters":{},"clarification":null,"model_id":"model_energy_saving_calc"}',
    ):
        client, _ = fake_client(output)
        with pytest.raises(LLMOutputInvalid):
            client.extract_parameters(model, "算一下", {})
