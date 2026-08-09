"""ProxyManager 双 IP 缓存与 429 分类测试。"""

import time
from types import SimpleNamespace

import pytest

from gsuid_core.plugins.ProxyManager.ProxyManager.pm_core.models import Proxy, ProxySlot
from gsuid_core.plugins.ProxyManager.ProxyManager.pm_core.manager import ProxyManager

_DEFAULT_CONFIG = {
    "ProxyCacheSize": 2,
    "DualIPConcurrencyThreshold": 8,
    "Connect429BackoffSeconds": 3,
    "ProxyEnable": True,
    "PrimaryProxyType": "api",
    "PrimaryFailureRateMinSamples": 100,
    "PrimaryFailureRateThresholdPercent": 50,
    "PatrolInterval": 60,
}


@pytest.fixture
def manager(monkeypatch):
    import gsuid_core.plugins.ProxyManager.ProxyManager.pm_core.manager as manager_module

    values = dict(_DEFAULT_CONFIG)
    monkeypatch.setattr(
        manager_module.config,
        "get_config",
        lambda key: SimpleNamespace(data=values[key]),
    )
    proxy_manager = ProxyManager()
    proxy_manager._test_config = values
    return proxy_manager


def _proxy(host: str, expires_in: int = 60) -> Proxy:
    return Proxy(
        url=f"http://{host}:8080",
        is_primary=True,
        source="主代理(api)",
        expires_at=time.time() + expires_in,
    )


@pytest.mark.anyio
async def test_low_concurrency_only_caches_one_proxy(manager, monkeypatch):
    calls = 0

    async def get_primary(excluded=None):
        nonlocal calls
        calls += 1
        return _proxy("10.0.0.1")

    async def get_backup(excluded=None):
        return None

    monkeypatch.setattr(manager, "_get_from_primary", get_primary)
    monkeypatch.setattr(manager, "_get_from_backup", get_backup)

    proxy = await manager.acquire_proxy()

    assert proxy is not None
    assert calls == 1
    assert len(manager._proxy_slots) == 1
    manager.release_proxy(proxy)


@pytest.mark.anyio
async def test_threshold_adds_second_proxy_and_balances(manager, monkeypatch):
    proxies = iter([_proxy("10.0.0.1"), _proxy("10.0.0.2")])

    async def get_primary(excluded=None):
        return next(proxies, None)

    async def get_backup(excluded=None):
        return None

    monkeypatch.setattr(manager, "_get_from_primary", get_primary)
    monkeypatch.setattr(manager, "_get_from_backup", get_backup)

    acquired = [await manager.acquire_proxy() for _ in range(8)]

    assert all(proxy is not None for proxy in acquired)
    assert len(manager._proxy_slots) == 2
    assert acquired[-1].url == "http://10.0.0.2:8080"
    assert sorted(slot.in_flight for slot in manager._proxy_slots) == [1, 7]

    for proxy in acquired:
        manager.release_proxy(proxy)
    assert [slot.in_flight for slot in manager._proxy_slots] == [0, 0]


