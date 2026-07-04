"""router.py — 封装 route_engine.py 调用的工具模块。"""
import json
import logging
import shlex
import subprocess

logger = logging.getLogger("route-router")

ROUTE_ENGINE_PYTHON = "/opt/hermes/.venv/bin/python3"
ROUTE_ENGINE_SCRIPT = "/opt/data/scripts/route_engine.py"
ROUTE_ENGINE_CWD = "/opt/data"

TIMEOUT_SECONDS = 5


def run_route_engine(user_input: str) -> dict | None:
    """
    调用 route_engine.py 对用户输入进行路由判断。

    使用 args list 模式调用 subprocess.run，避免 shell 注入风险。
    超时 5 秒，解析 stdout 的 JSON 输出，失败返回 None。

    Args:
        user_input: 用户消息文本。

    Returns:
        路由引擎返回的 dict，失败时返回 None。
    """
    if not user_input or not user_input.strip():
        return None

    args = [
        ROUTE_ENGINE_PYTHON,
        ROUTE_ENGINE_SCRIPT,
        user_input,  # subprocess 的 args list 模式自动 escape，无注入风险
    ]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            cwd=ROUTE_ENGINE_CWD,
        )
    except subprocess.TimeoutExpired:
        logger.error("route_engine 调用超时（%s 秒）", TIMEOUT_SECONDS)
        return None
    except FileNotFoundError:
        logger.error("route_engine 可执行文件或脚本不存在: %s", args[:2])
        return None
    except OSError as exc:
        logger.error("route_engine 调用系统错误: %s", exc)
        return None

    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        logger.error(
            "route_engine 返回非零退出码 %s: %s",
            result.returncode,
            stderr,
        )
        return None

    if not result.stdout or not result.stdout.strip():
        logger.warning("route_engine 输出为空")
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.error("route_engine 输出 JSON 解析失败: %s", exc)
        return None

    if not isinstance(data, dict):
        logger.error("route_engine 输出不是 dict: type=%s", type(data).__name__)
        return None

    return data
