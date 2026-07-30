"""BOTCTL 插件 —— 通过 dk 指令管理 QQ 机器人

自动发现机器人实例：扫描运行环境解析实例名 {前缀}{序号}nuo{QQ} + 端口映射。
无需手动维护映射，发指令的人 QQ 对应实例名里的 QQ 即为持有者。

指令（前缀 dk/DK/Dk）：
    dk状态 [序号/QQ]   查询机器人运行与登录状态
    dk二维码 [序号/QQ] 获取登录二维码（掉线时扫码重登）
    dk二维码刷新       刷新二维码
    dk重启 [序号/QQ]   重启机器人（完成后自动查登录状态，需扫码会直接发二维码）
    dk软重启 [序号/QQ] 仅重启登录服务（更快，不影响机器人本体）
    dk反检测 [开/关] [序号/QQ] 查询配置；修改仅管理员
    dk列表             列出所有机器人（管理员）
    dk帮助             查看指令说明

权限：
    普通用户只能操作自己 QQ 对应的机器人；
    管理员(pm<=2)可带序号/QQ参数操作任意机器人。
"""

import asyncio
from typing import Tuple, Optional

from gsuid_core.sv import SV, Plugins
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from . import config as dk_config
from .docker_ctl import (
    DockerError,
    ContainerInfo,
    find_container,
    list_containers,
    restart_container,
    wait_container_up,
    find_container_by_qq,
)
from .napcat_api import NapCatAPIError, NapCatAPIClient, generate_qrcode_image

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
    await bot.send(qr_bytes)
    await bot.send(
        "请用该机器人的 QQ 扫码登录\n"
        "二维码有效期较短，失效可发「dk二维码刷新」重新获取"
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
        except DockerError as e:
            await bot.send(f"查询失败: {e}")
            return None

        if c is None:
            await bot.send(f"未找到序号或 QQ 为 {arg} 的机器人")
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
        "dk状态 [序号/QQ] - 查询机器人状态\n"
        "dk二维码 [序号/QQ] - 获取登录二维码\n"
        "dk二维码刷新 - 刷新二维码\n"
        "dk重启 [序号/QQ] - 重启机器人（自动查登录，需扫码直接发码）\n"
        "dk软重启 [序号/QQ] - 重新登录（更快，同样自动查登录发码）\n"
        "dk反检测 [序号/QQ] - 查询反检测配置\n"
        "dk反检测 [开/关] [序号/QQ] - 修改配置（仅管理员）\n"
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
        containers = await list_containers()
    except DockerError as e:
        await bot.send(f"查询失败: {e}")
        return

    if not containers:
        await bot.send("当前没有运行中的机器人")
        return

    lines = ["【机器人列表】"]
    for c in containers:
        short_status = c.status.split("(")[0].strip()
        lines.append(f"序号 {c.serial} | QQ {c.qq} | {short_status}")
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

    await bot.send(qr_bytes)
    await bot.send(
        "请用该机器人的 QQ 扫码登录\n"
        "二维码有效期较短，失效可发「dk二维码刷新」重新获取"
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
