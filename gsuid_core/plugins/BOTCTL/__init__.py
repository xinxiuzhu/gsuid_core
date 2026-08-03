"""BOTCTL 插件 —— 通过 dk 指令管理 QQ 机器人

自动发现机器人实例：扫描运行环境解析实例名 {前缀}{序号}nuo{用户QQ} + 端口映射。
无需手动维护映射，发指令的人 QQ 对应实例名里的 QQ 即为持有者。
容器内 ACCOUNT 环境变量为实际登录的小号 QQ。

指令（前缀 dk/DK/Dk）：
    dk状态 [序号/QQ/小号]   查询机器人运行与登录状态
    dk二维码 [序号/QQ/小号] 获取登录二维码（掉线时扫码重登）
    dk二维码刷新           刷新二维码
    dk重启 [序号/QQ/小号]   重启机器人（完成后自动查登录状态，需扫码会直接发二维码）
    dk软重启 [序号/QQ/小号] 仅重启登录服务（更快，不影响机器人本体）
    dk反检测 [开/关] [序号/QQ/小号] 查询配置；修改仅管理员
    dk新号 <小号QQ>    预生成新账号配置与默认反向 WS（管理员）
    dk新号模板         更新所有新账号使用的全局模板（管理员）
    dk开号 <用户QQ> <小号QQ> 自动分配序号端口并创建容器（管理员）
    dk换号 <用户QQ> <新小号QQ> 容器不动只换小号（管理员，封号换号最快）
    dk删号 <用户QQ/序号/小号> [确认] 删除容器，保留数据卷（管理员）
    dk版本 <标签>      统一所有容器到指定镜像版本（管理员）
    dk列表             列出所有机器人（管理员）
    dk帮助             查看指令说明

权限：
    普通用户只能操作自己 QQ 对应的机器人；
    管理员(pm<=2)可带序号/QQ参数操作任意机器人。
"""

import os
import json
import asyncio
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

from gsuid_core.sv import SV, Plugins
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from . import config as dk_config
from .docker_ctl import (
    DockerError,
    ContainerInfo,
    pull_image,
    find_container,
    list_containers,
    create_container,
    find_free_serial,
    remove_container,
    restart_container,
    wait_container_up,
    recreate_container,
    clear_container_path,
    find_container_by_qq,
    list_container_details,
    read_napcat_config_file,
    write_napcat_config_file,
    find_container_by_account,
)
from .napcat_api import NapCatAPIError, NapCatAPIClient, generate_qrcode_image
from .account_config import (
    normalize_qq,
    build_ws_clients,
    build_core_config,
    build_onebot_config,
    build_protocol_config,
)

Plugins(
    name="BOTCTL",
    force_prefix=["dk", "DK", "Dk"],
    allow_empty_prefix=False,
)

sv = SV("机器人管理", pm=6, area="ALL")

# 复用 API 客户端以缓存鉴权凭证
_api_clients: dict[str, NapCatAPIClient] = {}

ADMIN_PM = 2  # pm <= 2 视为管理员（owner 及以上）

_BYPASS_KEYS = (
    "hook",
    "window",
    "module",
    "process",
    "container",
    "js",
)
_BYPASS_ON_WORDS = {"开", "开启", "启用", "on", "enable", "全开"}
_BYPASS_OFF_WORDS = {"关", "关闭", "禁用", "off", "disable", "全关"}


async def _wait_webui_ready(
    api: NapCatAPIClient, max_wait: float = 60.0, interval: float = 3.0
) -> bool:
    """轮询直到 WebUI 能正常应答（容器 Up ≠ 服务就绪）"""
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval
        try:
            await api.check_login_status()
            return True  # 有响应（不管成功还是"QQ已登录"），说明服务已就绪
        except NapCatAPIError:
            return True  # 能返回业务错误说明服务在运行
        except Exception:
            # 502 / 连接拒绝 / 超时 → 还没就绪，继续等
            logger.debug(
                f"[{api.serial}] WebUI 尚未就绪，已等 {elapsed:.0f}s/{max_wait:.0f}s"
            )
    return False


