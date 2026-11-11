# ChatGPT Desktop Telegram Bridge

An unofficial, experimental macOS bridge connecting a Telegram bot to existing local Codex conversations. The bridge is a background Python program, not an AI agent. Each configured group reaches one dedicated conversation; an optional private-chat default handles ordinary bot DMs.

This source-checkout alpha uses desktop UI internals and local `.codex/sessions` transcripts. App updates can break it. It requires a compatible installed desktop build, a logged-in local account, Python 3.11+, and permission to control the desktop. The current implementation was exercised on macOS with the Codex local-task UI in the ChatGPT desktop app; other app builds must be checked locally. Configure the actual `.app` path rather than assuming its name. Linux CI covers isolated logic, not native desktop compatibility. No Windows/Linux desktop support is claimed.

## Behavior

- Explicit owner pairing and agent registry; no bundled personal routes or default task IDs.
- One-agent-per-group routing, sender allowlists or all-human-member access, sender identity and quoted reply context.
- Mentions, replies to the bot and `/ask`; forwarding ordinary group messages is a separate opt-in, off by default.
- Saved incoming inputs, retry backoff, native app queueing and receipt reconciliation. Ambiguous accepted input is reconciled before another click.
- Typing indicators, queued/seen/done reactions, progress updates and final replies.
- Incoming attachments and outgoing files restricted to each agent's approved folders.
- Outgoing reply modes: No, First and All. All is the compatibility default.
- Bundled Telegram and collaborative-work skills. Optional board-governance integration is disabled unless privately configured; it is not needed for normal messaging.

## Install from source

Use a dedicated project directory. Install Python 3.11+ and `uv`, then from this repository:

```sh
cd desktop-source
uv sync --locked --extra dev
cd ..
cp setup-target.example.json setup-target.json
```

Edit the ignored `setup-target.json`: enter your bot username, exact existing local conversation ID/title and actual desktop app path. Leave `file_roots` empty until its sharing scope is approved. `default_private_agent` makes untagged DMs select that registered alias; omit it to require commands/replies. Task IDs are visible in local task links. Do not create a new task just to reconnect an existing one.

Create a dedicated bot with Telegram's BotFather. Pair it using hidden token entry and the one-time private-chat link:

```sh
./desktop-source/.venv/bin/python setup_telegram.py
./desktop-source/.venv/bin/python install_telegram_skill.py
```

Pairing stores the token, exact owner ID and routing under ignored `private/`, with private permissions. Keep that directory local. Do not commit your setup target, environment files, installed skill registry, logs, attachments or state.

### Desktop coordination

All agents/users sharing the desktop must follow one coordination board. If an installed `~/.local/bin/board` owns `~/desktop-control.md`, reuse its CLI and generated view; never replace the view or take another owner's lease. The bridge derives a distinct relay identity for each installation.

If no board system exists, and you have verified nobody is using the desktop, create the bundled legacy Markdown board explicitly:

```sh
./desktop-source/.venv/bin/python init_desktop_board.py
```

This refuses to overwrite an existing board or replace an installed board CLI. Every other desktop user must also follow its claims/reservations/releases. It is cooperative coordination, not an OS security boundary. A missing or ambiguous board stops desktop input.

### Complete each agent's setup

Have the connected agent read both installed skills. Record who owns the host/accounts, who may direct project work and what still requires owner approval. Group task access does not grant payment, private-account, publication or destructive-action authority.

Before declaring setup ready, choose a dedicated outgoing folder and obtain the owner's approval of its exact absolute path **and destination chat**, including visibility to every group member. Stop the listener before changing routes, then configure and inspect it:

```sh
./desktop-source/.venv/bin/python manage_chat_routes.py allow-files assistant /absolute/Project/Telegram/Outgoing
./desktop-source/.venv/bin/python manage_chat_routes.py list
```

Restart and send a genuine request from that chat. Return a harmless file in a final answer as an absolute local-file Markdown link; verify Telegram receives a downloadable attachment with the expected contents. Text delivery alone does not verify file setup. The progress helper sends text, not attachments.

### Start

Finish active work and quit the configured app normally before enabling its local connection for the first time. The launcher does not kill a running app. Run:

```sh
./desktop-source/.venv/bin/python open_desktop.py
```

It respects `private/config.toml`'s app path and debugging port, opens the normal profile, checks the endpoint and starts the paired listener in the background. On first use, select **Queue** in the desktop app's follow-up behavior setting. The bridge will wait rather than use Steer when the agent is busy. The app's existing permissions and approval prompts still apply.

The `.command` launchers are macOS conveniences. For a foreground listener, run `start_telegram_bridge.command`; stop it with `stop_telegram_bridge.command`. Do not run two listeners for one bot. An existing listener is detected using its process lock, not a stale PID file.

## Moving an existing installation

Stop the listener, then move the complete project folder, including ignored `private/` and any `installation.json` receipt. A moved editable Python environment can still point at the old source directory. Reinstall it from the new location:

