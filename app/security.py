"""API 访问控制。

为什么需要它：这个服务能驱动本机 Excel 干活，而且**数据归属只靠一个请求头**
（`X-User-ID`）区分——那不是凭据。所以只要本机以外的人能连上，就等于把
机器算力和全部报告一起交出去。

由此定下一条规则，并由代码强制，而不是靠人记得：

> **远程可达 + 没有口令 = 不允许。**

两处执行：

* 启动时（`app/serve.py`）：要绑到非「仅本机」地址却没配口令 → 拒绝启动；
* 每个请求（`app/api.py` 的中间件）：没配口令却用非本机主机名访问 → 503。

第二条不只是冗余：**隧道场景下启动时看不出来**——cloudflared 从本机连过来，
绑的仍然是 `127.0.0.1`，但 Host 头会是 `xxx.trycloudflare.com`。
"""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import dataclass

#: Host 请求头里算「本机访问」的名字。
#: `0.0.0.0` 出现在 Host 里只可能是本机浏览器直连；
#: `testserver` 是 Starlette TestClient 的默认主机名。
LOCAL_HOST_HEADERS = frozenset(
    {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0", "testserver"}
)

#: 允许作为**监听地址**的「仅本机」取值。
#: 注意 `0.0.0.0` **不在**这里——它的含义正好相反：监听所有网卡。
LOCAL_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def bind_host_is_local(host: str) -> bool:
    """这个监听地址是否只对本机可见。"""
    return (host or "").strip().lower() in LOCAL_BIND_HOSTS


def host_header_is_local(hostname: str | None) -> bool:
    """请求的 Host 是否指向本机。"""
    return bool(hostname) and hostname.strip().lower() in LOCAL_HOST_HEADERS


@dataclass(frozen=True)
class ApiAuth:
    """`用户名 -> 口令` 清单。空清单表示不启用认证。"""

    credentials: dict[str, str]

    def __bool__(self) -> bool:
        return bool(self.credentials)

    @property
    def usernames(self) -> list[str]:
        return sorted(self.credentials)

    def authenticate(self, authorization: str | None) -> str | None:
        """校验 Authorization 头；成功返回用户名，失败返回 None。

        遍历全部账号且不提前退出，用户名与口令都用常量时间比较，
        避免用响应时间区分「用户名不存在」和「口令不对」。
        """
        parsed = parse_basic_authorization(authorization)
        if parsed is None:
            return None
        username, password = parsed
        matched: str | None = None
        for known_user, known_password in self.credentials.items():
            same_user = hmac.compare_digest(username, known_user)
            same_password = hmac.compare_digest(password, known_password)
            if same_user and same_password:
                matched = known_user
        return matched


def parse_basic_authorization(header: str | None) -> tuple[str, str] | None:
    """解析 `Authorization: Basic base64(用户名:口令)`。"""
    if not header:
        return None
    scheme, _, encoded = header.partition(" ")
    if scheme.strip().lower() != "basic" or not encoded.strip():
        return None
    try:
        raw = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, separator, password = raw.partition(":")
    if not separator or not username:
        return None
    return username, password


def load_api_auth(raw: str | None) -> ApiAuth:
    """解析 `EXCEL_API_AUTH`：`用户名:口令`，多个账号用逗号分隔。

    格式错误**直接报错**。静默忽略一个手误，就会把「以为有口令」变成
    「其实没有口令」——那正是这套机制要防的失败模式。
    """
    if raw is None or not raw.strip():
        return ApiAuth({})

    credentials: dict[str, str] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        username, separator, password = entry.partition(":")
        username = username.strip()
        password = password.strip()
        if not separator or not username or not password:
            raise ValueError(
                "EXCEL_API_AUTH 每一项都必须是「用户名:口令」形式，多个账号用逗号分隔；"
                f"无法解析：{entry!r}"
            )
        if not username.isascii():
            raise ValueError(
                f"EXCEL_API_AUTH 的用户名必须是 ASCII（它会作为 X-User-ID 使用）：{username!r}"
            )
        credentials[username] = password
    return ApiAuth(credentials)
