"""部署冒烟：以 server 模式启动真实进程，验证访问边界与优雅停机。

不属于 unittest 自动发现范围，由 CI 显式调用：
    python -X utf8 tests/smoke_deploy.py --mode process
    python -X utf8 tests/smoke_deploy.py --mode docker --image relay-price:smoke

全程只用合成配置（空站点、关闭调度），不会向任何站点发出请求。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
USERNAME = "smoke-user"
PASSWORD = "smoke-password-123"


def free_port() -> int:
    """让系统分配一个空闲端口；真正的独占绑定仍由被测进程完成。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def call(port: int, method: str, path: str, payload=None, headers=None, cookie=""):
    """发一个请求并返回 (status, headers, body)；4xx/5xx 也读取响应体。"""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    merged = dict(headers or {})
    if body is not None:
        merged.setdefault("Content-Type", "application/json")
    if cookie:
        merged["Cookie"] = cookie
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=merged)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def wait_ready(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _headers, _body = call(port, "GET", "/healthz")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError("服务在预算内没有就绪")


def prepare_state(state_dir: Path, *, shared: bool = False) -> None:
    """写合成状态：空站点、关闭调度、已初始化认证。

    shared=True 供容器模式使用：目录放开写权限，让镜像内非 root 用户能落盘；
    auth.json 仍保持 0600（服务端在 POSIX 上会拒绝更宽松的权限）。
    """
    sys.path.insert(0, str(ROOT))
    from integrations.auth import AuthState, AuthStore, hash_password

    state_dir.mkdir(parents=True, exist_ok=True)
    if shared and hasattr(os, "chmod"):
        os.chmod(state_dir, 0o777)
    (state_dir / "sites.json").write_text('{"sites": []}\n', encoding="utf-8")
    (state_dir / "settings.json").write_text(
        json.dumps({"schedule": {"enabled": False, "time": "04:30",
                                 "refresh_accounts": False}}, ensure_ascii=False),
        encoding="utf-8")
    AuthStore(state_dir).save(AuthState(True, USERNAME, hash_password(PASSWORD), 1))
    if shared and hasattr(os, "chmod"):
        os.chmod(state_dir / "auth.json", 0o600)


def server_env(state_dir: Path, port: int) -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RELAY_MODE": "server",
        "RELAY_HOST": "127.0.0.1",
        "RELAY_PORT": str(port),
        "RELAY_STRICT_PORT": "1",
        "RELAY_STATE_DIR": str(state_dir),
        "RELAY_AUTH_DEFAULT_ENABLED": "1",
        "RELAY_ALLOWED_ORIGINS": "http://127.0.0.1:" + str(port),
        "RELAY_ALLOWED_HOSTS": "127.0.0.1:" + str(port) + ",localhost:" + str(port),
    })
    return env


def login(port: int) -> tuple[str, str]:
    """走完整「挑战 → 登录」流程，返回 (会话 Cookie, CSRF)。"""
    status, _headers, raw = call(port, "GET", "/api/auth/challenge")
    assert status == 200, status
    challenge = json.loads(raw)
    status, headers, raw = call(
        port, "POST", "/api/auth/login",
        {"challenge": challenge["challenge"], "csrf": challenge["csrf"],
         "username": USERNAME, "password": PASSWORD},
        {"Origin": "http://127.0.0.1:" + str(port)})
    assert status == 200 and json.loads(raw)["ok"], (status, raw)
    cookie = headers.get("Set-Cookie") or ""
    return cookie.split(";", 1)[0], json.loads(raw)["csrf"]


