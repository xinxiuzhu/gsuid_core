"""BOTCTL 插件 - 配置管理器

使用框架的 StringConfig，配置可在网页控制台编辑，
存储于 data/BOTCTL/config.json。
"""

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .config_default import CONFIG_DEFAULT

CONFIG_PATH = get_res_path("BOTCTL") / "config.json"

BOTCTLConfig = StringConfig(
    "BOTCTL",
    CONFIG_PATH,
    CONFIG_DEFAULT,
)


def get_webui_token() -> str:
    return BOTCTLConfig.get_config("webui_token").data


def get_host() -> str:
    return BOTCTLConfig.get_config("host").data


def get_auto_enable_bypass() -> bool:
    return BOTCTLConfig.get_config("auto_enable_bypass").data