async def _post_restart_check(
    bot: Bot, api: NapCatAPIClient, c: ContainerInfo
) -> None:
    """重启后自动检查登录状态：已自登 → 告知成功；需扫码 → 自动发二维码"""
    try:
        data = await api.check_login_status()
    except Exception as e:
        await bot.send(f"重启完成，但查询登录状态失败: {e}\n请稍后用「dk状态」查看")
        return

    if data.get("isLogin"):
        await bot.send(f"机器人 {c.serial} 重启完成，已自动登录 ✅")
        return

    # 未登录 → 自动获取二维码
    await bot.send(f"机器人 {c.serial} 已重启，需要扫码登录，正在获取二维码...")
    try:
        qr_url = await api.get_or_refresh_qrcode()
    except NapCatAPIError as e:
        await bot.send(f"获取二维码失败: {e}")
        return

    # 对齐 WWUID 等内置插件的图片发送模式：下载原始 bytes 直接传给 bot.send()
    try:
        qr_bytes = generate_qrcode_image(qr_url)
    except Exception as e:
        await bot.send(f"下载二维码图片失败: {e}")
        return
    await bot.send(qr_bytes, at_sender=True)
    await bot.send(
        "请用该机器人的 QQ 扫码登录\n"
        "二维码有效期较短，失效可发「dk二维码刷新」重新获取",
        at_sender=True,
    )


async def _enable_bypass_before_restart(
    bot: Bot, api: NapCatAPIClient, c: ContainerInfo
) -> None:
    """按插件配置在重启前确保反检测已开启，不阻断原重启流程"""
    if not dk_config.get_auto_enable_bypass():
        return
    try:
        await api.set_all_bypasses(True)
        logger.info(f"[{c.serial}] 重启前已确保 NapCat 反检测全部开启")
    except Exception as e:
        logger.warning(f"[{c.serial}] 重启前自动开启反检测失败: {e}")
        await bot.send(f"反检测自动开启失败，将继续重启：{e}")


def _parse_bypass_args(text: str) -> Tuple[Optional[bool], str]:
    """解析反检测指令，返回 (开关动作, 目标参数)"""
    action: Optional[bool] = None
    target_parts = []
    for part in text.strip().split():
        lowered = part.lower()
        if lowered in _BYPASS_ON_WORDS:
            action = True
        elif lowered in _BYPASS_OFF_WORDS:
            action = False
        else:
            target_parts.append(part)
    return action, " ".join(target_parts)


async def _get_config_writer(qq: str = "") -> ContainerInfo:
    """选择一个运行中的 NapCat 容器作为共享配置目录的写入入口。"""
    containers = await list_containers()
    running = [c for c in containers if c.status.startswith("Up")]
    if qq:
        for container in running:
            if container.qq == qq:
                return container
    if running:
        return running[0]
    raise DockerError("没有运行中的 NapCat 容器，无法写入共享配置目录")


async def _read_json_config(
    container: ContainerInfo,
    filename: str,
) -> Optional[Dict[str, Any]]:
    content = await read_napcat_config_file(
        container.name,
        dk_config.get_napcat_config_dir(),
        filename,
    )
    if content is None:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise DockerError(f"{filename} JSON 格式损坏: {e}") from e
    if not isinstance(data, dict):
        raise DockerError(f"{filename} 顶层必须是 JSON 对象")
    return data


async def _write_json_config(
    container: ContainerInfo,
    filename: str,
    data: Dict[str, Any],
) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    await write_napcat_config_file(
        container.name,
        dk_config.get_napcat_config_dir(),
        filename,
        content,
    )


def _build_new_account_clients() -> list[Dict[str, Any]]:
    return build_ws_clients(
        name=dk_config.get_new_account_ws_name(),
        url=dk_config.get_new_account_ws_url(),
        token=dk_config.get_new_account_ws_token(),
        custom_json=dk_config.get_new_account_ws_clients_json(),
    )


def _host_config_dir() -> Path:
    raw = dk_config.get_host_napcat_config_dir().strip()
    if not raw:
        raise DockerError(
            "未配置宿主机 NapCat 配置目录（host_napcat_config_dir）"
        )
    return Path(raw)


def _read_host_json(filename: str) -> Optional[Dict[str, Any]]:
    path = _host_config_dir() / filename
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DockerError(f"{filename} JSON 格式损坏: {e}") from e
    if not isinstance(data, dict):
        raise DockerError(f"{filename} 顶层必须是 JSON 对象")
    return data


def _write_host_json(filename: str, data: Dict[str, Any]) -> None:
    path = _host_config_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(tmp, 0o664)
    except OSError:
        pass
    tmp.replace(path)


def _remove_host_account_configs(qq: str) -> None:
    """尽力清理旧小号的账号配置文件（换号时使用，失败仅记日志）"""
    if not dk_config.get_host_napcat_config_dir().strip():
        return
    for filename in (
        f"napcat_{qq}.json",
        f"onebot11_{qq}.json",
        f"napcat_protocol_{qq}.json",
    ):
        try:
            path = _host_config_dir() / filename
            if path.exists():
                path.unlink()
                logger.info(f"[BOTCTL] 已删除旧小号配置 {filename}")
        except Exception as e:
            logger.warning(f"[BOTCTL] 删除旧小号配置 {filename} 失败: {e}")


