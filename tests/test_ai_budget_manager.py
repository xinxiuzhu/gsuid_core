"""AI 预算 group_each 语义与 WebConsole 辅助函数回归测试。"""

import time
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gsuid_core.ai_core.budget.models import AIBudgetRule
from gsuid_core.ai_core.budget.manager import BudgetManager, _UsageRow


def _run(coro):
    return asyncio.run(coro)


def _rule(**overrides) -> AIBudgetRule:
    data = {
        "id": 1,
        "name": "全局单群默认额度",
        "scope_type": "group_each",
        "scope_id": "",
        "member_id": "",
        "bot_id": "",
        "enabled": True,
        "priority": 10,
        "period_mode": "rolling",
        "short_window_hours": 5,
        "limit_short": 100,
        "limit_day": 0,
        "limit_week": 0,
    }
    data.update(overrides)
    return AIBudgetRule(**data)


def _usage(group_id: str, tokens: int, *, bot_id: str = "bot", age: int = 0) -> _UsageRow:
    return _UsageRow(
        group_id=group_id,
        user_id="u1",
        bot_id=bot_id,
        session_id=f"session-{group_id}",
        input_tokens=tokens,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        exempt=False,
        created_at=int(time.time()) - age,
    )


def _new_manager() -> BudgetManager:
    manager = object.__new__(BudgetManager)
    manager._init()
    return manager


def _manager_with(rule: AIBudgetRule, usage: list[_UsageRow]) -> BudgetManager:
    manager = _new_manager()
    manager._usage = usage
    manager._rules_cache = [rule]
    manager._whitelist_cache = []
    manager._cache_ts = time.time()
    return manager


def _config_value(key: str):
    values = {
        "enable": True,
        "count_mode": "input_output",
        "count_exempt_usage": False,
        "exempt_masters": False,
    }
    return SimpleNamespace(data=values[key])


def test_group_each_matches_only_groups_and_stacks_with_other_rules() -> None:
    group_each = _rule()
    group = _rule(id=2, scope_type="group", scope_id="g1")
    global_rule = _rule(id=3, scope_type="global")

    assert BudgetManager._rule_matches(group_each, "g1", "u1", "bot")
    assert not BudgetManager._rule_matches(group_each, "", "u1", "bot")
    assert BudgetManager._rule_matches(group, "g1", "u1", "bot")
    assert BudgetManager._rule_matches(global_rule, "g1", "u1", "bot")


def test_group_each_evaluate_isolates_actual_group_and_labels_it() -> None:
    rule = _rule(limit_short=100)
    manager = _manager_with(rule, [_usage("g1", 110), _usage("g2", 20)])

    with patch(
        "gsuid_core.ai_core.budget.manager.budget_config.get_config",
        side_effect=_config_value,
    ):
        g1 = _run(manager.evaluate("g1", "u1", "bot"))
        g2 = _run(manager.evaluate("g2", "u1", "bot"))
        private = _run(manager.evaluate("", "u1", "bot"))

    assert not g1.allowed
    assert g1.block_scope_label == "全局单群（群 g1）"
    assert g1.rule_statuses[0].effective_group_id == "g1"
    assert g1.rule_statuses[0].windows[0].used == 110
    assert g2.allowed
    assert g2.rule_statuses[0].windows[0].used == 20
    assert private.allowed
    assert private.rule_statuses == []


def test_group_each_summary_keeps_groups_separate() -> None:
    rule = _rule(limit_short=100, bot_id="bot")
    manager = _manager_with(
        rule,
        [
            _usage("g1", 60),
            _usage("g1", 50),
            _usage("g2", 80),
            _usage("", 1000),
            _usage("g3", 500, bot_id="other"),
        ],
    )

    with patch(
        "gsuid_core.ai_core.budget.manager.budget_config.get_config",
        side_effect=_config_value,
    ):
        summary = _run(manager.group_each_usage_summary(rule))

    assert summary.active_group_count == 2
    assert summary.blocked_group_count == 1
    assert summary.top_group_id == "g1"
    assert summary.top_utilization == 1.1
    assert summary.top_status is not None
    assert summary.top_status.effective_group_id == "g1"
    assert summary.top_status.scope_label == "全局单群（群 g1）"
    assert summary.top_status.windows[0].used == 110


def test_reset_rejects_abstract_group_each_scope() -> None:
    manager = _new_manager()
    try:
        _run(manager.reset_scope("group_each"))
    except ValueError as exc:
        assert "具体维度" in str(exc)
    else:
        raise AssertionError("group_each reset should be rejected")


def test_api_helpers_normalize_scope_and_validate_group_each() -> None:
    from gsuid_core.webconsole.budget_api import (
        _limits_valid,
        _normalize_rule_scope,
        _validate_rule_fields,
    )

    assert _validate_rule_fields("group_each", "stale", "stale", "rolling", 5) is None
    assert _normalize_rule_scope("group_each", " stale ", " member ") == ("", "")
    assert _normalize_rule_scope("global", " stale ", " member ") == ("", "")
    assert _normalize_rule_scope("group", " g1 ", " member ") == ("g1", "")
    assert _normalize_rule_scope("user", " u1 ", " member ") == ("u1", "")
    assert _normalize_rule_scope("member", " g1 ", " u1 ") == ("g1", "u1")
    assert _limits_valid(0, 1, 0)
    assert not _limits_valid(0, 0, 0)


