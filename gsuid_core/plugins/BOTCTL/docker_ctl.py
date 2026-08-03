"""Docker CLI 异步封装

通过 asyncio.create_subprocess_exec 调用 docker CLI，零额外依赖。
容器命名规律：{前缀}{序号}nuo{QQ号}，例如 fb20012nuo2082318370
端口映射：序号 -> 3000(napcat OneBot), 序号+20000 -> 6099(webui)
"""

import re
import json
import asyncio
from typing import Any, Dict, List, Tuple, Optional
from dataclasses import field, dataclass

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
    qq: str  # 容器名内记录的用户 QQ
    status: str  # docker 状态行，如 "Up 5 hours"
    running_for: str  # 如 "5 hours ago"
    webui_port: Optional[int] = None  # 宿主机 webui 端口
    napcat_port: Optional[int] = None  # 宿主机 napcat(3000) 端口
    raw_ports: str = field(default="", repr=False)
    account: str = ""  # ACCOUNT 环境变量，即容器内登录的小号 QQ
    image: str = ""  # 容器使用的镜像，如 mlikiowa/napcat-docker:v4.17.5


_CONFIG_FILE_RE = re.compile(
    r"(?:onebot11|napcat|napcat_protocol)(?:_[1-9]\d{4,11})?\.json"
)


async def _run(
    args: list,
    timeout: float = 60.0,
    input_data: Optional[bytes] = None,
) -> tuple:
    """执行 docker 命令，返回 (returncode, stdout, stderr)"""
    cmd = ["docker"] + args
    logger.debug(f"[napcat_docker] exec: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise DockerError("未找到 docker 命令，请确认 docker CLI 已安装且在 PATH 中")
    except Exception as e:
        raise DockerError(f"启动 docker 进程失败: {e}")

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=input_data), timeout=timeout
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


def _validate_config_filename(filename: str) -> str:
    if not _CONFIG_FILE_RE.fullmatch(filename):
        raise DockerError(f"非法 NapCat 配置文件名: {filename}")
    return filename


async def read_napcat_config_file(
    container_name: str,
    config_dir: str,
    filename: str,
) -> Optional[str]:
    """通过运行中的 NapCat 容器读取共享配置文件，不存在返回 None。"""
    filename = _validate_config_filename(filename)
    script = '[ -f "$1/$2" ] || exit 3; cat -- "$1/$2"'
    rc, stdout, stderr = await _run(
        [
            "exec",
            container_name,
            "sh",
            "-c",
            script,
            "sh",
            config_dir,
            filename,
        ],
        timeout=15,
    )
    if rc == 3:
        return None
    if rc != 0:
        raise DockerError(
            f"读取 {filename} 失败: {stderr or stdout or '未知错误'}"
        )
    return stdout


async def write_napcat_config_file(
    container_name: str,
    config_dir: str,
    filename: str,
    content: str,
) -> None:
    """通过运行中的 NapCat 容器原子写入共享配置文件。"""
    filename = _validate_config_filename(filename)
    script = """
set -eu
mkdir -p -- "$1"
target="$1/$2"
tmp="$target.tmp.$$"
trap 'rm -f -- "$tmp"' EXIT
cat > "$tmp"
chmod 664 "$tmp" 2>/dev/null || true
mv -f -- "$tmp" "$target"
trap - EXIT
""".strip()
    rc, stdout, stderr = await _run(
        [
            "exec",
            "-i",
            container_name,
            "sh",
            "-c",
            script,
            "sh",
            config_dir,
            filename,
        ],
        timeout=15,
        input_data=content.encode("utf-8"),
    )
    if rc != 0:
        raise DockerError(
            f"写入 {filename} 失败: {stderr or stdout or '未知错误'}"
        )


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
            "{{.Names}}|{{.Status}}|{{.RunningFor}}|{{.Ports}}|{{.Image}}",
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
        image = parts[4].strip() if len(parts) > 4 else ""

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
                image=image,
            )
        )

    return result


async def find_container_by_qq(qq: str) -> Optional[ContainerInfo]:
    """根据容器内记录的用户 QQ 找容器（容器名以 nuo<qq> 结尾）"""
    qq = str(qq).strip()
    containers = await list_containers()
    for c in containers:
        if c.qq == qq:
            return c
    return None