async def _write_account_configs(
    qq: str, clients: List[Dict[str, Any]]
) -> List[str]:
    """预生成小号的三份 NapCat 配置，优先直写宿主机目录，缺失时回退容器 exec"""
    written: List[str] = []
    if dk_config.get_host_napcat_config_dir().strip():
        global_core = _read_host_json("napcat.json")
        account_core = _read_host_json(f"napcat_{qq}.json")
        account_onebot = _read_host_json(f"onebot11_{qq}.json")
        account_protocol = _read_host_json(f"napcat_protocol_{qq}.json")

        _write_host_json(
            f"napcat_{qq}.json",
            build_core_config(
                account_core,
                global_core,
                dk_config.get_auto_enable_bypass(),
            ),
        )
        written.append(f"napcat_{qq}.json")
        _write_host_json(
            f"onebot11_{qq}.json",
            build_onebot_config(account_onebot, clients),
        )
        written.append(f"onebot11_{qq}.json")
        if dk_config.get_new_account_create_protocol():
            _write_host_json(
                f"napcat_protocol_{qq}.json",
                build_protocol_config(account_protocol),
            )
            written.append(f"napcat_protocol_{qq}.json")
        if dk_config.get_new_account_sync_global_template():
            _write_host_json(
                "onebot11.json",
                build_onebot_config(_read_host_json("onebot11.json"), clients),
            )
            written.append("onebot11.json（全局模板）")
        return written

    writer = await _get_config_writer()
    global_core = await _read_json_config(writer, "napcat.json")
    account_core = await _read_json_config(writer, f"napcat_{qq}.json")
    account_onebot = await _read_json_config(writer, f"onebot11_{qq}.json")
    account_protocol = await _read_json_config(writer, f"napcat_protocol_{qq}.json")

    await _write_json_config(
        writer,
        f"napcat_{qq}.json",
        build_core_config(
            account_core,
            global_core,
            dk_config.get_auto_enable_bypass(),
        ),
    )
    written.append(f"napcat_{qq}.json")
    await _write_json_config(
        writer,
        f"onebot11_{qq}.json",
        build_onebot_config(account_onebot, clients),
    )
    written.append(f"onebot11_{qq}.json")
    if dk_config.get_new_account_create_protocol():
        await _write_json_config(
            writer,
            f"napcat_protocol_{qq}.json",
            build_protocol_config(account_protocol),
        )
        written.append(f"napcat_protocol_{qq}.json")
    if dk_config.get_new_account_sync_global_template():
        await _sync_global_onebot_template(writer, clients)
        written.append("onebot11.json（全局模板）")
    return written


def _image_parts() -> Tuple[str, str]:
    """拆分配置镜像为 (仓库名, 标签)"""
    image = dk_config.get_image().strip()
    name, _, tag = image.rpartition(":")
    if not name or not tag:
        raise DockerError(f"配置的镜像格式非法: {image}")
    return name, tag


async def _sync_global_onebot_template(
    container: ContainerInfo,
    clients: list[Dict[str, Any]],
) -> None:
    existing = await _read_json_config(container, "onebot11.json")
    await _write_json_config(
        container,
        "onebot11.json",
        build_onebot_config(existing, clients),
    )


def _get_api_client(c: ContainerInfo) -> NapCatAPIClient:
    """构造/复用对应实例的 NapCatAPIClient"""
    key = c.name
    if key not in _api_clients:
        port = c.webui_port
        if not port or port < 0:
            # 端口解析失败，用序号兜底推断（序号 + 20000）
            try:
                port = int(c.serial) + 20000
            except ValueError:
                port = -1
        _api_clients[key] = NapCatAPIClient(
            host=dk_config.get_host(),
            port=port,
            token=dk_config.get_webui_token(),
            serial=c.serial,
        )
    return _api_clients[key]


async def _resolve(
    bot: Bot, ev: Event, arg: str
) -> Optional[Tuple[NapCatAPIClient, ContainerInfo]]:
    """根据参数与权限解析目标机器人

    返回 (api_client, container_info) 或 None（已回复错误信息）。
    """
    arg = arg.strip()

    if arg:
        # 带参数 → 按序号/QQ/名称匹配；非管理员仅当该机器人归自己所有时放行
        try:
            c = await find_container(arg)
            if c is None:
                c = await find_container_by_account(arg)
        except DockerError as e:
            await bot.send(f"查询失败: {e}")
            return None

        if c is None:
            await bot.send(f"未找到序号、用户QQ或小号QQ为 {arg} 的机器人")
            return None

        if ev.user_pm > ADMIN_PM and c.qq != str(ev.user_id):
            await bot.send("无权操作该机器人，仅管理员或机器人主人可用")
            return None
    else:
        # 无参数 → 按发送者 QQ 匹配
        try:
            c = await find_container_by_qq(str(ev.user_id))
        except DockerError as e:
            await bot.send(f"查询失败: {e}")
            return None

        if c is None:
            if ev.user_pm <= ADMIN_PM:
                await bot.send(
                    "未找到你 QQ 对应的机器人，管理员可带序号使用，例如：dk状态 20012"
                )
            else:
                await bot.send("未找到你 QQ 对应的机器人，如需使用请联系管理员")
            return None

    api = _get_api_client(c)
    return api, c


