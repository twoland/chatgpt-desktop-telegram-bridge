# Agent and group administration

Use the local owner account. Stop the bridge listener before changing routes and restart it afterward. Do not delete state to bypass a conflicting binding: it preserves delivery receipts and prevents accidental cross-chat disclosure.

## Add an existing conversation

```sh
./desktop-source/.venv/bin/python manage_chat_routes.py add editor EXISTING_LOCAL_THREAD_ID --title "Existing task title"
./desktop-source/.venv/bin/python manage_chat_routes.py list
```

Use the actual existing task ID and exact sidebar title. This registers a conversation; it does not spawn an agent or grant it new tool/account permissions. Configure its approved outgoing folder during setup, as described in README.

## Bind one group

Add your bot to the group. As the paired owner, send `/chatid@YourBot` there to retrieve its numeric chat ID. Use that ID in place of the synthetic `-10001` below.

```sh
./desktop-source/.venv/bin/python manage_chat_routes.py bind -10001 editor --all-members
```

`--all-members` permits all human members to give the dedicated agent project tasks. Alternatively use `--allow USER_ID OTHER_USER_ID` to restrict senders. Other agents remain inaccessible from that group. Each agent can belong to only one configured group, and its group assignment prevents ordinary owner-DM access to that group-only task.

Default routing requires `/ask@YourBot message`, a mention or a reply to the bot. Replies to unrelated human messages do not route. Bot/anonymous senders are rejected.

## Optional visibility

Each switch is independent and off by default:

```sh
./desktop-source/.venv/bin/python manage_chat_routes.py reply-chains -10001 on
./desktop-source/.venv/bin/python manage_chat_routes.py all-messages -10001 on
```

`reply-chains` follows replies to human messages already routed in this group. `all-messages` forwards every authorized human message/attachment, including ordinary conversations. Obtain the owner's approval of that wider scope and inform group members before enabling it. Replace `on` with `off` to disable. This configuration cannot make Telegram deliver messages it withholds: review the bot's Telegram privacy/admin settings separately, with owner approval. Commands explicitly addressed to other bots remain ignored.

For outgoing formatting:

```sh
./desktop-source/.venv/bin/python manage_chat_routes.py reply-mode no --chat-id -10001
```

Use `no`, `first` or `all`. Omit `--chat-id` to set the bridge-wide fallback. This changes presentation only.

## Private DM default

During pairing, `default_private_agent` in the ignored setup target selects a registered private agent for ordinary DMs. Existing installations may set the same key in ignored `private/chat-routing.json` while the listener is stopped. Validate with `manage_chat_routes.py list` and restart. Never select a group-only agent as the private default.

## Remove or reassign

```sh
./desktop-source/.venv/bin/python manage_chat_routes.py unbind -10001
./desktop-source/.venv/bin/python manage_chat_routes.py remove editor
```

The tool refuses conflicting saved state and removal of a still-bound group. Prepare a deliberate migration with backups if moving an existing task between audiences; do not erase historical receipts or replay pending messages into a new audience.

Telegram may upgrade a group to a supergroup, changing its numeric ID. Use `migrate-group OLD_ID NEW_ID` with the listener stopped. It checks Telegram membership/migration evidence and retains backups; old outbox requests are held rather than replayed. Inspect the private receipt before retrying an uncertain migration.

## Authority and file sharing

Group task access is project direction, not spending, private-data, publication or security-setting authority. Follow the bundled collaborative-work playbook and verify the affected owner's approval at the consequential action. Approve each agent's exact outgoing folder and destination group before enabling file uploads; every member of that group can download them. The bridge's file-root policy does not mediate the agent's other tools.
