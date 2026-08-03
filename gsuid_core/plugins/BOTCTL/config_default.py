"""BOTCTL 插件 - 默认配置（网页控制台可编辑）

机器人实例自动发现（实例名 {前缀}{序号}nuo{QQ}），端口映射自动解析。
本配置仅存放登录鉴权与网络相关的少量参数。
"""

from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
)

CONFIG_DEFAULT: dict[str, GSC] = {
    "webui_token": GsStrConfig(
        "登录密码",
        "机器人后台的登录密码，此处填明文即可，程序会自动加密",
        "",
        secret=True,
    ),
    "host": GsStrConfig(
        "服务器地址",
        "机器人所在服务器的地址（Docker 部署可使用 host.docker.internal）",
        "host.docker.internal",
    ),
    "auto_enable_bypass": GsBoolConfig(
        "重启前自动开启反检测",
        "通过 BOTCTL 重启或软重启 NapCat 前，自动开启六项反检测与 O3 Hook",
        False,
    ),
    "image": GsStrConfig(
        "NapCat 镜像",
        "dk开号创建容器使用的镜像，格式 仓库名:标签",
        "mlikiowa/napcat-docker:v4.17.5",
    ),
    "prefix": GsStrConfig(
        "容器名前缀",
        "新容器命名为 {前缀}{序号}nuo{用户QQ}",
        "fb",
    ),
    "serial_start": GsIntConfig(
        "序号起始值",
        "dk开号自动分配序号的下限（自动避让已占用序号与端口）",
        20000,
    ),
    "napcat_port_offset": GsIntConfig(
        "OneBot端口偏移",
        "宿主 OneBot 端口 = 序号 + 偏移（如序号20012 -> 30012）",
        10000,
    ),
    "webui_port_offset": GsIntConfig(
        "WebUI端口偏移",
        "宿主 WebUI 端口 = 序号 + 偏移（如序号20012 -> 40012）",
        20000,
    ),
    "host_qqbot_dir": GsStrConfig(
        "宿主机QQBot目录",
        "映射到容器 /app/qqbot 的宿主机目录",
        "/vol2/1000/qqbot",
    ),
    "host_napcat_config_dir": GsStrConfig(
        "宿主机NapCat配置目录",
        "映射到容器 /app/napcat/config 的宿主机目录，新号配置文件直写该目录",
        "/vol2/1000/qqbot/napcat/napcat/config",
    ),
    "napcat_data_dir": GsStrConfig(
        "容器内QQ登录数据目录",
        "换号时清理的容器内 QQ 登录数据目录",
        "/app/.config/QQ",
    ),
    "napcat_uid": GsStrConfig(
        "NAPCAT_UID",
        "创建容器时设置的环境变量 NAPCAT_UID",
        "1000",
    ),
    "napcat_gid": GsStrConfig(
        "NAPCAT_GID",
        "创建容器时设置的环境变量 NAPCAT_GID",
        "1000",
    ),
    "napcat_config_dir": GsStrConfig(
        "NapCat 容器配置目录",
        "NapCat 容器内部的配置目录，默认镜像通常为 /app/napcat/config",
        "/app/napcat/config",
    ),
    "new_account_ws_name": GsStrConfig(
        "新号反向WS名称",
        "dk新号生成的默认反向 WebSocket 连接名称",
        "trss",
    ),
    "new_account_ws_url": GsStrConfig(
        "新号反向WS地址",
        "dk新号生成的默认反向 WebSocket 地址",
        "ws://172.17.0.1:2536/OneBotv11",
    ),
    "new_account_ws_token": GsStrConfig(
        "新号反向WS Token",
        "默认反向 WebSocket Token；没有鉴权可留空",
        "",
        secret=True,
    ),
    "new_account_ws_clients_json": GsStrConfig(
        "新号连接JSON",
        "可选：填写 websocketClients 数组 JSON 后，将覆盖上面的单连接设置，支持一次生成多个连接",
        "",
        secret=True,
    ),
    "new_account_create_protocol": GsBoolConfig(
        "生成Protocol兼容配置",
        "dk新号时同时生成 napcat_protocol_<QQ>.json；新版会自动生成，保留可兼容旧流程",
        True,
    ),
    "new_account_sync_global_template": GsBoolConfig(
        "同步全局新号模板",
        "dk新号时同时更新 onebot11.json，未预生成账号文件的新号也会自动继承连接",
        True,
    ),
}
