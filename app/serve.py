"""启动 API 的推荐入口。

为什么要有这个文件：**监听地址必须和「有没有口令」一起被检查**。
用 `uvicorn ... --host 0.0.0.0` 手写命令时，这句检查没地方写，只能靠人记得。

    # 只给本机用（默认）
    .\\.venv\\Scripts\\python.exe -m app.serve

    # 对外可用：先设口令，再绑到所有网卡
    $env:EXCEL_API_AUTH = "tester:一串24位以上的随机口令"
    $env:EXCEL_API_HOST = "0.0.0.0"
    .\\.venv\\Scripts\\python.exe -m app.serve

绑到非「仅本机」地址却没设口令 → 直接拒绝启动（见 app/security.py 的说明）。
"""

from __future__ import annotations

import uvicorn

from .config import get_settings
from .security import ApiAuth, bind_host_is_local, load_api_auth

_REFUSAL = """\
拒绝启动：监听地址 {host!r} 会让本机以外的人访问这个 API，但没有配置访问口令。

这个服务没有别的身份认证，而且能：
  · 触发本机 Excel 计算（占用你的桌面与 CPU）
  · 读取并下载任意用户的任务、报告
  · 消耗服务端配置的 DeepSeek 额度

请先设置口令（多个账号用逗号分隔）：

    $env:EXCEL_API_AUTH = "tester:{example}"
    .\\.venv\\Scripts\\python.exe -m app.serve

只想本机用，就不要设 EXCEL_API_HOST（默认为 127.0.0.1）。
"""


def resolve_bind(host: str, port: int, auth: ApiAuth) -> tuple[str, int]:
    """决定实际监听地址。远程可达却没有口令时拒绝启动。"""
    if not 1 <= port <= 65535:
        raise SystemExit(f"EXCEL_API_PORT 不合法：{port}")
    if not bind_host_is_local(host) and not auth:
        raise SystemExit(_REFUSAL.format(host=host, example="换成你自己的随机口令"))
    return host, port


def _announce(host: str, port: int, auth: ApiAuth) -> None:
    """把监听状态与风险提示打到终端。

    必须 `flush=True`：stdout 在非 TTY（比如重定向到日志文件）下是块缓冲的，
    不加这一句，这几行警告要等到进程退出才出现——那还不如不打印。
    """
    if bind_host_is_local(host):
        print(f"API 监听 {host}:{port}  ·  口令认证：未启用（只有本机能访问）", flush=True)
        return

    accounts = "、".join(auth.usernames)
    print(
        f"API 监听 {host}:{port}  ·  口令认证：已启用（{len(auth.credentials)} 个账号：{accounts}）",
        flush=True,
    )
    print("⚠️  本进程本身不提供 HTTPS。", flush=True)
    print(
        "    若经「公网IP:端口」暴露，口令会明文经过网络；请改为经由隧道 / 反向代理访问。",
        flush=True,
    )


def main() -> None:
    settings = get_settings()
    try:
        auth = load_api_auth(settings.api_auth)
    except ValueError as exc:
        raise SystemExit(f"EXCEL_API_AUTH 配置有误：{exc}") from exc

    host, port = resolve_bind(settings.api_host, settings.api_port, auth)
    _announce(host, port, auth)

    uvicorn.run("app.api:app", host=host, port=port)


if __name__ == "__main__":
    main()