# ---------------- 指令 ----------------


@sv.on_command(("帮助", "help"), block=True)
async def dk_help(bot: Bot, ev: Event):
    await bot.send(
        "【机器人管理】\n"
        "dk状态 [序号/QQ/小号] - 查询机器人状态\n"
        "dk二维码 [序号/QQ/小号] - 获取登录二维码\n"
        "dk二维码刷新 - 刷新二维码\n"
        "dk重启 [序号/QQ/小号] - 重启机器人（自动查登录，需扫码直接发码）\n"
        "dk软重启 [序号/QQ/小号] - 重新登录（更快，同样自动查登录发码）\n"
        "dk反检测 [序号/QQ/小号] - 查询反检测配置\n"
        "dk反检测 [开/关] [序号/QQ/小号] - 修改配置（仅管理员）\n"
        "dk开号 <用户QQ> <小号QQ> - 自动分配序号端口创建容器（仅管理员）\n"
        "dk换号 <用户QQ> <新小号QQ> - 容器不动只换小号（仅管理员）\n"
        "dk删号 <用户QQ/序号/小号> [确认] - 删除容器保留数据卷（仅管理员）\n"
        "dk版本 <标签> - 统一所有容器到指定版本（仅管理员）\n"
        "dk新号 <小号QQ> - 仅预生成账号配置（仅管理员）\n"
        "dk新号模板 - 更新新账号全局模板（仅管理员）\n"
        "dk列表 - 列出所有机器人\n"
        "普通用户仅可管理自己的机器人；管理员可带序号管理任意机器人。"
    )


@sv.on_command(("列表", "list"), block=True)
async def dk_list(bot: Bot, ev: Event):
    logger.info("开始执行 BOTCTL [列表]")
    if ev.user_pm > ADMIN_PM:
        await bot.send("仅管理员可查看机器人列表")
        return
    try:
        containers = await list_container_details()
    except DockerError as e:
        await bot.send(f"查询失败: {e}")
        return

    if not containers:
        await bot.send("当前没有运行中的机器人")
        return

    lines = ["【机器人列表】"]
    for c in containers:
        short_status = c.status.split("(")[0].strip()
        if c.napcat_port and c.webui_port:
            ports = f"{c.napcat_port}/{c.webui_port}"
        else:
            ports = "端口未知"
        lines.append(
            f"序号 {c.serial} | 用户QQ {c.qq} | 小号 {c.account or '未知'} | "
            f"{ports} | {short_status}"
        )
    await bot.send("\n".join(lines))


@sv.on_command(
    ("新号", "newaccount", "新号模板", "newaccounttemplate"),
    block=True,
)
async def dk_new_account(bot: Bot, ev: Event):
    logger.info("开始执行 BOTCTL [新号]")
    if ev.user_pm > ADMIN_PM:
        await bot.send("仅管理员可生成共享的 NapCat 新账号配置")
        return

    raw_arg = ev.text.strip()
    template_mode = ev.command in ("新号模板", "newaccounttemplate") or raw_arg.lower() in (
        "模板",
        "template",
    )

    try:
        clients = _build_new_account_clients()
        if template_mode:
            if dk_config.get_host_napcat_config_dir().strip():
                _write_host_json(
                    "onebot11.json",
                    build_onebot_config(_read_host_json("onebot11.json"), clients),
                )
                writer_name = "宿主机配置目录"
            else:
                writer = await _get_config_writer()
                await _sync_global_onebot_template(writer, clients)
                writer_name = writer.name
            summary = "\n".join(
                f"- {client['name']}: {client['url']}"
                for client in clients
            )
            await bot.send(
                "新账号全局模板已更新 ✅\n"
                f"写入: {writer_name}\n"
                f"{summary}\n"
                "以后账号首次登录时，NapCat 会从 onebot11.json 继承并自动生成账号专属配置。"
            )
            return

        qq = normalize_qq(raw_arg)
        written = await _write_account_configs(qq, clients)
    except (DockerError, ValueError) as e:
        await bot.send(f"新号配置生成失败: {e}")
        return
    except Exception as e:
        logger.exception(f"BOTCTL 新号配置生成异常: {e}")
        await bot.send(f"新号配置生成异常: {e}")
        return

    connection_summary = "；".join(
        f"{client['name']} -> {client['url']}" for client in clients
    )
    await bot.send(
        f"QQ {qq} 的 NapCat 配置已准备完成 ✅\n"
        f"连接: {connection_summary}\n"
        f"文件: {', '.join(written)}\n"
        "现在可扫码登录；若该账号已在线，请重启后加载磁盘配置。"
    )


