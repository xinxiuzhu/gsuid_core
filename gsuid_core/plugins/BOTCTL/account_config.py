"""NapCat 新账号配置模板与合并逻辑。"""

import re
import json
from copy import deepcopy
from typing import Any, Dict, List, Optional

_QQ_RE = re.compile(r"[1-9]\d{4,11}")
_BYPASS_KEYS = ("hook", "window", "module", "process", "container", "js")

_DEFAULT_CORE_CONFIG: Dict[str, Any] = {
    "fileLog": False,
    "consoleLog": True,
    "fileLogLevel": "debug",
    "consoleLogLevel": "info",
    "packetBackend": "auto",
    "packetServer": "",
    "o3HookMode": 1,
    "autoTimeSync": True,
}

_DEFAULT_ONEBOT_CONFIG: Dict[str, Any] = {
    "network": {
        "httpServers": [],
        "httpSseServers": [],
        "httpClients": [],
        "websocketServers": [],
        "websocketClients": [],
        "plugins": [],
    },
    "musicSignUrl": "",
    "enableLocalFile2Url": False,
    "parseMultMsg": False,
    "imageDownloadProxy": "",
    "timeout": {
        "baseTimeout": 10000,
        "uploadSpeedKBps": 256,
        "downloadSpeedKBps": 256,
        "maxTimeout": 1800000,
    },
}

_DEFAULT_PROTOCOL_CONFIG: Dict[str, Any] = {
    "enable": False,
    "network": {
        "httpServers": [],
        "websocketServers": [],
        "websocketClients": [],
    },
}

_DEFAULT_WS_CLIENT: Dict[str, Any] = {
    "enable": True,
    "name": "trss",
    "url": "ws://172.17.0.1:2536/OneBotv11",
    "reportSelfMessage": False,
    "messagePostFormat": "array",
    "token": "",
    "debug": False,
    "heartInterval": 30000,
    "reconnectInterval": 30000,
}


def normalize_qq(value: str) -> str:
    qq = str(value).strip()
    if not _QQ_RE.fullmatch(qq):
        raise ValueError("QQ号必须是5到12位数字，且不能以0开头")
    return qq


def build_ws_clients(
    name: str,
    url: str,
    token: str = "",
    custom_json: str = "",
) -> List[Dict[str, Any]]:
    """构造要注入的反向 WebSocket 客户端列表。"""
    if custom_json.strip():
        try:
            raw_clients = json.loads(custom_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"自定义连接 JSON 格式错误: {e}") from e
        if not isinstance(raw_clients, list) or not raw_clients:
            raise ValueError("自定义连接 JSON 必须是非空数组")
    else:
        raw_clients = [{"name": name, "url": url, "token": token}]

    clients: List[Dict[str, Any]] = []
    names = set()
    for index, raw_client in enumerate(raw_clients, 1):
        if not isinstance(raw_client, dict):
            raise ValueError(f"第 {index} 个连接必须是 JSON 对象")
        client = {**_DEFAULT_WS_CLIENT, **raw_client}
        client_name = client.get("name")
        client_url = client.get("url")
        if not isinstance(client_name, str) or not client_name.strip():
            raise ValueError(f"第 {index} 个连接缺少有效 name")
        if not isinstance(client_url, str) or not client_url.strip():
            raise ValueError(f"第 {index} 个连接缺少有效 url")
        if client_name in names:
            raise ValueError(f"连接名称重复: {client_name}")
        names.add(client_name)
        clients.append(client)
    return clients


def build_onebot_config(
    existing: Optional[Dict[str, Any]],
    generated_clients: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """保留现有连接，并按 name/url 幂等新增或更新模板连接。"""
    config = deepcopy(_DEFAULT_ONEBOT_CONFIG)
    if isinstance(existing, dict):
        for key, value in existing.items():
            if key != "network":
                config[key] = deepcopy(value)
        if isinstance(existing.get("network"), dict):
            config["network"].update(deepcopy(existing["network"]))

    network = config["network"]
    for key in (
        "httpServers",
        "httpSseServers",
        "httpClients",
        "websocketServers",
        "websocketClients",
        "plugins",
    ):
        if not isinstance(network.get(key), list):
            network[key] = []

    clients = network["websocketClients"]
    for generated in generated_clients:
        matched_index = next(
            (
                index
                for index, current in enumerate(clients)
                if isinstance(current, dict)
                and (
                    current.get("name") == generated["name"]
                    or current.get("url") == generated["url"]
                )
            ),
            None,
        )
        if matched_index is None:
            clients.append(deepcopy(generated))
        else:
            clients[matched_index] = {
                **clients[matched_index],
                **deepcopy(generated),
            }
    return config


def build_core_config(
    existing: Optional[Dict[str, Any]],
    fallback: Optional[Dict[str, Any]],
    enable_bypass: bool,
) -> Dict[str, Any]:
    config = deepcopy(_DEFAULT_CORE_CONFIG)
    if isinstance(fallback, dict):
        config.update(deepcopy(fallback))
    if isinstance(existing, dict):
        config.update(deepcopy(existing))
    if enable_bypass:
        config["o3HookMode"] = 1
        config["bypass"] = {key: True for key in _BYPASS_KEYS}
    return config


def build_protocol_config(
    existing: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    config = deepcopy(_DEFAULT_PROTOCOL_CONFIG)
    if isinstance(existing, dict):
        config.update(deepcopy(existing))
        if isinstance(existing.get("network"), dict):
            config["network"] = {
                **_DEFAULT_PROTOCOL_CONFIG["network"],
                **deepcopy(existing["network"]),
            }
    return config
