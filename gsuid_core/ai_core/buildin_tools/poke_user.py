"""AI 戳一戳工具。

目标会话完全取自当前 ``Event``，模型只负责选择当前上下文里真实出现的用户 ID。
"""

import time
from typing import Dict, Tuple

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

POKE_USER_COOLDOWN_SEC = 60.0

# (bot_self_id, group/direct scope, target_user_id) -> monotonic timestamp
_LAST_POKE_AT: Dict[Tuple[str, str, str], float] = {}
# (session_id, agent_run_id) -> monotonic timestamp；单轮只允许真正提交一次
_TURN_POKE_AT: Dict[Tuple[str, str], float] = {}


def clear_poke_user_throttle() -> None:
    """清空工具节流状态，供测试与热重载使用。"""
    _LAST_POKE_AT.clear()
    _TURN_POKE_AT.clear()


def _cleanup_throttle(now: float) -> None:
    cutoff = now - POKE_USER_COOLDOWN_SEC
    if len(_LAST_POKE_AT) > 4096:
        stale = [key for key, value in _LAST_POKE_AT.items() if value < cutoff]
        for key in stale:
            _LAST_POKE_AT.pop(key, None)
    if len(_TURN_POKE_AT) > 4096:
        stale_turns = [key for key, value in _TURN_POKE_AT.items() if value < cutoff]
        for key in stale_turns:
            _TURN_POKE_AT.pop(key, None)


@ai_tools(category="self", capability_domain="群聊互动")
async def poke_user(
    ctx: RunContext[ToolContext],
    target_user_id: str,
) -> str:
    """在当前会话中真实地戳一戳指定用户。

    仅当你确实想执行 QQ 戳一戳动作时调用；口头说“戳你一下”不会产生动作。
    ``target_user_id`` 必须原样取自当前群聊记录中的用户 ID，禁止猜测、编造，
    也不要填写昵称。群聊目标必须仍在当前群内；私聊只能戳当前联系人。

    Args:
        ctx: 工具执行上下文。
        target_user_id: 当前聊天记录中真实存在的目标用户 ID。

    Returns:
        请求是否已提交，或未执行的具体原因。
    """
    tool_ctx = ctx.deps
    bot = tool_ctx.bot
    ev = tool_ctx.ev
    target = str(target_user_id or "").strip()
    if bot is None or ev is None:
        return "戳一戳未执行：当前没有可用的会话连接"
    if not target:
        return "戳一戳未执行：目标用户 ID 为空"
    if target == str(ev.bot_self_id or ""):
        return "戳一戳未执行：不能戳自己"

    is_direct = ev.user_type == "direct"
    scope_id = str(ev.user_id if is_direct else ev.group_id or "")
    if not scope_id:
        return "戳一戳未执行：当前会话缺少目标信息"
    if is_direct:
        if target != str(ev.user_id or ""):
            return "戳一戳未执行：私聊中只能戳当前联系人"
    else:
        members = await bot.get_group_member_list(timeout=5.0)
        if members is None:
            return "戳一戳未执行：暂时无法确认当前群成员"
        member_ids = {
            str(member.get("user_id", ""))
            for member in members
            if isinstance(member, dict) and member.get("user_id") is not None
        }
        if target not in member_ids:
            return "戳一戳未执行：目标用户不在当前群聊中"

    now = time.monotonic()
    turn_id = str(tool_ctx.extra.get("agent_run_id", ""))
    turn_key = (str(ev.session_id or ""), turn_id)
    if turn_id and turn_key in _TURN_POKE_AT:
        return "戳一戳未执行：本轮已经戳过一位用户了"

    cooldown_key = (str(ev.bot_self_id or ""), scope_id, target)
    last_at = _LAST_POKE_AT.get(cooldown_key, 0.0)
    remaining = POKE_USER_COOLDOWN_SEC - (now - last_at)
    if remaining > 0:
        return f"戳一戳未执行：操作过于频繁，请约 {int(remaining) + 1} 秒后再试"

    # 校验完成后、首次出站 await 前占位，避免同一模型并行调用绕过节流。
    _LAST_POKE_AT[cooldown_key] = now
    if turn_id:
        _TURN_POKE_AT[turn_key] = now
    _cleanup_throttle(now)

    try:
        submitted = await bot.poke_user(target)
    except Exception:
        _LAST_POKE_AT.pop(cooldown_key, None)
        if turn_id:
            _TURN_POKE_AT.pop(turn_key, None)
        raise
    if not submitted:
        _LAST_POKE_AT.pop(cooldown_key, None)
        if turn_id:
            _TURN_POKE_AT.pop(turn_key, None)
        return "戳一戳未执行：当前连接不支持该操作"

    logger.info(
        t(
            "log.ai.buildintools_poke_user_submitted",
            bot_self_id=ev.bot_self_id,
            scope_id=scope_id,
            target_user_id=target,
        )
    )
    return "戳一戳请求已提交"