@sv.on_command(("开号", "create"), block=True)
async def dk_create(bot: Bot, ev: Event):
    """dk开号 <用户QQ> <小号QQ>：自动分配序号端口并创建容器（管理员）"""
    logger.info("开始执行 BOTCTL [开号]")
    if ev.user_pm > ADMIN_PM:
        await bot.send("仅管理员可开号")
        return
    args = ev.text.split()
    if len(args) != 2:
        await bot.send(
            "用法: dk开号 <用户QQ> <小号QQ>\n"
            "例: dk开号 123456789 987654321\n"
            "用户QQ用于容器命名与权限；小号QQ是实际登录的机器人号"
        )
        return
    try:
        user_qq = normalize_qq(args[0])
        small_qq = normalize_qq(args[1])
    except ValueError as e:
        await bot.send(f"参数错误: {e}")
        return

    try:
        containers = await list_container_details()
        existing = [c for c in containers if c.qq == user_qq]
        if existing:
            await bot.send(
                f"用户QQ {user_qq} 已有容器 {existing[0].name}\n"
                f"如需更换小号请用「dk换号 {user_qq} <新小号>」"
            )
            return
        occupied = [c for c in containers if c.account == small_qq]
        if occupied:
            await bot.send(f"小号 {small_qq} 已在容器 {occupied[0].name} 中使用")
            return

        await bot.send(f"正在为 用户QQ {user_qq} 开号（小号 {small_qq}）...")

        clients = _build_new_account_clients()
        files = await _write_account_configs(small_qq, clients)

        serial = await find_free_serial(
            dk_config.get_serial_start(),
            dk_config.get_napcat_port_offset(),
            dk_config.get_webui_port_offset(),
        )
        name = f"{dk_config.get_prefix()}{serial}nuo{user_qq}"
        napcat_port = serial + dk_config.get_napcat_port_offset()
        webui_port = serial + dk_config.get_webui_port_offset()
        image = dk_config.get_image()

        await bot.send(
            f"序号 {serial}，端口 {napcat_port}/{webui_port}\n"
            f"正在拉取镜像 {image} ..."
        )
        await pull_image(image)
        await create_container(
            name,
            image,
            {
                "ACCOUNT": small_qq,
                "NAPCAT_UID": dk_config.get_napcat_uid(),
                "NAPCAT_GID": dk_config.get_napcat_gid(),
                "TZ": "Asia/Shanghai",
            },
            [
                (str(napcat_port), "3000/tcp"),
                (str(webui_port), "6099/tcp"),
            ],
            [
                (dk_config.get_host_qqbot_dir(), "/app/qqbot", ""),
                (
                    dk_config.get_host_napcat_config_dir(),
                    "/app/napcat/config",
                    "",
                ),
            ],
        )
    except (DockerError, ValueError) as e:
        await bot.send(f"开号失败: {e}")
        return
    except Exception as e:
        logger.exception(f"BOTCTL 开号异常: {e}")
        await bot.send(f"开号异常: {e}")
        return

    await bot.send(f"容器 {name} 已创建，等待服务就绪...")
    await wait_container_up(name, max_wait=30, interval=2)

    api = NapCatAPIClient(
        host=dk_config.get_host(),
        port=webui_port,
        token=dk_config.get_webui_token(),
        serial=str(serial),
    )
    ready = await _wait_webui_ready(api, max_wait=60, interval=3)

    connection_summary = "；".join(
        f"{client['name']} -> {client['url']}" for client in clients
    )
    lines = [
        "开号完成 ✅",
        f"容器: {name}",
        f"用户QQ: {user_qq} | 小号: {small_qq}",
        f"端口: OneBot {napcat_port} | WebUI {webui_port}",
        f"配置文件: {', '.join(files)}",
        f"连接: {connection_summary}",
    ]
    if not ready:
        lines.append("⚠ 服务启动较慢，稍后可用「dk状态」查看")
    lines.append("让用户发送「dk二维码」即可扫码登录。")
    await bot.send("\n".join(lines))


