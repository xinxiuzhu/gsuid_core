import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import find_tool_base
from gsuid_core.ai_core.interaction_scaffold import SLIM_GROUP_CORE_TOOLS
from gsuid_core.ai_core.buildin_tools.poke_user import (
    poke_user,
    clear_poke_user_throttle,
)


def _run(coro):
    return asyncio.run(coro)


def _event(*, user_type: str = "group", user_id: str = "speaker") -> Event:
    return Event(
        bot_id="onebot",
        real_bot_id="onebot",
        bot_self_id="bot",
        user_id=user_id,
        group_id="group" if user_type == "group" else None,
        user_type=user_type,
        WS_BOT_ID="yunzai",
    )


def _ctx(bot, ev: Event, run_id: str = "run-1") -> SimpleNamespace:
    return SimpleNamespace(
        deps=ToolContext(
            bot=bot,
            ev=ev,
            extra={"agent_run_id": run_id},
        )
    )


def test_poke_user_is_a_structured_slim_group_tool() -> None:
    tool_base = find_tool_base("poke_user")

    assert tool_base is not None
    assert "poke_user" in SLIM_GROUP_CORE_TOOLS
    assert tool_base.tool.function_schema.json_schema == {
        "additionalProperties": False,
        "properties": {
            "target_user_id": {
                "description": "当前聊天记录中真实存在的目标用户 ID。",
                "type": "string",
            }
        },
        "required": ["target_user_id"],
        "type": "object",
    }


def test_poke_user_submits_valid_current_group_member() -> None:
    clear_poke_user_throttle()
    bot = SimpleNamespace(
        get_group_member_list=AsyncMock(return_value=[{"user_id": "target"}]),
        poke_user=AsyncMock(return_value=True),
    )

    result = _run(poke_user(_ctx(bot, _event()), "target"))

    assert result == "戳一戳请求已提交"
    bot.get_group_member_list.assert_awaited_once_with(timeout=5.0)
    bot.poke_user.assert_awaited_once_with("target")


def test_poke_user_rejects_unknown_member_and_self() -> None:
    clear_poke_user_throttle()
    bot = SimpleNamespace(
        get_group_member_list=AsyncMock(return_value=[{"user_id": "known"}]),
        poke_user=AsyncMock(return_value=True),
    )
    ev = _event()

    unknown = _run(poke_user(_ctx(bot, ev), "unknown"))
    self_result = _run(poke_user(_ctx(bot, ev, "run-2"), "bot"))

    assert "不在当前群聊" in unknown
    assert "不能戳自己" in self_result
    bot.poke_user.assert_not_awaited()


def test_poke_user_limits_one_submission_per_agent_run() -> None:
    clear_poke_user_throttle()
    bot = SimpleNamespace(
        get_group_member_list=AsyncMock(return_value=[{"user_id": "first"}, {"user_id": "second"}]),
        poke_user=AsyncMock(return_value=True),
    )
    ctx = _ctx(bot, _event())

    first = _run(poke_user(ctx, "first"))
    second = _run(poke_user(ctx, "second"))

    assert first == "戳一戳请求已提交"
    assert "本轮已经戳过" in second
    bot.poke_user.assert_awaited_once_with("first")


def test_poke_user_private_scope_only_allows_current_contact() -> None:
    clear_poke_user_throttle()
    bot = SimpleNamespace(poke_user=AsyncMock(return_value=True))
    ev = _event(user_type="direct", user_id="friend")

    rejected = _run(poke_user(_ctx(bot, ev), "someone-else"))
    accepted = _run(poke_user(_ctx(bot, ev, "run-2"), "friend"))

    assert "只能戳当前联系人" in rejected
    assert accepted == "戳一戳请求已提交"
    bot.poke_user.assert_awaited_once_with("friend")
