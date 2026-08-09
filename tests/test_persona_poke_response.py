import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from msgspec import json as msgjson

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event, MessageSend
from gsuid_core.ai_core import poke
from gsuid_core.message_history import get_history_manager


def _event(*, group_id: str = "g1", user_id: str = "u1", target_id: str = "bot") -> Event:
    return Event(
        bot_id="onebot",
        real_bot_id="onebot",
        bot_self_id="bot",
        user_id=user_id,
        group_id=group_id,
        user_type="group",
        WS_BOT_ID="yunzai",
        sender={"nickname": "Tester"},
        meta_event_type="poke",
        meta_event_data={
            "user_id": user_id,
            "target_id": target_id,
            "group_id": group_id,
        },
    )


def _run(coro):
    return asyncio.run(coro)


def _reset_state() -> None:
    poke._last_response_at.clear()
    poke._active_ai_requests = 0


def test_poke_rejects_other_targets(monkeypatch) -> None:
    _reset_state()
    resolver = AsyncMock()
    monkeypatch.setattr(poke, "_generate_reaction", resolver)
    _run(poke.handle_persona_poke(SimpleNamespace(), _event(target_id="someone-else")))
    resolver.assert_not_awaited()


def test_poke_records_context_and_respects_session_cooldown(monkeypatch) -> None:
    _reset_state()
    ev = _event()
    history = get_history_manager()
    history.clear_history(ev)

    config = SimpleNamespace(get_config=lambda key: SimpleNamespace(data=key == "poke_response_enabled"))
    manager = SimpleNamespace(
        get_persona_for_session=lambda _session_id: "Sayu",
        get_config=lambda _name: config,
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.config.persona_config_manager",
        manager,
    )
    monkeypatch.setattr(
        poke,
        "_generate_reaction",
        AsyncMock(return_value=poke.PokeReaction(text="别戳啦", mood="害羞", poke_back=True)),
    )
    monkeypatch.setattr(poke, "_send_persona_meme", AsyncMock(return_value=True))

    bot = SimpleNamespace(send=AsyncMock(), poke=AsyncMock())
    _run(poke.handle_persona_poke(bot, ev))
    _run(poke.handle_persona_poke(bot, ev))

    bot.send.assert_awaited_once()
    assert bot.send.await_args.kwargs["observe_memory"] is False
    bot.poke.assert_awaited_once()
    records = history.get_history(ev)
    assert [record.content for record in records] == [
        "<互动：戳了你一下>",
        "<互动动作：尝试回戳当前用户>",
    ]
    assert records[0].metadata["interaction"] == "poke"
    assert records[1].metadata["status"] == "attempted"


def test_full_ai_capacity_does_not_queue_or_send_fallback(monkeypatch) -> None:
    _reset_state()
    poke._active_ai_requests = poke.POKE_AI_MAX_CONCURRENCY
    ev = _event(group_id="busy")
    history = get_history_manager()
    history.clear_history(ev)

    config = SimpleNamespace(get_config=lambda _key: SimpleNamespace(data=True))
    manager = SimpleNamespace(
        get_persona_for_session=lambda _session_id: "Sayu",
        get_config=lambda _name: config,
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.config.persona_config_manager",
        manager,
    )
    generate = AsyncMock()
    fallback = AsyncMock()
    monkeypatch.setattr(poke, "_generate_reaction", generate)
    monkeypatch.setattr(poke, "_send_persona_meme", fallback)

    _run(poke.handle_persona_poke(SimpleNamespace(), ev))

    generate.assert_not_awaited()
    fallback.assert_not_awaited()
    assert [record.content for record in history.get_history(ev)] == ["<互动：戳了你一下>"]


def test_failed_ai_releases_slot_and_uses_persona_fallback(monkeypatch) -> None:
    _reset_state()
    ev = _event(group_id="fallback")
    config = SimpleNamespace(get_config=lambda _key: SimpleNamespace(data=True))
    manager = SimpleNamespace(
        get_persona_for_session=lambda _session_id: "Sayu",
        get_config=lambda _name: config,
    )
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.config.persona_config_manager",
        manager,
    )
    monkeypatch.setattr(poke, "_generate_reaction", AsyncMock(return_value=None))
    fallback = AsyncMock(return_value=True)
    monkeypatch.setattr(poke, "_send_persona_meme", fallback)

    _run(poke.handle_persona_poke(SimpleNamespace(), ev))

    fallback.assert_awaited_once_with(SimpleNamespace(), ev, "Sayu")
    assert poke._active_ai_requests == 0


def test_bot_poke_encodes_single_control_packet() -> None:
    async def run() -> None:
        ev = _event()
        raw_bot = _Bot("yunzai")
        packets: list[bytes] = []

        async def capture(coro):
            queued = await raw_bot._send_queue.get() if False else None
            del queued
            original = raw_bot.bot
            raw_bot.bot = SimpleNamespace(send_bytes=AsyncMock(side_effect=lambda body: packets.append(body)))
            try:
                await coro
            finally:
                raw_bot.bot = original

        raw_bot._enqueue_send = capture  # type: ignore[method-assign]
        await Bot(raw_bot, ev).poke()

        assert len(packets) == 1
        packet = msgjson.decode(packets[0], type=MessageSend)
        assert packet.target_type == "group"
        assert packet.target_id == "g1"
        assert packet.content and len(packet.content) == 1
        assert packet.content[0].type == "excute_poke_user"
        assert packet.content[0].data == {"user_id": "u1", "group_id": "g1"}

    _run(run())


def test_bot_poke_user_encodes_explicit_group_target() -> None:
    async def run() -> None:
        ev = _event(user_id="speaker")
        raw_bot = _Bot("yunzai")
        packets: list[bytes] = []

        async def capture(coro):
            original = raw_bot.bot
            raw_bot.bot = SimpleNamespace(send_bytes=AsyncMock(side_effect=lambda body: packets.append(body)))
            try:
                await coro
            finally:
                raw_bot.bot = original

        raw_bot._enqueue_send = capture  # type: ignore[method-assign]
        assert await Bot(raw_bot, ev).poke_user("chosen-user") is True

        packet = msgjson.decode(packets[0], type=MessageSend)
        assert packet.target_type == "group"
        assert packet.target_id == "g1"
        assert packet.content[0].data == {"user_id": "chosen-user", "group_id": "g1"}

    _run(run())
