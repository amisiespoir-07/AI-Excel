"""API 访问控制：口令认证 + 「远程可达却没有口令」的兜底拦截。

背景：这个服务能驱动本机 Excel，而数据归属只靠一个请求头（`X-User-ID`）区分——
那不是凭据。所以规则是 **远程可达 + 没有口令 = 不允许**，由代码强制。

这些测试同时守住一个容易忽略的边界：**口令必须决定身份**。
否则拿到口令的人只要把 `X-User-ID` 改成别人的名字，就能读别人的数据。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api as api_module
from app.security import (
    ApiAuth,
    bind_host_is_local,
    host_header_is_local,
    load_api_auth,
    parse_basic_authorization,
)
from app.serve import resolve_bind
from app.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
LOCAL_BASE = "http://127.0.0.1:8000"
REMOTE_BASE = "http://192.168.0.107:8000"


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #

def test_empty_configuration_means_auth_is_off():
    assert not load_api_auth(None)
    assert not load_api_auth("")
    assert not load_api_auth("   ")


def test_multiple_accounts_are_parsed():
    auth = load_api_auth("alice:pw1, bob:pw2")

    assert auth.usernames == ["alice", "bob"]
    assert auth.authenticate(basic("alice", "pw1")["Authorization"]) == "alice"
    assert auth.authenticate(basic("bob", "pw2")["Authorization"]) == "bob"
    assert auth.authenticate(basic("alice", "pw2")["Authorization"]) is None


@pytest.mark.parametrize("raw", ["justapassword", "alice:", ":pw", "alice:pw," + "broken"])
def test_malformed_configuration_fails_loudly(raw):
    """写错必须报错——静默忽略就把「以为有口令」变成「其实没有口令」。"""
    with pytest.raises(ValueError):
        load_api_auth(raw)


def test_non_ascii_username_is_rejected():
    """用户名会作为 X-User-ID 使用，必须是 ASCII。"""
    with pytest.raises(ValueError, match="ASCII"):
        load_api_auth("张三:pw")


def test_basic_authorization_parsing():
    assert parse_basic_authorization('Basic ' + base64.b64encode(b"u:p").decode()) == ("u", "p")
    assert parse_basic_authorization(None) is None
    assert parse_basic_authorization("Bearer abc") is None
    assert parse_basic_authorization("Basic not-base64!!") is None
    assert parse_basic_authorization("Basic " + base64.b64encode(b"nocolon").decode()) is None


# --------------------------------------------------------------------------- #
# 本机判定
# --------------------------------------------------------------------------- #

def test_bind_host_and_host_header_have_different_meanings():
    """0.0.0.0 作为**监听地址**意味着所有网卡；作为**Host** 只可能是本机直连。"""
    assert bind_host_is_local("127.0.0.1") is True
    assert bind_host_is_local("localhost") is True
    assert bind_host_is_local("0.0.0.0") is False
    assert bind_host_is_local("192.168.0.107") is False

    assert host_header_is_local("127.0.0.1") is True
    assert host_header_is_local("0.0.0.0") is True
    assert host_header_is_local("testserver") is True      # Starlette TestClient
    assert host_header_is_local("192.168.0.107") is False
    assert host_header_is_local("xxx.trycloudflare.com") is False
    assert host_header_is_local(None) is False


# --------------------------------------------------------------------------- #
# 启动期：远程可达却没有口令 → 拒绝启动
# --------------------------------------------------------------------------- #

def test_binding_to_all_interfaces_without_a_password_refuses_to_start():
    with pytest.raises(SystemExit) as excinfo:
        resolve_bind("0.0.0.0", 8000, ApiAuth({}))

    message = str(excinfo.value)
    assert "拒绝启动" in message
    assert "EXCEL_API_AUTH" in message          # 告诉运维具体怎么修


def test_local_binding_without_a_password_is_allowed():
    assert resolve_bind("127.0.0.1", 8000, ApiAuth({})) == ("127.0.0.1", 8000)


def test_remote_binding_with_a_password_is_allowed():
    auth = load_api_auth("alice:pw")
    assert resolve_bind("0.0.0.0", 9000, auth) == ("0.0.0.0", 9000)


def test_invalid_port_refuses_to_start():
    with pytest.raises(SystemExit, match="EXCEL_API_PORT"):
        resolve_bind("127.0.0.1", 70000, ApiAuth({}))


# --------------------------------------------------------------------------- #
# 请求期
# --------------------------------------------------------------------------- #

def test_local_access_without_auth_still_works(monkeypatch):
    monkeypatch.setattr(api_module, "API_AUTH", ApiAuth({}))

    with TestClient(api_module.app, base_url=LOCAL_BASE) as client:
        assert client.get("/health").status_code == 200


def test_remote_access_without_auth_is_refused_with_actionable_message(monkeypatch):
    """隧道场景就靠这条：cloudflared 从本机连过来，启动期看不出来。"""
    monkeypatch.setattr(api_module, "API_AUTH", ApiAuth({}))

    with TestClient(api_module.app, base_url=REMOTE_BASE) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert "EXCEL_API_AUTH" in response.json()["detail"]
    assert "charset=utf-8" in response.headers["content-type"]


def test_missing_or_wrong_password_gets_401(monkeypatch):
    monkeypatch.setattr(api_module, "API_AUTH", load_api_auth("alice:pw"))

    with TestClient(api_module.app, base_url=REMOTE_BASE) as client:
        anonymous = client.get("/health")
        wrong = client.get("/health", headers=basic("alice", "nope"))

    assert anonymous.status_code == 401
    assert "Basic" in anonymous.headers["www-authenticate"]
    assert wrong.status_code == 401


def test_static_assets_and_docs_are_also_protected(monkeypatch):
    monkeypatch.setattr(api_module, "API_AUTH", load_api_auth("alice:pw"))

    with TestClient(api_module.app, base_url=REMOTE_BASE) as client:
        for path in ("/", "/static/chat.js", "/docs", "/openapi.json"):
            assert client.get(path).status_code == 401, path


def test_correct_password_grants_access(monkeypatch):
    monkeypatch.setattr(api_module, "API_AUTH", load_api_auth("alice:pw"))

    with TestClient(api_module.app, base_url=REMOTE_BASE) as client:
        response = client.get("/health", headers=basic("alice", "pw"))

    assert response.status_code == 200


def test_password_decides_identity_and_spoofed_header_is_ignored(tmp_path, monkeypatch):
    """这条是口令方案的**关键边界**：拿到口令的人也不能冒充别人。"""
    storage = Storage(tmp_path / "auth.sqlite3")
    monkeypatch.setattr(api_module, "storage", storage)
    monkeypatch.setattr(api_module, "API_AUTH", load_api_auth("alice:pw,bob:pw2"))

    with TestClient(api_module.app, base_url=REMOTE_BASE) as client:
        # alice 认证通过，但同时伪造了一个别人的 X-User-ID
        created = client.post(
            "/api/v1/sessions",
            json={},
            headers={**basic("alice", "pw"), "X-User-ID": "bob"},
        )
    assert created.status_code == 201
    session_id = created.json()["session_id"]

    # 会话必须属于 alice（口令里的身份），而不是 bob（请求头里的）
    assert storage.get_session(session_id, "alice") is not None
    assert storage.get_session(session_id, "bob") is None

    with TestClient(api_module.app, base_url=REMOTE_BASE) as client:
        # bob 用自己的合法口令，也打不开 alice 的会话（在调用大模型之前就被挡住）
        response = client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"message": "热负荷240MW"},
            headers=basic("bob", "pw2"),
        )

    assert response.status_code == 404
    assert "会话不存在" in response.text