def test_update_rejects_final_all_zero_limits_and_normalizes_dimension_switch() -> None:
    from gsuid_core.webconsole.budget_api import UpdateRuleRequest, update_rule

    existing = _rule(scope_type="member", scope_id="g1", member_id="u1", limit_short=5)
    with (
        patch.object(AIBudgetRule, "get_rule", new=AsyncMock(return_value=existing)),
        patch.object(AIBudgetRule, "update_data_by_data", new=AsyncMock()) as update_data,
    ):
        rejected = _run(update_rule(1, UpdateRuleRequest(limit_short=0), {}))
        assert rejected["status"] == 1
        update_data.assert_not_awaited()

    updated = _rule(scope_type="group_each", scope_id="", member_id="", limit_short=5)
    with (
        patch.object(AIBudgetRule, "get_rule", new=AsyncMock(side_effect=[existing, updated])),
        patch.object(AIBudgetRule, "update_data_by_data", new=AsyncMock()) as update_data,
    ):
        result = _run(
            update_rule(
                1,
                UpdateRuleRequest(scope_type="group_each", scope_id="stale", member_id="stale"),
                {},
            )
        )

    assert result["status"] == 0
    payload = update_data.await_args.kwargs["update_data"]
    assert payload["scope_id"] == ""
    assert payload["member_id"] == ""


def test_scope_query_and_reset_api_reject_group_each() -> None:
    from gsuid_core.webconsole.budget_api import ResetRequest, get_scope_usage, reset_scope_usage

    scope_result = _run(get_scope_usage("group_each", "", "", "", {}))
    reset_result = _run(reset_scope_usage(ResetRequest(scope_type="group_each"), {}))

    assert scope_result["status"] == 1
    assert reset_result["status"] == 1


def test_group_each_rule_detail_returns_summary_instead_of_aggregate_usage() -> None:
    from gsuid_core.webconsole.budget_api import get_rule
    from gsuid_core.ai_core.budget.manager import RuleStatus, WindowStatus, GroupEachUsageSummary

    rule = _rule()
    top_status = RuleStatus(
        rule_id=1,
        rule_name=rule.name,
        scope_type="group_each",
        scope_label="全局单群（群 g1）",
        period_mode="rolling",
        blocked=True,
        effective_group_id="g1",
        windows=[WindowStatus("short", 18000, 100, 110, 0, True, None)],
    )
    summary = GroupEachUsageSummary(2, 1, "g1", 1.1, top_status)
    with (
        patch.object(AIBudgetRule, "get_rule", new=AsyncMock(return_value=rule)),
        patch(
            "gsuid_core.webconsole.budget_api.budget_manager.group_each_usage_summary",
            new=AsyncMock(return_value=summary),
        ),
        patch(
            "gsuid_core.webconsole.budget_api.budget_manager.rule_live_status",
            new=AsyncMock(),
        ) as live_status,
    ):
        result = _run(get_rule(1, {}))

    assert result["status"] == 0
    assert "usage_summary" in result["data"]
    assert "usage" not in result["data"]
    assert result["data"]["usage_summary"]["top_status"]["effective_group_id"] == "g1"
    live_status.assert_not_awaited()


def test_overview_reports_blocked_group_each_with_summary() -> None:
    from gsuid_core.ai_core.budget.models import AIBudgetWhitelist
    from gsuid_core.webconsole.budget_api import get_overview
    from gsuid_core.ai_core.budget.manager import RuleStatus, WindowStatus, GroupEachUsageSummary

    rule = _rule()
    top_status = RuleStatus(
        rule_id=1,
        rule_name=rule.name,
        scope_type="group_each",
        scope_label="全局单群（群 g1）",
        period_mode="rolling",
        blocked=True,
        effective_group_id="g1",
        windows=[WindowStatus("short", 18000, 100, 110, 0, True, None)],
    )
    summary = GroupEachUsageSummary(2, 1, "g1", 1.1, top_status)
    with (
        patch.object(AIBudgetRule, "get_all_rules", new=AsyncMock(return_value=[rule])),
        patch.object(AIBudgetWhitelist, "get_all_entries", new=AsyncMock(return_value=[])),
        patch(
            "gsuid_core.webconsole.budget_api.budget_manager.group_each_usage_summary",
            new=AsyncMock(return_value=summary),
        ),
        patch("gsuid_core.webconsole.budget_api.budget_manager.usage_total", return_value=110),
        patch("gsuid_core.webconsole.budget_api.budget_manager.top_consumers", return_value=[]),
        patch(
            "gsuid_core.webconsole.budget_api.budget_config.get_config",
            return_value=SimpleNamespace(data=True),
        ),
    ):
        result = _run(get_overview({}))

    blocked = result["data"]["blocked_rules"]
    assert len(blocked) == 1
    assert blocked[0]["scope_type"] == "group_each"
    assert blocked[0]["usage_summary"]["blocked_group_count"] == 1
    assert "usage" not in blocked[0]