```sh
cd /new/path/to/telegram-bridge/desktop-source
uv sync --locked --extra dev
cd ..
```

Update the old installation paths in `private/config.toml`: `telegram.bot_token_file`, `bridge.state_path`, and any explicit log paths. Keep the existing token, routes and delivery state. Run `./desktop-source/.venv/bin/python install_telegram_skill.py` again to repoint the installed helper registry, then start `open_desktop_for_telegram.command`. Keep `private/bridge-id.json` with the installation: it preserves the bridge identity used by existing agent requests. Older installations can recover the same identity from their `installation.json` receipt.

The launcher respects the configured app path and port. If the app is already running without that port, quit it normally after active work finishes, then run the launcher. The listener waits briefly for the task sidebar to render after the local endpoint appears.

## Routes and groups

See [Group Chat Guide](Group%20Chat%20Guide.md) for adding/removing agents, group membership policy, opt-in message visibility and migrations. Stop the listener before administration, then restart it. Configuration changes never authorize another account, folder or audience by themselves.

Use `/agents` to list only the routes available in that chat. In a configured group, use `/ask@YourBot message`, mention the bot or reply to its message. Ordinary human-to-human replies do not route by default. The owner can independently enable `reply-chains` or `all-messages` for a group.

`/replymode` reports the chat's mode. The paired owner may set `/replymode no`, `/replymode first` or `/replymode all` without restarting. No = plain messages; First = only the first outgoing response per incoming request is a reply; All = every response replies to the original. Mode does not change access or routing.

## Agent communication

[Telegram skill](skills/telegram/SKILL.md) requires agents to acknowledge substantial requests briefly, send meaningful progress/blockers/questions to the assigned Telegram chat, and return results there. Unrelated desktop work is not automatically forwarded. Telegram-origin final answers are relayed automatically; explicitly sending the same final again would duplicate it.

The installed skill helper sends a scoped progress update using the request's bridge/agent/chat/message metadata:

```sh
python3 /absolute/skills/telegram/scripts/send_update.py --bridge BRIDGE_ID --agent assistant --chat-id CHAT_ID --reply-to MESSAGE_ID --text-file /absolute/Project/Telegram/update.txt
```

`status: sent` with nonempty `sent_ids` confirms delivery. An unchanged retry is idempotent; inspect an uncertain receipt before trying again. Keep questions as ordinary text in Telegram. Interactive app-only question panels block this workflow and must not be used.

Reactions: 😴 saved/waiting; 👀 exact input confirmed in an agent turn; 👌 final response delivered. Seen is a transport receipt, not proof of understanding. The bridge owns typing indicators and reactions.

## Reliability and recovery

Connection liveness and message delivery are different. Desktop contention, an unsent draft, an interactive question, Queue/Steer settings, an incompatible app build or an interrupted local connection can delay input while the bot remains online. Preserve active work. Check the assigned conversation, native queue and saved receipt before resending an uncertain message. Never clear saved inputs or restart the app merely to force an acknowledgment.

The bridge does not promise exactly-once execution across arbitrary crashes or app changes. Unknown delivery is retained for reconciliation; legitimate duplicates can still require operator review. Native permissions and approvals must be answered through their supported interface.

## Security and limitations

See [SECURITY.md](SECURITY.md). The local debugging endpoint must stay on loopback; do not expose it to a LAN or the internet. A process with access to it can control the logged-in desktop app. The bridge also reads registered local transcripts, so its OS account needs only the access you intend to grant.

Chat routing and file-root checks are not isolation of the model's other tools. Agents running under one macOS account may still access shared accounts/files. Use separate OS users or scoped accounts/tools for stronger isolation. Skills describe approval behavior; they do not implement a payment/data firewall.

Treat logs, diagnostics, state, screenshots and attachments as sensitive. Do not post raw support bundles. Defaults suppress HTTP request logs and use WARNING protocol logging during pairing; enabling debug logging can expose message contents. Only one agent's approved final file links are eligible for its configured chat; do not broaden roots to work around a failed upload.

## Development

```sh
cd desktop-source
uv sync --locked --extra dev
uv run --no-sync pytest
cd ..
python3 tools/check_release.py
```

Normal tests use temporary fixtures/mocked APIs. They do not contact Telegram or control the real desktop. `tests/e2e/` retains an opt-in development harness; it is not evidence that a new app build or clean macOS account has passed a native end-to-end test. Run any live test only with a dedicated account/bot, approved folders and an exclusive desktop claim.

The repository includes source-level credential checks in CI. They are a guard, not a guarantee: review every release tree/artifact and keep runtime data outside Git.

## Attribution

Derived from [toolittlecakes/codex-telegram-bridge](https://github.com/toolittlecakes/codex-telegram-bridge), with desktop adaptation and local routing/reliability/skill work. Both MIT license copies are retained. This is a community project and is not affiliated with or supported by OpenAI or Telegram. Fresh publication history intentionally excludes private installation history; the retained upstream license and attribution still apply.
