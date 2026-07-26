"""Persona-aware poke interaction handling."""

import time
import asyncio
from uuid import uuid4
from typing import Optional

from pydantic import Field, BaseModel

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event

POKE_RESPONSE_COOLDOWN_SEC = 30.0
POKE_AI_TIMEOUT_SEC = 8.0
POKE_AI_MAX_CONCURRENCY = 3

_state_lock = asyncio.Lock()
_last_response_at: dict[str, float] = {}
_active_ai_requests = 0


class PokeReaction(BaseModel):
    """Low-level model decision for one poke interaction."""

    text: str = Field(default="", max_length=24)
    mood: str = Field(default="", max_length=16)
    poke_back: bool = False


async def _accept_session(session_id: str) -> bool:
    now = time.monotonic()
    async with _state_lock:
        last = _last_response_at.get(session_id, 0.0)
        if now - last < POKE_RESPONSE_COOLDOWN_SEC:
            return False
        _last_response_at[session_id] = now
        if len(_last_response_at) > 4096:
            cutoff = now - POKE_RESPONSE_COOLDOWN_SEC
            stale = [key for key, value in _last_response_at.items() if value < cutoff]
            for key in stale:
                _last_response_at.pop(key, None)
        return True


async def _try_acquire_ai_slot() -> bool:
    global _active_ai_requests
    async with _state_lock:
        if _active_ai_requests >= POKE_AI_MAX_CONCURRENCY:
            return False
        _active_ai_requests += 1
        return True


async def _release_ai_slot() -> None:
    global _active_ai_requests
    async with _state_lock:
        _active_ai_requests = max(0, _active_ai_requests - 1)


def _user_name(ev: Event) -> Optional[str]:
    sender = ev.sender or {}
    value = sender.get("card") or sender.get("nickname") or sender.get("name")
    return str(value) if value else None


def _record_incoming_poke(ev: Event, persona_name: str) -> None:
    from gsuid_core.message_history import get_history_manager

    get_history_manager().add_message(
        event=ev,
        role="user",
        content="<互动：戳了你一下>",
        user_name=_user_name(ev),
        metadata={
            "type": "interaction",
            "interaction": "poke",
            "direction": "incoming",
            "actor_id": str(ev.get_meta("user_id", ev.user_id) or ""),
            "target_id": str(ev.get_meta("target_id", "") or ""),
            "persona_name": persona_name,
        },
    )


def _record_poke_back_attempt(ev: Event, persona_name: str) -> None:
    from gsuid_core.message_history import get_history_manager

    get_history_manager().add_message(
        event=ev,
        role="assistant",
        content="<互动动作：尝试回戳当前用户>",
        user_name="AI",
        metadata={
            "type": "platform_action",
            "interaction": "poke_back",
            "direction": "outgoing",
            "status": "attempted",
            "target_user_id": str(ev.get_meta("user_id", ev.user_id) or ""),
            "persona_name": persona_name,
        },
    )


async def _send_persona_meme(
    bot: Bot,
    ev: Event,
    persona_name: str,
    mood: str = "",
) -> bool:
    from gsuid_core.ai_core.meme.config import meme_config

    if not meme_config.get_config("meme_enable").data:
        return False

    from gsuid_core.ai_core.meme.selector import pick
    from gsuid_core.ai_core.buildin_tools.meme_tools import send_meme_record

    try:
        record, _ = await pick(
            mood=mood,
            scene="戳一戳互动",
            persona=persona_name,
            session_id=ev.session_id,
            fallback_common=False,
        )
        if record is None and mood:
            record, _ = await pick(
                mood="",
                scene="",
                persona=persona_name,
                session_id=ev.session_id,
                fallback_common=False,
            )
        if record is None:
            return False
        return await send_meme_record(
            bot,
            ev,
            record,
            mood=mood,
            scene="戳一戳互动",
            persona_name=persona_name,
            interaction="poke_meme",
            observe_memory=False,
        )
    except Exception as exc:
        logger.warning(t("[Poke] 发送人格表情失败: {exc}", exc=exc))
        return False


