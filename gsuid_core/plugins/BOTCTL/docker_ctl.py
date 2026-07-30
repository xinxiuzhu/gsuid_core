"""Docker CLI 异步封装

通过 asyncio.create_subprocess_exec 调用 docker CLI，零额外依赖。
容器命名规律：{前缀}{序号}nuo{QQ号}，例如 fb20012nuo2082318370
端口映射：序号 -> 3000(napcat OneBot), 序号+20000 -> 6099(webui)
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional, List

from gsuid_core.logger import logger

# 容器名格式：{prefix}{serial}nuo{qq}  例如 fb20012nuo2082318370
# prefix 可含字母/数字，serial 是纯数字，nuo 固定分隔，qq 纯数字
# 注意 prefix 可能也含数字，用贪婪从末尾匹配最稳妥
_NAME_RE = re.compile(r"(\d+)nuo(\d+)$")


class DockerError(Exception):
    """docker 命令执行错误"""


@dataclass
class ContainerInfo:
    name: str  # 完整容器名，如 fb20012nuo2082318370
    serial: str  # 序号，如 20012
    qq: str  # 容器内登录的 QQ 号
    status: str  # docker 状态行，如 "Up 5 hours"
    running_for: str  # 如 "5 hours ago"
    webui_port: Optional[int] = None  # 宿主机 webui 端口
    napcat_port: Optional[int] = None  # 宿主机 napcat(3000) 端口
    raw_ports: str = field(default="", repr=False)


async def _run(args: list, timeout: float = 60.0) -> tuple:
    """执行 docker 命令，返回 (returncode, stdout, stderr)"""
    cmd = ["docker"] + args
    logger.debug(f"[napcat_docker] exec: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise DockerError("未找到 docker 命令，请确认 docker CLI 已安装且在 PATH 中")
    except Exception as e:
        raise DockerError(f"启动 docker 进程失败: {e}")

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise DockerError(f"docker 命令超时 ({timeout}s): {' '.join(cmd)}")

    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()
    logger.debug(
        f"[napcat_docker] exec done rc={proc.returncode} "
        f"stdout={stdout[:300]!r} stderr={stderr[:200]!r}"
    )
    return proc.returncode or 0, stdout, stderr


def _parse_port(port_str: str, container_port: int) -> Optional[int]:
    """从端口映射串里提取指定容器端口对应的宿主端口

    例如 "0.0.0.0:40012->6099/tcp" 找 6099 -> 返回 40012
    """
    pattern = re.compile(rf"(\d+)->{container_port}/tcp")
    m = pattern.search(port_str)
    if m:
        return int(m.group(1))
    return None


def _parse_name(name: str) -> tuple:
    """解析容器名 -> (serial, qq)，解析失败返回 (None, None)"""
    m = _NAME_RE.search(name)
    if m:
        return m.group(1), m.group(2)
    return None, None


async def list_containers() -> List[ContainerInfo]:
    """列出所有容器，解析名字与端口映射"""
    rc, stdout, stderr = await _run(
        [
            "ps",
            "-a",
            "--format",
            "{{.Names}}|{{.Status}}|{{.RunningFor}}|{{.Ports}}",
        ],
        timeout=15,
    )
    if rc != 0:
        raise DockerError(f"docker ps 失败: {stderr or stdout}")

    result: List[ContainerInfo] = []
    for line in stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        name = parts[0].strip()
        status = parts[1].strip()
        running_for = parts[2].strip()
        ports = parts[3].strip() if len(parts) > 3 else ""

        if not name:
            continue

        serial, qq = _parse_name(name)
        if serial is None:
            # 不是 napcat 容器命名格式，跳过
            continue

        webui_port = _parse_port(ports, 6099)
        napcat_port = _parse_port(ports, 3000)

        result.append(
            ContainerInfo(
                name=name,
                serial=serial,
                qq=qq or "",
                status=status,
                running_for=running_for,
                webui_port=webui_port,
                napcat_port=napcat_port,
                raw_ports=ports,
            )
        )

    return result


async def find_container_by_qq(qq: str) -> Optional[ContainerInfo]:
    """根据容器内登录的 QQ 号找容器（容器名以 nuo<qq> 结尾）"""
    qq = str(qq).strip()
    containers = await list_containers()
    for c in containers:
        if c.qq == qq:
            return c
    return None


async def find_container_by_serial(serial: str) -> Optional[ContainerInfo]:
    """根据序号找容器"""
    serial = str(serial).strip()
    containers = await list_containers()
    for c in containers:
        if c.serial == serial:
            return c
    return None


async def find_container(name_or_serial_or_qq: str) -> Optional[ContainerInfo]:
    """通用查找：先按完整名/序号/QQ 匹配"""
    key = str(name_or_serial_or_qq).strip()
    containers = await list_containers()
    for c in containers:
        if c.name == key or c.serial == key or c.qq == key:
            return c
    # 兜底：容器名以 key 结尾（子串）
    for c in containers:
        if c.name.endswith(key):
            return c
    return None


async def restart_container(container_name: str, timeout: float = 60.0) -> None:
    """重启容器 (docker restart)"""
    rc, stdout, stderr = await _run(["restart", container_name], timeout=timeout)
    if rc != 0:
        raise DockerError(
            f"docker restart {container_name} 失败: {stderr or stdout or '未知错误'}"
        )
    logger.success(f"[napcat_docker] 容器 {container_name} 已重启")


async def wait_container_up(
    container_name: str, max_wait: float = 30.0, interval: float = 2.0
) -> bool:
    """轮询等待容器恢复 Up 状态"""
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval
        try:
            c = await find_container(container_name)
            if c and c.status.startswith("Up"):
                return True
        except DockerError as e:
            logger.debug(f"[napcat_docker] 等待容器 {container_name} 时查询失败: {e}")
    return False
