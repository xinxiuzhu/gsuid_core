"""保留心跳新鲜度与本地戳一戳回归；寻址和资金红线由专项测试覆盖。"""

import time
import inspect
from typing import Literal

from gsuid_core.message_history.manager import MessageRecord
from gsuid_core.ai_core.heartbeat.decision import (
    DECISION_USER_TEMPLATE,
    PROACTIVE_MESSAGE_USER_TEMPLATE,
    build_staleness_section,
)
from gsuid_core.ai_core.interaction_scaffold import MAIN_AGENT_CORE_TOOLS, SLIM_GROUP_CORE_TOOLS


def _msg(role: Literal["user", "assistant"], minutes_ago: float, now: float) -> MessageRecord:
    return MessageRecord(role=role, content="", user_id="test-user", timestamp=now - minutes_ago * 60)


def test_stale_topic_triggers_note() -> None:
    """最后人类消息 35 分钟前时，不应继续附和旧话题。"""
    now = time.time()
    history = [_msg("user", 39, now), _msg("user", 35, now)]
    note = build_staleness_section(history, now)
    assert "35 分钟前" in note
    assert "不要" in note and "接话" in note.replace("'", "")


def test_fresh_topic_no_note() -> None:
    now = time.time()
    history = [_msg("user", 3, now)]
    assert build_staleness_section(history, now) == ""


def test_assistant_message_also_anchors_staleness() -> None:
    """bot 刚发过言也应刷新时间锚；空历史不注入冷场提示。"""
    now = time.time()
    assert build_staleness_section([_msg("assistant", 5, now)], now) == ""
    assert build_staleness_section([_msg("assistant", 40, now)], now) != ""
    assert build_staleness_section([_msg("user", 40, now), _msg("assistant", 5, now)], now) == ""
    assert build_staleness_section([], now) == ""


def test_templates_carry_staleness_and_addressing() -> None:
    assert "{staleness_section}" in DECISION_USER_TEMPLATE
    assert "{staleness_section}" in PROACTIVE_MESSAGE_USER_TEMPLATE
    assert "不是主人发的就绝不称" in PROACTIVE_MESSAGE_USER_TEMPLATE


def test_proactive_poke_requires_real_tool_and_explicit_target() -> None:
    assert "必须调用 poke_user 工具" in PROACTIVE_MESSAGE_USER_TEMPLATE
    assert "target_user_id 只能原样选择" in PROACTIVE_MESSAGE_USER_TEMPLATE
    assert "每次最多戳一人" in PROACTIVE_MESSAGE_USER_TEMPLATE


def test_poke_survives_unified_channel_kernel() -> None:
    assert "poke_user" in MAIN_AGENT_CORE_TOOLS
    assert "poke_user" in SLIM_GROUP_CORE_TOOLS
    assert SLIM_GROUP_CORE_TOOLS == frozenset(MAIN_AGENT_CORE_TOOLS)
    assert "capability_map" in MAIN_AGENT_CORE_TOOLS
    assert "check_delegation" not in MAIN_AGENT_CORE_TOOLS
    assert "web_search_tool" not in MAIN_AGENT_CORE_TOOLS


def test_no_tool_reminder_exempts_chitchat() -> None:
    """闲聊计数与催工具提示共用豁免口径，不依赖已移除的 LIGHT 档位。"""
    from gsuid_core.ai_core.const import _PROGRESSIVE_TOOLS_SKIP_INTENTS
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    assert "闲聊" in _PROGRESSIVE_TOOLS_SKIP_INTENTS
    inject_src = inspect.getsource(GsCoreAIAgent._run_once_prepare_user_message)
    count_src = inspect.getsource(GsCoreAIAgent._run_once_settle_result)
    assert "st.intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS" in inject_src
    assert "st.intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS" in count_src