@sv.on_command(("换号", "swap"), block=True)
async def dk_swap(bot: Bot, ev: Event):
    """dk换号 <用户QQ> <新小号QQ>：容器不动，只更换登录的小号（管理员）"""
    logger.info("开始执行 BOTCTL [换号]")
    if ev.user_pm > ADMIN_PM:
        await bot.send("仅管理员可换号")
        return
    args = ev.text.split()
    if len(args) != 2:
        await bot.send(
            "用法: dk换号 <用户QQ> <新小号QQ>\n"
            "例: dk换号 123456789 111222333\n"
            "容器不动只换小号，最快出二维码"
        )
        return
    try:
        user_qq = normalize_qq(args[0])
        new_small = normalize_qq(args[1])
    except ValueError as e:
        await bot.send(f"参数错误: {e}")
        return

    try:
        containers = await list_container_details()
        c = next((x for x in containers if x.qq == user_qq), None)
        if c is None:
            await bot.send(
                f"未找到用户QQ {user_qq} 的容器，请先用「dk开号」"
            )
            return
        old_small = c.account
        if old_small == new_small:
            await bot.send(f"小号没变化（都是 {old_small}），无需换号")
            return
        occupied = [x for x in containers if x.account == new_small]
        if occupied:
            await bot.send(f"小号 {new_small} 已在容器 {occupied[0].name} 中使用")
            return

        await bot.send(
            f"正在为 {c.name} 换号: 小号 {old_small or '未知'} -> {new_small} ..."
        )
        clients = _build_new_account_clients()
        files = await _write_account_configs(new_small, clients)

        if c.status.startswith("Up"):
            try:
                await clear_container_path(c.name, dk_config.get_napcat_data_dir())
            except DockerError as e:
                logger.warning(f"[{c.serial}] 清理旧登录数据失败: {e}")

        await recreate_container(c.name, env_override={"ACCOUNT": new_small})
    except (DockerError, ValueError) as e:
        await bot.send(f"换号失败: {e}")
        return
    except Exception as e:
        logger.exception(f"BOTCTL 换号异常: {e}")
        await bot.send(f"换号异常: {e}")
        return

    await wait_container_up(c.name, max_wait=30, interval=2)
    api = _get_api_client(c)
    ready = await _wait_webui_ready(api, max_wait=60, interval=3)
    if old_small:
        _remove_host_account_configs(old_small)

    lines = [
        "换号完成 ✅",
        f"容器: {c.name}（序号 {c.serial}，端口不变）",
        f"小号: {old_small or '未知'} -> {new_small}",
        f"配置文件: {', '.join(files)}",
    ]
    if not ready:
        lines.append("⚠ 服务启动较慢，稍后可用「dk状态」查看")
    lines.append("让用户发送「dk二维码」扫码登录新小号。")
    await bot.send("\n".join(lines))


@sv.on_command(("删号", "remove", "delete"), block=True)
async def dk_delete(bot: Bot, ev: Event):
    """dk删号 <用户QQ/序号> [确认]：删除容器，保留数据卷（管理员）"""
    logger.info("开始执行 BOTCTL [删号]")
    if ev.user_pm > ADMIN_PM:
        await bot.send("仅管理员可删号")
        return
    args = ev.text.split()
    if not args:
        await bot.send("用法: dk删号 <用户QQ/序号> [确认]")
        return
    confirm = args[-1] in ("确认", "confirm", "yes")
    target = " ".join(args[:-1]) if confirm else " ".join(args)

    try:
        c = await find_container(target)
        if c is None:
            c = await find_container_by_account(target)
    except DockerError as e:
        await bot.send(f"查询失败: {e}")
        return
    if c is None:
        await bot.send(f"未找到 {target} 对应的容器")
        return

    if not confirm:
        try:
            details = await list_container_details()
            info = next((x for x in details if x.name == c.name), c)
        except DockerError:
            info = c
        await bot.send(
            f"将删除容器 {c.name}\n"
            f"序号 {c.serial} | 用户QQ {c.qq} | 小号 {info.account or '未知'}\n"
            f"⚠ 仅删除容器，数据卷保留\n"
            f"确认请发送: dk删号 {target} 确认"
        )
        return

    try:
        await remove_container(c.name, remove_volume=False)
    except DockerError as e:
        await bot.send(f"删除失败: {e}")
        return
    await bot.send(f"容器 {c.name}（用户QQ {c.qq}）已删除，数据卷保留 ✅")


