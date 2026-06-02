# Telegram setup for ANNA

> This guide assumes ANNA is installed via the Phase 2.5 `uv tool install`
> path: shim binaries on `~/.local/bin/`, state files under `~/anna/`,
> systemd unit at `~/.config/systemd/user/anna.service`. If you're on a
> pre-migration install where `~/anna/.venv/` still exists, run
> `scripts/migrate-to-uv-tool.sh` first — see the README's
> *Migrating from a pre-Phase-A install* section.

This is the full version of the Telegram walkthrough the `anna-setup` wizard
shows. The wizard prints only the essentials; this is the detailed reference.

ANNA talks to Telegram with long-polling, so there's no public URL or inbound
networking to configure. You need one **bot token** and your own **numeric
user ID**.

## 1. Create the bot with @BotFather

1. Open Telegram and start a chat with **@BotFather**.
2. Send `/newbot`.
3. Pick a **display name** (e.g. `ANNA`).
4. Pick a **username** that must end in `bot` (e.g. `seths_anna_bot`).
5. BotFather replies with an **HTTP API token** that looks like
   `123456789:AAExampleExampleExampleExampleExample`. Copy it.

Paste that token into the wizard. It is stored in `.env` at `chmod 600` and is
never logged; only its last four characters appear in audit events.

## 2. Find your numeric user ID

ANNA pins its allowed-users list to your numeric ID so strangers can't talk to
her. To find it:

- DM **@userinfobot** (or **@RawDataBot**) and it replies with your numeric ID, or
- After ANNA is running, DM your new bot once and read the ID from
  `anna-logs --follow`.

Enter that number when the wizard asks. This same ID is used as the
admin-alert chat, so operational alerts come straight to your DM.

## 3. Say hello

Once setup finishes and ANNA reports the Telegram channel **connected**, DM
your bot (`@your_bot_username`) and say hi.

## Optional: bot settings

In @BotFather you can later set a profile photo (`/setuserpic`), description
(`/setdescription`), and the about text (`/setabouttext`). None of these affect
functionality — they're cosmetic.
