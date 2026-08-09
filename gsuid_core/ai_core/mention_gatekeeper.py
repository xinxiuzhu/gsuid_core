"""
提及门控模块

当用户在群里呼唤 AI 名字时，先经过频率限制，
决定是否调用大模型响应，避免大群里高频提及导致成本爆炸。

流程：
1. 硬限检查：同一群 10 分钟内已响应 ≥20 次 → 直接忽略（零成本）
2. 通过 → 允许调用大模型
"""

import time
from typing import Dict, Tuple
from collections import defaultdict

from gsuid_core.logger import logger

# ============== 配置 ==============
# 硬限：时间窗口（秒）
RATE_LIMIT_WINDOW = 10 * 60  # 10 分钟
# 硬限：窗口内最大响应次数
RATE_LIMIT_MAX_COUNT = 20


class MentionGatekeeper:
    """提及门控器"""

    def __init__(self):
        # 记录每个群的响应时间戳
        # key: group_id, value: list[timestamp]
        self._response_times: Dict[str, list[float]] = defaultdict(list)

    def _clean_old_records(self, group_id: str) -> None:
        """清理过期的响应记录"""
        now = time.time()
        cutoff = now - RATE_LIMIT_WINDOW
        self._response_times[group_id] = [
            t for t in self._response_times[group_id] if t > cutoff
        ]

    def check_rate_limit(self, group_id: str) -> Tuple[bool, int]:
        """
        检查频率限制

        Args:
            group_id: 群组 ID

        Returns:
            (是否允许, 当前窗口内已响应次数)
        """
        if not group_id:
            return True, 0

        self._clean_old_records(group_id)
        current_count = len(self._response_times[group_id])

        if current_count >= RATE_LIMIT_MAX_COUNT:
            logger.info(
                f"🚪 [MentionGate] 群 {group_id} 频率限制: "
                f"{current_count}/{RATE_LIMIT_MAX_COUNT} 次/10分钟"
            )
            return False, current_count

        return True, current_count

    def record_response(self, group_id: str) -> None:
        """记录一次响应"""
        if group_id:
            self._response_times[group_id].append(time.time())

    def should_respond(self, group_id: str) -> bool:
        """
        判断是否应该响应

        Args:
            group_id: 群组 ID

        Returns:
            是否应该响应
        """
        # 1. 硬限检查
        allowed, current_count = self.check_rate_limit(group_id)
        if not allowed:
            return False

        # 2. 通过，记录响应
        self.record_response(group_id)
        return True


# 全局单例
_gatekeeper = MentionGatekeeper()


def get_gatekeeper() -> MentionGatekeeper:
    """获取提及门控器实例"""
    return _gatekeeper