@sv.on_command(("版本", "version", "统一版本"), block=True)
async def dk_version(bot: Bot, ev: Event):
    """dk版本 <标签>：统一所有容器到指定镜像版本（管理员）"""
    logger.info("开始执行 BOTCTL [版本]")
    if ev.user_pm > ADMIN_PM:
        await bot.send("仅管理员可统一 NapCat 版本")
        return
    tag = ev.text.strip()
    if not tag or " " in tag or "/" in tag or ":" in tag:
        await bot.send("用法: dk版本 <标签>\n例: dk版本 v4.17.5")
        return
    try:
        image_name, _ = _image_parts()
        image = f"{image_name}:{tag}"
    except DockerError as e:
        await bot.send(f"镜像配置错误: {e}")
        return

    await bot.send(
        f"开始统一 NapCat 版本到 {image} ...\n"
        "将逐台重建镜像不同的容器（保留挂载/端口/数据卷/环境变量/重启策略）"
    )
    try:
        await pull_image(image)
    except DockerError as e:
        await bot.send(f"拉取镜像失败: {e}")
        return

    try:
        containers = await list_container_details()
    except DockerError as e:
        await bot.send(f"查询容器失败: {e}")
        return
    targets = [c for c in containers if c.image != image]
    if not targets:
        await bot.send(f"所有容器已是 {image}，无需操作")
        return

    ok_names: List[str] = []
    fail_list: List[str] = []
    total = len(targets)
    for index, c in enumerate(targets, 1):
        try:
            await bot.send(f"[{index}/{total}] 正在重建 {c.name} ...")
            await recreate_container(c.name, image=image)
            ok_names.append(c.name)
        except Exception as e:
            logger.exception(f"[BOTCTL] 重建 {c.name} 失败: {e}")
            fail_list.append(f"{c.name}: {e}")

    lines = [f"版本统一完成 ✅ 目标: {image}", f"成功: {len(ok_names)} 台"]
    if fail_list:
        lines.append(f"失败: {len(fail_list)} 台")
        lines.extend(f"- {msg}" for msg in fail_list)
    if ok_names:
        lines.append("容器正在恢复，可用「dk状态」或「dk列表」查看。")
    await bot.send("\n".join(lines))


@sv.on_command(("状态", "status"), block=True)
async def dk_status(bot: Bot, ev: Event):
    logger.info("开始执行 BOTCTL [状态]")
    resolved = await _resolve(bot, ev, ev.text)
    if resolved is None:
        return
    api, c = resolved

    # QQ 登录状态
    login_line = "（登录状态查询失败）"
    try:
        data = await api.check_login_status()
        is_login = data.get("isLogin")
        is_offline = data.get("isOffline")
        login_error = data.get("loginError")
        if is_login:
            login_line = "登录状态: 在线 ✅"
        elif is_offline:
            login_line = "登录状态: 已掉线 ❌"
        else:
            login_line = "登录状态: 未登录 ❌（需扫码登录）"
        if login_error:
            login_line += f"\n掉线原因: {login_error}"
    except NapCatAPIError as e:
        login_line = f"登录状态查询失败: {e}"
    except Exception as e:
        login_line = f"登录状态查询异常: {e}"

    await bot.send(
        f"机器人 {c.serial}：\n"
        f"运行状态: {c.status}（{c.running_for}）\n"
        f"{login_line}"
    )


@sv.on_command(("反检测", "bypass"), block=True)
async def dk_bypass(bot: Bot, ev: Event):
    logger.info("开始执行 BOTCTL [反检测]")
    action, target = _parse_bypass_args(ev.text)
    if action is not None and ev.user_pm > ADMIN_PM:
        await bot.send("仅管理员可修改共享的 NapCat 反检测配置")
        return
    resolved = await _resolve(bot, ev, target)
    if resolved is None:
        return
    api, c = resolved

    try:
        if action is not None:
            await api.set_all_bypasses(action)

        config = await api.get_napcat_config()
    except NapCatAPIError as e:
        await bot.send(f"反检测配置操作失败: {e}")
        return
    except Exception as e:
        await bot.send(f"反检测配置操作异常: {e}")
        return

    bypass = config.get("bypass") or {}
    enabled_count = sum(bypass.get(key) is True for key in _BYPASS_KEYS)
    o3_enabled = config.get("o3HookMode") == 1
    all_enabled = enabled_count == len(_BYPASS_KEYS) and o3_enabled
    state = "全部开启 ✅" if all_enabled else "未全部开启 ❌"

    lines = [
        f"机器人 {c.serial} 反检测: {state}",
        f"Bypass: {enabled_count}/{len(_BYPASS_KEYS)}",
        f"O3 Hook: {'开启' if o3_enabled else '关闭'}",
    ]
    if action is not None:
        lines.append("配置已保存，重启 NapCat 后生效。")
        lines.append("共享同一 config 挂载的容器会读取同一份全局配置。")
    await bot.send("\n".join(lines))


