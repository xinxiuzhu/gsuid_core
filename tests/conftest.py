"""离线测试启用真实 AI 模块，不修改部署配置或调用模型。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic_ai.models import override_allow_model_requests

from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.utils.plugins_config.models import GsBoolConfig

# 注册表与 RAG 在首次 import 时读取总开关；只改当前进程内存，不落盘。
enable = ai_config.config["enable"]
assert isinstance(enable, GsBoolConfig)
enable.data = True


@pytest.fixture(autouse=True)
def prevent_model_requests() -> Iterator[None]:
    with override_allow_model_requests(False):
        yield