async def find_container_by_account(account: str) -> Optional[ContainerInfo]:
    """根据 ACCOUNT 环境变量（小号 QQ）找容器"""
    account = str(account).strip()
    containers = await list_container_details()
    for c in containers:
        if c.account == account:
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


# ---------------- 容器生命周期（dk开号/删号/换号/版本统一） ----------------


async def pull_image(image: str, timeout: float = 900.0) -> None:
    """拉取镜像 (docker pull)"""
    rc, stdout, stderr = await _run(["pull", image], timeout=timeout)
    if rc != 0:
        raise DockerError(f"docker pull {image} 失败: {stderr or stdout}")
    logger.success(f"[napcat_docker] 镜像 {image} 已拉取")


async def create_container(
    name: str,
    image: str,
    env: Dict[str, str],
    port_maps: List[Tuple[str, str]],
    mounts: List[Tuple[str, str, str]],
    restart: str = "always",
    timeout: float = 120.0,
) -> None:
    """创建并启动容器 (docker run -d)

    port_maps: [(宿主端口, 容器端口/协议)]，如 [("30012", "3000/tcp")]
    mounts:    [(源, 目标, 模式)]，模式可为 "" 或 "ro"
    """
    args = ["run", "-d", "--name", name, "--restart", restart]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    for host_port, container_port in port_maps:
        args += ["-p", f"{host_port}:{container_port}"]
    for src, dst, mode in mounts:
        mount = f"{src}:{dst}"
        if mode:
            mount += f":{mode}"
        args += ["-v", mount]
    args.append(image)

    rc, stdout, stderr = await _run(args, timeout=timeout)
    if rc != 0:
        raise DockerError(
            f"docker run 创建容器 {name} 失败: {stderr or stdout or '未知错误'}"
        )
    logger.success(f"[napcat_docker] 容器 {name} 已创建")


async def remove_container(
    container_name: str, remove_volume: bool = False, timeout: float = 60.0
) -> None:
    """强制删除容器；remove_volume=True 时连带删除匿名数据卷"""
    args = ["rm", "-f"]
    if remove_volume:
        args.append("-v")
    args.append(container_name)
    rc, stdout, stderr = await _run(args, timeout=timeout)
    if rc != 0:
        raise DockerError(
            f"docker rm {container_name} 失败: {stderr or stdout or '未知错误'}"
        )
    logger.success(f"[napcat_docker] 容器 {container_name} 已删除")


async def get_container_envs(names: List[str]) -> Dict[str, Dict[str, str]]:
    """批量读取容器环境变量，返回 {容器名: {KEY: VALUE}}"""
    if not names:
        return {}
    rc, stdout, stderr = await _run(["inspect", *names], timeout=30)
    if rc != 0:
        raise DockerError(f"docker inspect 失败: {stderr or stdout}")
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise DockerError(f"docker inspect 输出解析失败: {e}") from e

    result: Dict[str, Dict[str, str]] = {}
    for item in raw:
        name = str(item.get("Name", "")).lstrip("/")
        env: Dict[str, str] = {}
        for kv in item.get("Config", {}).get("Env", []) or []:
            key, _, value = str(kv).partition("=")
            env[key] = value
        result[name] = env
    return result


async def list_container_details() -> List[ContainerInfo]:
    """列出所有容器并附带 ACCOUNT 小号与镜像信息"""
    containers = await list_containers()
    if not containers:
        return containers
    env_map = await get_container_envs([c.name for c in containers])
    for c in containers:
        c.account = env_map.get(c.name, {}).get("ACCOUNT", "") or ""
    return containers


async def find_free_serial(
    serial_start: int,
    napcat_port_offset: int,
    webui_port_offset: int,
) -> int:
    """从 serial_start 起找第一个空闲序号：序号、OneBot 端口、WebUI 端口均未被占用"""
    rc, stdout, stderr = await _run(
        ["ps", "-a", "--format", "{{.Names}}|{{.Ports}}"],
        timeout=15,
    )
    if rc != 0:
        raise DockerError(f"docker ps 失败: {stderr or stdout}")

    used_serials: set[int] = set()
    used_ports: set[int] = set()
    for line in stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, ports = parts[0].strip(), parts[1].strip()
        serial, _ = _parse_name(name)
        if serial is not None:
            try:
                used_serials.add(int(serial))
            except ValueError:
                pass
        for m in re.finditer(r"(\d+)->\d+/tcp", ports):
            try:
                used_ports.add(int(m.group(1)))
            except ValueError:
                pass

    candidate = serial_start
    while True:
        if candidate in used_serials:
            candidate += 1
            continue
        if (
            candidate + napcat_port_offset in used_ports
            or candidate + webui_port_offset in used_ports
        ):
            candidate += 1
            continue
        return candidate


