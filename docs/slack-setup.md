# Slack setup for ANNA

> This guide assumes ANNA is installed via the Phase 2.5 `uv tool install`
> path: shim binaries on `~/.local/bin/`, state files under `~/anna/`,
> systemd unit at `~/.config/systemd/user/anna.service`. If you're on a
> pre-migration install where `~/anna/.venv/` still exists, run
> `scripts/migrate-to-uv-tool.sh` first — see the README's
> *Migrating from a pre-Phase-A install* section.

This is the full, screenshot-friendly version of the Slack walkthrough the
`anna-setup` wizard shows. The wizard only prints the essentials; everything
here is the detailed reference, including the gotchas that trip people up.

ANNA talks to Slack over **Socket Mode**, so you do **not** need a public URL
or any inbound networking. You need two tokens: a **bot token** (`xoxb-…`) and
an **app-level token** (`xapp-…`).

## 1. Create the app

1. Go to <https://api.slack.com/apps> and click **Create New App → From scratch**.
2. Name it (e.g. `ANNA`) and pick your workspace.

## 2. Bot scopes (OAuth & Permissions)

Under **OAuth & Permissions → Scopes → Bot Token Scopes**, add:

| Scope | Why |
|-------|-----|
| `chat:write` | send messages |
| `chat:write.public` | post in channels it hasn't joined |
| `channels:read` | resolve public channel info |
| `groups:read` | resolve private channel info |
| `app_mentions:read` | see `@anna` mentions |
| `im:history` | read DM history |
| `im:read` | list DMs |
| `im:write` | open/send DMs |

## 3. Socket Mode

Under **Socket Mode**, toggle **Enable Socket Mode** on. When prompted, create
an **app-level token** with the `connections:write` scope. Copy the
`xapp-…` token — that's the app token the wizard asks for.

## 4. Event Subscriptions

Under **Event Subscriptions**:

1. Toggle **Enable Events** on.
2. Expand **Subscribe to bot events** and click **Add Bot User Event**. Add
   **both** (you need both):
   - `message.im` — required for DMs
   - `app_mention` — required for `@anna` in channels
3. **Save Changes** at the bottom of the page.

## 5. App Home (the gotcha)

Under **App Home**:

- Set a **Display Name** and **Default Username** for the bot user.
- Under **Show Tabs**, enable the **Messages Tab**, **and** tick
  **“Allow users to send Slash commands and messages from the messages tab.”**

> ⚠️ If you skip that checkbox, Slack shows **“Sending messages to this app has
> been turned off”** when you try to DM the bot, and nothing works.

## 6. Install + copy tokens

1. **Install App** to your workspace and approve the scopes.
2. From **OAuth & Permissions**, copy the **Bot User OAuth Token** (`xoxb-…`).
3. Paste the `xoxb-…` (bot) and `xapp-…` (app) tokens into the wizard.

## Changing scopes later

If you add scopes or events after installing, you must **Reinstall** the app
from **OAuth & Permissions** for the changes to take effect.

## Admin-alerts channel

The wizard optionally asks for a **channel ID** (looks like `C0AFD2LM38R`, not
`#name`) where ANNA posts operational alerts (auth failures, transport
restarts, stalled workers). Get the ID from the channel’s **About** panel, or
right-click the channel → **Copy link** (the ID is the last path segment).
**Invite the bot to that channel** (`/invite @anna`) or it can’t post there.
Leave it blank to skip — ANNA still logs alerts, just not to Slack.
