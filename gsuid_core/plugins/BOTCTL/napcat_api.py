"""NapCat WebUI HTTP API 客户端

封装 NapCat WebUI 的鉴权、登录与核心配置接口：
- 鉴权：POST /api/auth/login -> Credential (base64, 1小时有效)
- 受保护请求带 Authorization: Bearer <Credential>
- 状态：POST /api/QQLogin/CheckLoginStatus
- 二维码：POST /api/QQLogin/GetQQLoginQrcode / RefreshQRcode
- 软重启：POST /api/QQLogin/RestartNapCat
- 核心配置：GET/POST /api/NapCatConfig/GetConfig / SetConfig
"""

import time
import hashlib
from typing import Any, Dict, Optional

import httpx

from gsuid_core.logger import logger

# Credential 有效期 1 小时，提前 5 分钟刷新
_CRED_TTL = 3600 - 300

_BYPASS_KEYS = (
    "hook",
    "window",
    "module",
    "process",
    "container",
    "js",
)


class NapCatAPIError(Exception):
    """NapCat WebUI 返回的已知错误"""


def _hash_token(token: str) -> str:
    """计算 NapCat WebUI 登录所需的 hash 值

    NapCat WebUI 的 comparePasswordHash 校验：
        generatePasswordHash(password) === hash
    其中 generatePasswordHash = sha256(password + '.napcat') 的 hex 摘要。
    浏览器登录时发送的是这个哈希值，而非明文密码。
    """
    return hashlib.sha256((token + ".napcat").encode()).hexdigest()


class NapCatAPIClient:
    """单个 NapCat WebUI 实例的客户端

    对应一个容器：host + port(webui) + token
    """

    def __init__(self, host: str, port: int, token: str, serial: str = ""):
        self.host = host
        self.port = port
        self.token = token
        self.serial = serial or f"{host}:{port}"
        self._base = f"http://{host}:{port}/api"
        self._cred: Optional[str] = None
        self._cred_expire: float = 0.0

    @property
    def base_url(self) -> str:
        return self._base

    def _is_cred_valid(self) -> bool:
        return bool(self._cred) and time.time() < self._cred_expire

    async def _login(self) -> str:
        """登录获取 Credential

        NapCat WebUI 要求 hash = sha256(token + '.napcat')，
        而非明文 token（浏览器登录同理）。
        """
        url = f"{self._base}/auth/login"
        payload = {"hash": _hash_token(self.token)}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != 0:
            raise NapCatAPIError(
                f"[{self.serial}] WebUI 登录失败: {data.get('message', '未知错误')}"
            )

        cred = data.get("data", {}).get("Credential")
        if not cred:
            raise NapCatAPIError(f"[{self.serial}] WebUI 登录返回无 Credential")

        self._cred = cred
        self._cred_expire = time.time() + _CRED_TTL
        logger.debug(f"[{self.serial}] WebUI 登录成功，credential 已缓存")
        return cred

    async def _request(
        self,
        path: str,
        method: str = "POST",
        payload: Optional[Dict[str, Any]] = None,
        retry: bool = True,
    ) -> Dict[str, Any]:
        """发起受保护请求，自动带 Bearer，失败重登一次"""
        if not self._is_cred_valid():
            await self._login()

        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {self._cred}"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.request(
                method,
                url,
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        # credential 失效 -> 重登重试一次
        msg = data.get("message", "")
        if data.get("code") != 0 and (
            "Authorization Failed" in msg
            or "Token has been revoked" in msg
            or "token" in msg.lower()
        ):
            if retry:
                logger.debug(f"[{self.serial}] credential 失效，重新登录...")
                self._cred = None
                return await self._request(
                    path,
                    method=method,
                    payload=payload,
                    retry=False,
                )
            raise NapCatAPIError(f"[{self.serial}] 鉴权失败: {msg}")

        return data

    async def check_login_status(self) -> Dict[str, Any]:
        """检查 QQ 登录状态

        返回 data: { isLogin, isOffline, qrcodeurl, loginError }
        """
        data = await self._request("/QQLogin/CheckLoginStatus")
        if data.get("code") != 0:
            raise NapCatAPIError(
                f"[{self.serial}] 查询登录状态失败: {data.get('message')}"
            )
        return data.get("data", {}) or {}

    async def get_qrcode(self) -> str:
        """获取登录二维码 URL

        返回二维码图片 URL；若已登录会抛 NapCatAPIError
        """
        data = await self._request("/QQLogin/GetQQLoginQrcode")
        if data.get("code") != 0:
            raise NapCatAPIError(
                f"[{self.serial}] 获取二维码失败: {data.get('message')}"
            )
        url = (data.get("data") or {}).get("qrcode")
        if not url:
            raise NapCatAPIError(f"[{self.serial}] 二维码 URL 为空")
        return url

    async def refresh_qrcode(self) -> None:
        """刷新二维码（强制重新生成）"""
        data = await self._request("/QQLogin/RefreshQRcode")
        if data.get("code") != 0:
            raise NapCatAPIError(
                f"[{self.serial}] 刷新二维码失败: {data.get('message')}"
            )

    async def get_or_refresh_qrcode(self) -> str:
        """先尝试取码，取不到则刷新后再取"""
        try:
            return await self.get_qrcode()
        except NapCatAPIError as e:
            if "QRCode" in str(e) or "empty" in str(e).lower():
                logger.debug(f"[{self.serial}] 二维码未生成，触发刷新...")
                await self.refresh_qrcode()
                return await self.get_qrcode()
            raise

    async def restart_napcat(self) -> str:
        """软重启 NapCat 进程（不重启容器）"""
        data = await self._request("/QQLogin/RestartNapCat")
        if data.get("code") != 0:
            raise NapCatAPIError(
                f"[{self.serial}] 软重启失败: {data.get('message')}"
            )
        return (data.get("data") or {}).get("message", "已发起")

    async def get_napcat_config(self) -> Dict[str, Any]:
        """读取 NapCat 全局核心配置（config/napcat.json）"""
        data = await self._request(
            "/NapCatConfig/GetConfig",
            method="GET",
        )
        if data.get("code") != 0:
            raise NapCatAPIError(
                f"[{self.serial}] 读取核心配置失败: {data.get('message')}"
            )
        return data.get("data", {}) or {}

    async def set_napcat_config(self, patch: Dict[str, Any]) -> None:
        """合并写入 NapCat 全局核心配置"""
        data = await self._request(
            "/NapCatConfig/SetConfig",
            payload=patch,
        )
        if data.get("code") != 0:
            raise NapCatAPIError(
                f"[{self.serial}] 保存核心配置失败: {data.get('message')}"
            )

    async def set_all_bypasses(self, enabled: bool) -> None:
        """统一开关六项反检测与 O3 Hook，重启 NapCat 后生效"""
        await self.set_napcat_config(
            {
                "bypass": {key: enabled for key in _BYPASS_KEYS},
                "o3HookMode": 1 if enabled else 0,
            }
        )

    async def download_image(self, url: str) -> bytes:
        """下载二维码图片 bytes（备用，当前改用 generate_qrcode_image 本地生成）"""
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content


def generate_qrcode_image(url: str) -> bytes:
    """用 Python qrcode 库本地生成二维码 PNG，不依赖 txz.qq.com 下载"""
    from io import BytesIO

    import qrcode

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
