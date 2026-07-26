"""Persona-aware poke response entrypoint."""

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event

sv_persona_poke = SV("人格戳一戳回应", pm=6, priority=5, enabled=True, area="ALL")


@sv_persona_poke.on_meta("poke", block=True)
async def handle_poke(bot: Bot, ev: Event) -> None:
    from gsuid_core.ai_core.poke import handle_persona_poke

    await handle_persona_poke(bot, ev)
