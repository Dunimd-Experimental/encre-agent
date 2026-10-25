# ea-connector-irc

System-default IRC connector plugin for Encre Agent.

Bridges Encre Agent into IRC networks over RFC 1459-style sockets: the
adapter registers a nick, joins the configured channel, answers PING with
PONG, and routes PRIVMSG traffic to the agent. Outbound text is stripped
of markdown, split by byte count to respect the 510-byte line limit, and
paced to stay clear of server flood limits. Ships as a plugin so the
connector lifecycle (registration, start/stop, teardown) follows the
plugin system instead of being compiled into the core gateway.

The adapter is dependency-free — the protocol is implemented directly on
Python's standard-library asyncio sockets.

## Configuration

Required environment variables (or per-adapter settings):

- `IRC_SERVER` — IRC server hostname (e.g. `irc.libera.chat`)
- `IRC_CHANNEL` — channel to join (e.g. `#encre`)

Optional extras: `IRC_PORT` (default 6697), `IRC_NICKNAME` (default
`encre-bot`), `IRC_USE_TLS`, `IRC_SERVER_PASSWORD`,
`IRC_NICKSERV_PASSWORD`, `IRC_ALLOWED_USERS` / `IRC_ALLOW_ALL_USERS`,
`IRC_HOME_CHANNEL`, `IRC_HOME_CHANNEL_NAME`, and the `max_message_length`
key under `platforms.irc` in `config.yaml`.

An empty allowlist allows every nick: IRC nicks are self-declared rather
than authenticated, so the allowlist is the only local defence against a
spoofed sender. Run `encre gateway setup` for the interactive wizard.
