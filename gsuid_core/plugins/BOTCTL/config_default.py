"""BOTCTL 插件 - 默认配置（网页控制台可编辑）

机器人实例自动发现（实例名 {前缀}{序号}nuo{QQ}），端口映射自动解析。
本配置仅存放登录鉴权与网络相关的少量参数。
"""

from gsuid_core.utils.plugins_config.models import (
    GSC,
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
        True,
    ),
}
