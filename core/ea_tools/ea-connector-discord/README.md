# ea-connector-discord

System-default Discord connector plugin for Encre Agent.

Bridges Discord (via the third-party `discord.py` library) into the Encre
Agent gateway as a first-class platform adapter. Ships as a plugin so the
connector lifecycle (registration, start/stop, teardown) follows the plugin
system instead of being compiled into the core gateway.

## Features

- Message, forum-channel, and thread support with reply and history context
- Native slash commands with dynamic `/skill` autocomplete and server-side
  authorization gating
- Interactive component views (exec approval, slash confirm, clarify choice,
  update prompt, model picker)
- Voice channels end-to-end: UDP voice connection, per-SSRC opus decode, and
  continuous ambient/speech mixing (needs the optional `numpy` dependency and
  a system `ffmpeg` binary)
- Durable missed-message recovery backed by a per-profile SQLite ledger

## Configuration

Required environment variables (or per-adapter settings):

- `DISCORD_BOT_TOKEN` — Discord bot token

The adapter SDK dependencies (`discord.py`, `PyNaCl`, `aiohttp`) install via
the `all` extra: `pip install ea-connector-discord[all]`.

Reference: https://discord.com/developers/docs
