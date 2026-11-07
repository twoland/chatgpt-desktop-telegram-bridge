# Security and reporting

This is an experimental community bridge, not a security sandbox. Report suspected vulnerabilities privately through the repository's GitHub security reporting feature when available. Never post tokens, private transcripts, screenshots, raw state or diagnostic bundles in public issues.

- Pair a dedicated bot with the one-time private-chat code. The numeric paired owner controls routes. Display names, forwarded approvals and group-admin status do not grant owner authority.
- Keep `private/`, setup-target.json, installation registries, environments, logs, attachments and backups outside Git and release archives. `.gitignore` is a convenience; tracked files and manually built archives can bypass it.
- Bind the desktop debugging connection to 127.0.0.1 only. Any local process with access to that connection may control the logged-in application. Do not expose it through tunnels or a LAN listener.
- Chat-to-agent isolation and approved attachment roots limit the bridge's own routing and uploads. They do not isolate browser sessions, terminal tools, payment methods or the model's other capabilities. Use separate scoped accounts/OS users where necessary.
- Group participants may direct authorized project work. Spending, subscriptions, unrelated private access/disclosure, wider sharing, publication, destructive changes and access/delegation changes require the affected owner's explicit grant. The bundled skills describe these boundaries but do not enforce them at the OS/service level.
- The desktop board is cooperative. Every desktop user must follow it. Never steal a stale lease, replace another board with FREE, or close another user's active app/capture to clear a queue.
- Protocol/debug logging, screenshots and state can include conversation content and local paths. Review/redact support evidence before sharing. Default token-related API errors omit URLs; do not enable HTTP debug logs with a live token.
- A failed or timed-out send may already have reached the app. Inspect receipts and the native queue before resending. Keep backups and investigate uncertain cross-chat state; do not clear it as a shortcut.

If a credential is actually exposed, preserve minimal redacted evidence, stop further sharing and ask its owner to revoke/rotate it. Rewriting Git history does not revoke a token or erase copies, caches, forks or downloads. A source scan is useful evidence, not proof of no exposure.
