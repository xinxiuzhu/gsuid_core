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


def get_napcat_config_dir() -> str:
    return BOTCTLConfig.get_config("napcat_config_dir").data


def get_new_account_ws_name() -> str:
    return BOTCTLConfig.get_config("new_account_ws_name").data


def get_new_account_ws_url() -> str:
    return BOTCTLConfig.get_config("new_account_ws_url").data


def get_new_account_ws_token() -> str:
    return BOTCTLConfig.get_config("new_account_ws_token").data


def get_new_account_ws_clients_json() -> str:
    return BOTCTLConfig.get_config("new_account_ws_clients_json").data


def get_new_account_create_protocol() -> bool:
    return BOTCTLConfig.get_config("new_account_create_protocol").data


def get_new_account_sync_global_template() -> bool:
    return BOTCTLConfig.get_config("new_account_sync_global_template").data


def get_image() -> str:
    return BOTCTLConfig.get_config("image").data


def get_prefix() -> str:
    return BOTCTLConfig.get_config("prefix").data


def get_serial_start() -> int:
    return BOTCTLConfig.get_config("serial_start").data


def get_napcat_port_offset() -> int:
    return BOTCTLConfig.get_config("napcat_port_offset").data


def get_webui_port_offset() -> int:
    return BOTCTLConfig.get_config("webui_port_offset").data


def get_host_qqbot_dir() -> str:
    return BOTCTLConfig.get_config("host_qqbot_dir").data


def get_host_napcat_config_dir() -> str:
    return BOTCTLConfig.get_config("host_napcat_config_dir").data


def get_napcat_data_dir() -> str:
    return BOTCTLConfig.get_config("napcat_data_dir").data


def get_napcat_uid() -> str:
    return BOTCTLConfig.get_config("napcat_uid").data


def get_napcat_gid() -> str:
    return BOTCTLConfig.get_config("napcat_gid").data