def assert_boundaries(port: int, state_dir: Path) -> None:
    """逐条验证设计方案 9.1 的部署边界。"""
    origin = "http://127.0.0.1:" + str(port)
    status, _headers, raw = call(port, "GET", "/healthz")
    assert status == 200 and json.loads(raw)["ok"], (status, raw)

    # 非法 Host 必须挡在业务之前（DNS 重绑定）。
    status, _headers, _raw = call(port, "GET", "/healthz", headers={"Host": "evil.example"})
    assert status == 403, status

    # 未登录：业务接口与快照都不可读。
    for path in ("/api/state", "/data/prices.js"):
        status, _headers, raw = call(port, "GET", path)
        assert status == 401, (path, status)
        assert b'"sites"' not in raw

    # 未登录的写请求先撞认证，同样不得产生副作用。
    status, _headers, _raw = call(port, "POST", "/api/settings", {"settings": {}},
                                  {"Origin": "https://evil.example"})
    assert status == 401, status

    cookie, csrf = login(port)
    status, _headers, raw = call(port, "GET", "/api/state", cookie=cookie)
    assert status == 200 and json.loads(raw)["sites"] == [], (status, raw)

    # 登录后，非法 Origin 必须在业务动作之前被拒绝。
    status, _headers, _raw = call(port, "POST", "/api/settings", {"settings": {}},
                                  {"Origin": "https://evil.example"}, cookie=cookie)
    assert status == 403, status

    # 登录后仍要求 CSRF；缺失或错误一律 403。
    status, _headers, _raw = call(port, "POST", "/api/settings", {"settings": {}},
                                  {"Origin": origin}, cookie=cookie)
    assert status == 403, status
    status, _headers, _raw = call(port, "POST", "/api/settings", {"settings": {}},
                                  {"Origin": origin, "X-Relay-CSRF": csrf}, cookie=cookie)
    assert status == 200, status

    # 静态路径穿越。
    status, _headers, _raw = call(port, "GET", "/data/..%2f..%2fsites.json", cookie=cookie)
    assert status == 403, status

    assert list((state_dir / "logs").glob("*.log")), "日志没有落到状态目录"


def run_process(state_dir: Path, port: int) -> None:
    before = sorted(item.name for item in ROOT.iterdir())
    process = subprocess.Popen([sys.executable, "-X", "utf8", "server.py",
                                "--port", str(port), "--no-schedule"],
                               cwd=ROOT, env=server_env(state_dir, port),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_ready(port)
        assert_boundaries(port, state_dir)
    finally:
        if os.name == "nt":
            # Windows 无法投递 SIGTERM，这里只验证能结束进程。
            process.kill()
            process.wait(timeout=20)
        else:
            process.terminate()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                raise AssertionError("SIGTERM 后 60 秒内没有退出")
            assert process.returncode == 0, process.returncode
        output = (process.stdout.read() or b"").decode("utf-8", "replace")
    assert "Traceback" not in output, output
    # 代码目录不得出现运行期文件（快照、日志、锁都在状态目录）。
    assert sorted(item.name for item in ROOT.iterdir()) == before


def run_docker(state_dir: Path, port: int, image: str) -> None:
    """容器模式：宿主端口映射到容器内 8765。"""
    if shutil.which("docker") is None:
        raise SystemExit("未安装 docker，无法执行容器冒烟")
    name = "relay-smoke-" + str(os.getpid())
    command = ["docker", "run", "--rm", "-d", "--name", name,
               "-p", "127.0.0.1:" + str(port) + ":8765",
               "-v", str(state_dir) + ":/var/lib/relay-price",
               "-e", "RELAY_MODE=server", "-e", "RELAY_HOST=0.0.0.0",
               "-e", "RELAY_PORT=8765", "-e", "RELAY_STRICT_PORT=1",
               "-e", "RELAY_STATE_DIR=/var/lib/relay-price",
               "-e", "RELAY_AUTH_DEFAULT_ENABLED=1",
               "-e", "RELAY_ALLOWED_ORIGINS=http://127.0.0.1:" + str(port),
               "-e", "RELAY_ALLOWED_HOSTS=127.0.0.1:" + str(port) + ",localhost:" + str(port)]
    if hasattr(os, "getuid"):
        # 用宿主当前用户运行，预置的 auth.json（0600）才读得到。
        command += ["--user", str(os.getuid()) + ":" + str(os.getgid())]
    command += [image, "python", "-X", "utf8", "server.py", "--port", "8765",
                "--no-schedule"]
    subprocess.run(command, check=True, capture_output=True, text=True)
    try:
        wait_ready(port, timeout=45)
        assert_boundaries(port, state_dir)
    finally:
        subprocess.run(["docker", "stop", "-t", "60", name], capture_output=True, text=True)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="部署冒烟（合成配置，无站点请求）")
    parser.add_argument("--mode", choices=("process", "docker"), default="process")
    parser.add_argument("--image", default="relay-price:smoke")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="relay-smoke-") as temp:
        state_dir = Path(temp) / "state"
        port = free_port()
        prepare_state(state_dir, shared=args.mode == "docker")
        if args.mode == "docker":
            run_docker(state_dir, port, args.image)
        else:
            run_process(state_dir, port)
    print(json.dumps({"ok": True, "mode": args.mode}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