@pytest.mark.anyio
async def test_concurrent_acquire_reaches_second_proxy(manager, monkeypatch):
    import asyncio

    gate = asyncio.Event()
    calls = 0

    async def get_primary(excluded=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
        return _proxy(f"10.0.0.{calls}")

    async def get_backup(excluded=None):
        return None

    monkeypatch.setattr(manager, "_get_from_primary", get_primary)
    monkeypatch.setattr(manager, "_get_from_backup", get_backup)

    tasks = [asyncio.create_task(manager.acquire_proxy()) for _ in range(8)]
    await asyncio.sleep(0)
    gate.set()
    acquired = await asyncio.gather(*tasks)

    assert calls == 2
    assert len(manager._proxy_slots) == 2
    assert {proxy.url for proxy in acquired if proxy} == {
        "http://10.0.0.1:8080",
        "http://10.0.0.2:8080",
    }
    for proxy in acquired:
        manager.release_proxy(proxy)


@pytest.mark.anyio
async def test_concurrent_capacity_fill_is_single_flight(manager, monkeypatch):
    import asyncio

    calls = 0

    async def get_primary(excluded=None):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return _proxy(f"10.0.0.{calls}")

    async def get_backup(excluded=None):
        return None

    monkeypatch.setattr(manager, "_get_from_primary", get_primary)
    monkeypatch.setattr(manager, "_get_from_backup", get_backup)

    await asyncio.gather(*(manager._ensure_capacity(2) for _ in range(10)))

    assert calls == 2
    assert len(manager._proxy_slots) == 2
    assert len({manager._proxy_key(slot.proxy) for slot in manager._proxy_slots}) == 2


@pytest.mark.anyio
async def test_second_proxy_expires_without_low_load_refill(manager, monkeypatch):
    first = _proxy("10.0.0.1")
    second = _proxy("10.0.0.2", expires_in=-1)
    manager._proxy_slots = [ProxySlot(first), ProxySlot(second)]

    async def unexpected_primary(excluded=None):
        pytest.fail("低并发清理第二 IP 后不应立即补回")

    async def get_backup(excluded=None):
        return None

    monkeypatch.setattr(manager, "_get_from_primary", unexpected_primary)
    monkeypatch.setattr(manager, "_get_from_backup", get_backup)

    proxy = await manager.acquire_proxy()

    assert proxy is first
    assert len(manager._proxy_slots) == 1
    manager.release_proxy(proxy)


def test_connect_429_does_not_pollute_quality_stats(manager):
    proxy = _proxy("10.0.0.1")
    manager._proxy_slots = [ProxySlot(proxy)]

    manager.report_connect_throttle(proxy)

    assert manager.connect_429_count == 1
    assert manager.proxied_failure_count == 0
    assert manager._provider_stats == {}
    assert manager._failure_timestamps == {}
    assert manager._penalty_box == {}
    assert manager._proxy_slots[0].throttled_until > time.time()


def test_punishment_removes_only_matching_idle_slot(manager):
    first = _proxy("10.0.0.1")
    second = _proxy("10.0.0.2")
    manager._proxy_slots = [ProxySlot(first), ProxySlot(second)]

    manager.punish_proxy(manager._proxy_key(first), duration=180)

    assert [slot.proxy for slot in manager._proxy_slots] == [second]
    assert manager._proxy_key(first) in manager._penalty_box


@pytest.mark.anyio
async def test_connect_429_switches_proxy_and_releases_on_response_close(manager, monkeypatch):
    import gsuid_core.plugins.ProxyManager.ProxyManager.pm_patcher as patcher

    first = _proxy("10.0.0.1")
    second = _proxy("10.0.0.2")
    manager._proxy_domains = {"api.kurobbs.com"}
    manager._proxy_slots = [ProxySlot(first), ProxySlot(second)]
    assigned = iter([first, second])
    count_flags = []

    async def get_proxy(url, excluded=None, force_second=False, count_request=True):
        count_flags.append(count_request)
        proxy = next(assigned)
        manager._find_slot(proxy).in_flight += 1
        if count_request:
            manager.increment_proxied_requests("api.kurobbs.com")
        return proxy

    class Proxy429(Exception):
        def __init__(self):
            self.status = 429

    class Content:
        def __init__(self):
            self.callback = None

        def on_eof(self, callback):
            self.callback = callback

    class Response:
        status_code = 200
        closed = False

        def __init__(self):
            self.content = Content()

        def release(self):
            self.closed = True
            if self.content.callback:
                self.content.callback()

        def close(self):
            self.release()

    response = Response()
    calls = 0

    async def request(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Proxy429()
        return response

    monkeypatch.setattr(patcher, "proxy_manager", manager)
    monkeypatch.setattr(patcher, "get_proxy_for_url", get_proxy)
    monkeypatch.setattr(patcher, "original_aiohttp_request", request)
    monkeypatch.setattr(patcher.aiohttp, "ClientHttpProxyError", Proxy429)
    monkeypatch.setattr(
        patcher.config,
        "get_config",
        lambda key: SimpleNamespace(
            data=(
                ["403", "429", "502"]
                if key == "FailureHttpCodes"
                else _DEFAULT_CONFIG[key]
            )
        ),
    )

    result = await patcher.patched_aiohttp_request(
        object(),
        "POST",
        "https://api.kurobbs.com/forum/like",
    )

    assert result is response
    assert count_flags == [True, False]
    assert manager.proxied_requests_count == 1
    assert manager.connect_429_count == 1
    assert manager.connect_429_switch_count == 1
    assert manager.connect_429_retry_success_count == 1
    assert manager._penalty_box == {}
    assert manager._find_slot(first).in_flight == 0
    assert manager._find_slot(second).in_flight == 1

    response.close()
    assert manager._find_slot(second).in_flight == 0


@pytest.mark.anyio
async def test_target_429_is_not_proxy_quality_failure(manager, monkeypatch):
    import gsuid_core.plugins.ProxyManager.ProxyManager.pm_patcher as patcher

    monkeypatch.setattr(patcher, "proxy_manager", manager)
    monkeypatch.setattr(
        patcher.config,
        "get_config",
        lambda key: SimpleNamespace(data=["403", "429", "502"]),
    )
    proxy = _proxy("10.0.0.1")
    response = SimpleNamespace(status_code=429)

    await patcher._handle_response(proxy, response)

    assert manager.target_429_count == 1
    assert manager.proxied_failure_count == 1
    assert manager._provider_stats == {}
    assert manager._failure_timestamps == {}
    assert manager._penalty_box == {}


@pytest.mark.anyio
async def test_status_redacts_proxy_password(manager):
    proxy = Proxy(
        url="http://10.0.0.1:8080",
        is_primary=True,
        source="主代理(api)",
        expires_at=time.time() + 60,
        username="alice",
        password="secret-password",
    )
    manager._proxy_slots = [ProxySlot(proxy)]

    status = await manager.get_status_text()

    assert "secret-password" not in status
    assert "alice" not in status
    assert "***:***@10.0.0.1:8080" in status