@sv.on_command(("二维码", "qr", "刷新二维码", "refreshqr"), block=True)
async def dk_qrcode(bot: Bot, ev: Event):
    logger.info("开始执行 BOTCTL [二维码]")
    # "dk二维码刷新" → ev.command="二维码", ev.text="刷新"
    # "刷新" 是操作参数不是容器参数，清理掉避免 _resolve 误匹配
    arg = "" if ev.text.strip() == "刷新" else ev.text
    resolved = await _resolve(bot, ev, arg)
    if resolved is None:
        return
    api, c = resolved

    # 是否需要刷新
    want_refresh = ev.command in ("刷新二维码", "refreshqr") or "刷新" in ev.text

    # 先看在线状态
    try:
        data = await api.check_login_status()
        if data.get("isLogin"):
            await bot.send("机器人当前在线，无需扫码 ✅")
            return
    except NapCatAPIError as e:
        logger.warning(f"[{c.serial}] 查询登录状态失败: {e}，继续尝试取码")
    except Exception as e:
        logger.warning(f"[{c.serial}] 查询登录状态异常: {e}，继续尝试取码")

    # 获取二维码
    try:
        if want_refresh:
            try:
                await api.refresh_qrcode()
            except NapCatAPIError as e:
                # 已在线等情况下刷新会报错，忽略继续取码
                logger.debug(f"[{c.serial}] 刷新二维码: {e}")
            qr_url = await api.get_qrcode()
        else:
            try:
                qr_url = await api.get_qrcode()
            except NapCatAPIError as e:
                if "QRCode" in str(e) or "empty" in str(e).lower():
                    # 未生成，刷新后重试
                    logger.debug(f"[{c.serial}] 二维码未生成，触发刷新...")
                    await api.refresh_qrcode()
                    qr_url = await api.get_qrcode()
                else:
                    raise
    except NapCatAPIError as e:
        await bot.send(f"获取二维码失败: {e}")
        return
    except Exception as e:
        await bot.send(f"获取二维码异常: {e}")
        return

    # 对齐 WWUID 等内置插件模式：下载原始 bytes 直传 bot.send()
    try:
        qr_bytes = generate_qrcode_image(qr_url)
    except Exception as e:
        await bot.send(f"下载二维码图片失败: {e}\n二维码链接: {qr_url}")
        return

    await bot.send(qr_bytes, at_sender=True)
    await bot.send(
        "请用该机器人的 QQ 扫码登录\n"
        "二维码有效期较短，失效可发「dk二维码刷新」重新获取",
        at_sender=True,
    )


@sv.on_command(("重启", "restart"), block=True)
async def dk_restart(bot: Bot, ev: Event):
    logger.info("开始执行 BOTCTL [重启]")
    resolved = await _resolve(bot, ev, ev.text)
    if resolved is None:
        return
    api, c = resolved

    await bot.send(f"正在重启机器人 {c.serial}...")

    await _enable_bypass_before_restart(bot, api, c)

    # 1. docker restart
    try:
        await restart_container(c.name, timeout=60)
    except DockerError as e:
        await bot.send(f"重启失败: {e}")
        return

    # 2. 等待容器恢复 Up
    up = await wait_container_up(c.name, max_wait=30, interval=2)
    if not up:
        await bot.send(
            f"机器人 {c.serial} 重启已发出，但容器未在规定时间内恢复运行\n"
            f"请稍后用「dk状态」查看"
        )
        return

    # 3. 等待 WebUI 服务就绪（容器 Up ≠ 服务可用）
    await bot.send("容器已恢复，正在等待服务就绪...")
    ready = await _wait_webui_ready(api, max_wait=45, interval=3)
    if not ready:
        await bot.send(
            f"服务启动较慢，{c.serial} 尚未就绪\n"
            f"请稍后用「dk状态」查看进度"
        )
        return

    # 4. 检查登录状态 → 自登成功或自动发码
    await _post_restart_check(bot, api, c)


@sv.on_command(("软重启", "softrestart"), block=True)
async def dk_soft_restart(bot: Bot, ev: Event):
    logger.info("开始执行 BOTCTL [软重启]")
    resolved = await _resolve(bot, ev, ev.text)
    if resolved is None:
        return
    api, c = resolved

    await bot.send(f"正在重新登录机器人 {c.serial}...")

    await _enable_bypass_before_restart(bot, api, c)

    try:
        await api.restart_napcat()
    except NapCatAPIError as e:
        await bot.send(f"重新登录失败: {e}")
        return

    # 等待 WebUI 恢复（软重启更快，给 30s）
    ready = await _wait_webui_ready(api, max_wait=30, interval=2)
    if not ready:
        await bot.send(
            "已发起重新登录，但服务尚未响应\n"
            "请稍后用「dk状态」查看进度"
        )
        return

    await _post_restart_check(bot, api, c)