async def _generate_reaction(ev: Event, persona_name: str) -> Optional[PokeReaction]:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return None
    from gsuid_core.ai_core.startup import is_ai_core_ready

    if not is_ai_core_ready():
        return None

    from gsuid_core.ai_core.budget import budget_manager

    group_id = str(ev.group_id) if ev.group_id else ""
    user_id = str(ev.user_id)
    bot_id = ev.bot_id or ""
    try:
        decision = await budget_manager.evaluate(group_id, user_id, bot_id)
        if not decision.allowed:
            return None
    except Exception as exc:
        logger.warning(t("[Poke] 预算预检失败，继续尝试低级模型: {exc}", exc=exc))

    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.history_format import format_history_for_agent
    from gsuid_core.ai_core.context_assembly import build_session_system_prompt

    history = get_history_manager().get_history(ev, limit=10)
    history_context = format_history_for_agent(
        history,
        current_user_id=str(ev.user_id),
        current_user_name=_user_name(ev),
    )
    persona_prompt = await build_session_system_prompt(ev, persona_name)
    system_prompt = (
        f"{persona_prompt}\n\n"
        "你正在处理一次即时的戳一戳互动。请保持当前人格，用自然、简短的方式回应。"
        "text 不超过 24 个汉字；mood 只写一个简短情绪词；可以决定是否回戳。"
        "不要解释系统机制，不要声称执行了尚未执行的动作，也不要请求或调用其他工具。"
    )
    prompt = (
        f"{history_context}\n\n"
        "请对刚刚的戳一戳作出一次回应，并返回结构化结果。"
    )
    agent = create_agent(
        system_prompt=system_prompt,
        persona_name=persona_name,
        create_by="PokeResponse",
        max_iterations=1,
        max_history=0,
        task_level="low",
        session_id=f"poke_{uuid4().hex}",
        is_subagent=True,
        dynamic_tools=False,
        wall_clock_budget=POKE_AI_TIMEOUT_SEC,
    )
    try:
        result = await asyncio.wait_for(
            agent.run(
                user_message=prompt,
                bot=None,
                ev=ev,
                tools=[],
                return_mode="return",
                output_type=PokeReaction,
                intent="闲聊",
                budget_gate=True,
            ),
            timeout=POKE_AI_TIMEOUT_SEC,
        )
        return result if isinstance(result, PokeReaction) else None
    except asyncio.TimeoutError:
        logger.info(t("[Poke] 低级模型响应超时，降级为人格表情"))
        return None
    except Exception as exc:
        logger.warning(t("[Poke] 低级模型响应失败，降级为人格表情: {exc}", exc=exc))
        return None
    finally:
        agent._session_logger.close()


async def handle_persona_poke(bot: Bot, ev: Event) -> None:
    """Handle one validated meta-poke event without queueing excess AI work."""
    target_id = str(ev.get_meta("target_id", "") or "")
    user_id = str(ev.get_meta("user_id", ev.user_id) or "")
    if not target_id or target_id != str(ev.bot_self_id):
        return
    if not user_id or user_id == str(ev.bot_self_id):
        return

    from gsuid_core.ai_core.persona.config import persona_config_manager

    try:
        persona_name = persona_config_manager.get_persona_for_session(ev.session_id)
    except Exception:
        return
    if not persona_name:
        return
    persona_config = persona_config_manager.get_config(persona_name)
    if not persona_config.get_config("poke_response_enabled").data:
        return
    if not await _accept_session(ev.session_id):
        return

    _record_incoming_poke(ev, persona_name)
    if not await _try_acquire_ai_slot():
        return

    try:
        reaction = await _generate_reaction(ev, persona_name)
        if reaction is None:
            await _send_persona_meme(bot, ev, persona_name)
            return

        text = reaction.text.strip()
        mood = reaction.mood.strip()
        if text:
            await bot.send(
                text,
                extra_metadata={
                    "interaction": "poke_response",
                    "reply_to_user_id": user_id,
                    "persona_name": persona_name,
                    "mood": mood,
                },
                observe_memory=False,
            )
        if mood:
            await _send_persona_meme(bot, ev, persona_name, mood)
        if reaction.poke_back:
            await bot.poke()
            _record_poke_back_attempt(ev, persona_name)
    finally:
        await _release_ai_slot()
