"""用户偏好接口必须按模型隔离，不能残留「只有一个模型」的假设。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from test_multiple_model_resolution import BOILER_ID, THERMAL_ID, build_resolver


def make_client(tmp_path, monkeypatch):
    import app.api as api_module
    from app.storage import Storage

    resolver, catalog = build_resolver(tmp_path)
    storage = Storage(tmp_path / "api.sqlite3")
    monkeypatch.setattr(api_module, "model_registry", resolver)
    monkeypatch.setattr(api_module, "model_catalog", catalog)
    monkeypatch.setattr(api_module, "storage", storage)
    return TestClient(api_module.app)


def test_global_and_per_model_preferences_stay_separate(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        global_write = client.put("/api/v1/preferences", json={"values": {"heat_load": 100}})
        assert global_write.status_code == 200
        assert global_write.json()["scope"] == "global"

        model_write = client.put(
            "/api/v1/preferences",
            json={"values": {"heat_load": 240}, "model_id": THERMAL_ID},
        )
        assert model_write.status_code == 200
        assert model_write.json() == {
            "scope": "model",
            "values": {"heat_load": 240.0},
            "model_id": THERMAL_ID,
        }

        thermal = client.get("/api/v1/preferences", params={"model_id": THERMAL_ID}).json()
        assert thermal["model_id"] == THERMAL_ID
        assert thermal["global"] == {"heat_load": 100.0}
        assert thermal["model"] == {"heat_load": 240.0}

        # 另一个模型有自己的参数口径，模型偏好为空但全局偏好仍可见
        boiler = client.get("/api/v1/preferences", params={"model_id": BOILER_ID}).json()
        assert boiler["model_id"] == BOILER_ID
        assert boiler["model"] == {}
        assert boiler["global"] == {"heat_load": 100.0}


def test_second_model_preferences_use_its_own_schema(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.put(
            "/api/v1/preferences",
            json={"values": {"fuel_consumption": 900}, "model_id": BOILER_ID},
        )
        assert response.status_code == 200
        assert response.json()["values"] == {"fuel_consumption": 900.0}

        boiler = client.get("/api/v1/preferences", params={"model_id": BOILER_ID}).json()
        assert boiler["model"] == {"fuel_consumption": 900.0}

        # 锅炉参数不能被写进热负荷模型的偏好
        rejected = client.put(
            "/api/v1/preferences",
            json={"values": {"fuel_consumption": 900}, "model_id": THERMAL_ID},
        )
        assert rejected.status_code == 422


def test_preferences_fall_back_to_the_first_enabled_model(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.get("/api/v1/preferences")
        assert response.status_code == 200
        assert response.json()["model_id"] == THERMAL_ID


def test_preferences_for_unknown_model_are_rejected(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        assert client.get("/api/v1/preferences", params={"model_id": "nope"}).status_code == 404
        assert (
            client.put(
                "/api/v1/preferences", json={"values": {}, "model_id": "nope"}
            ).status_code
            == 404
        )