async def get_container_spec(name: str) -> Dict[str, Any]:
    """读取容器完整规格，用于无损重建：env/镜像/重启策略/挂载/端口/网络"""
    rc, stdout, stderr = await _run(["inspect", name], timeout=30)
    if rc != 0:
        raise DockerError(f"docker inspect {name} 失败: {stderr or stdout}")
    try:
        raw = json.loads(stdout)[0]
    except (json.JSONDecodeError, IndexError) as e:
        raise DockerError(f"docker inspect {name} 输出解析失败: {e}") from e

    env: Dict[str, str] = {}
    for kv in raw.get("Config", {}).get("Env", []) or []:
        key, _, value = str(kv).partition("=")
        env[key] = value

    mounts: List[Tuple[str, str, str]] = []
    for m in raw.get("Mounts", []) or []:
        mtype = m.get("Type")
        if mtype == "bind":
            src = m.get("Source", "")
        elif mtype == "volume":
            src = m.get("Name", "")
        else:
            continue
        dst = m.get("Destination", "")
        mode = "ro" if m.get("RW") is False else ""
        if src and dst:
            mounts.append((src, dst, mode))

    port_maps: List[Tuple[str, str]] = []
    for container_port, bindings in (
        raw.get("HostConfig", {}).get("PortBindings", {}) or {}
    ).items():
        for binding in bindings or []:
            host_port = binding.get("HostPort")
            host_ip = binding.get("HostIp")
            if not host_port:
                continue
            if host_ip and host_ip not in ("", "0.0.0.0", "::"):
                port_maps.append((f"{host_ip}:{host_port}", container_port))
            else:
                port_maps.append((host_port, container_port))

    return {
        "env": env,
        "image": raw.get("Config", {}).get("Image", ""),
        "restart": (
            raw.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "")
            or "no"
        ),
        "network_mode": raw.get("HostConfig", {}).get("NetworkMode", ""),
        "mounts": mounts,
        "port_maps": port_maps,
    }


async def recreate_container(
    name: str,
    image: str = "",
    env_override: Optional[Dict[str, str]] = None,
    timeout: float = 300.0,
) -> None:
    """无损重建容器：保留名称/挂载/端口/数据卷/重启策略，可更换镜像或环境变量"""
    spec = await get_container_spec(name)
    new_image = image or spec["image"]
    if not new_image:
        raise DockerError(f"无法确定 {name} 的镜像，拒绝重建")

    network_mode = spec["network_mode"]
    if network_mode not in ("default", "bridge", ""):
        raise DockerError(
            f"{name} 使用自定义网络 {network_mode}，无法安全重建，请手动处理"
        )

    env = dict(spec["env"])
    if env_override:
        env.update(env_override)

    await remove_container(name, remove_volume=False)
    await create_container(
        name,
        new_image,
        env,
        spec["port_maps"],
        spec["mounts"],
        restart=spec["restart"],
        timeout=timeout,
    )
    logger.success(f"[napcat_docker] 容器 {name} 已无损重建")


async def clear_container_path(name: str, path: str, timeout: float = 30.0) -> None:
    """清空容器内目录的所有内容（保留目录本身），仅允许 /app/ 下的路径"""
    if not path.startswith("/app/"):
        raise DockerError(f"拒绝清理非 /app/ 路径: {path}")
    script = 'if [ -d "$1" ]; then find -- "$1" -mindepth 1 -delete; fi'
    rc, stdout, stderr = await _run(
        ["exec", name, "sh", "-c", script, "sh", path],
        timeout=timeout,
    )
    if rc != 0:
        raise DockerError(f"清理 {path} 失败: {stderr or stdout or '未知错误'}")
    logger.success(f"[napcat_docker] 已清空容器 {name} 内 {path}")
