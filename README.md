# Soccer Pot Bot ⚽

A tiny Telegram bot that tracks who's paid for the game so nobody has to nag.
Honor system: players tap **I'm in**, then **I paid**. The bot keeps a live
scoreboard and can ping everyone who still owes.

## What it does

| Command | Who | What |
|---|---|---|
| `/newgame <amount> [label]` | organizers | Start collecting, e.g. `/newgame 10 Sunday 7s` |
| `/status` | anyone | Live scoreboard: paid vs. owe, totals |
| `/remind` | anyone | Privately DMs everyone who still owes |
| `/nudge` | anyone | Pings everyone who owes publicly in the group |
| `/markpaid` | anyone | Reply to a member's message to mark them paid |
| `/close` | organizers | Final tally, closes the game |

Players also get tap buttons on the game message: **I'm in**, **I paid**,
**Not paid** (undo), **Status**.

### Who counts as an organizer

`/newgame` and `/close` are restricted to **organizers**, which means:

- anyone who is an **admin/creator of the Telegram group**, plus
- any user id you list in the `ORGANIZER_IDS` environment variable
  (comma-separated), e.g. `export ORGANIZER_IDS="11111111,22222222"`.

In a 1-on-1 chat with the bot you're always treated as the organizer.

### How private reminders work

`/remind` no longer pings the group publicly. Instead it sends a **private DM**
to each person who still owes, attributed to whoever ran the command
("*Yerlan is collecting for Sunday 7s… you still owe $10*"). The full who-owes
list is sent only to the sender's DMs, so amounts never appear in the group.

⚠️ **Telegram limitation:** a bot can't message someone who has never opened a
chat with it. So private reminders only reach players who have pressed **Start**
on the bot at least once. The game message includes a *"Tap here & press Start"*
link for exactly this. Anyone who hasn't done so is reported back to the sender
("couldn't DM: Sam, Leo") so they can be nudged the old-fashioned way.

## 1. Create the bot (2 min)

1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`, pick a name and username. BotFather gives you a **token**
   like `123456:ABC-DEF...`. Keep it secret.
3. Send `/setprivacy` → select your bot → **Disable** is *optional*. Leave it
   ON (default) — the bot only needs to see commands and button taps, which
   work fine with privacy on.

## 2. Run it

```bash
pip install -r requirements.txt
export BOT_TOKEN="123456:ABC-DEF..."          # paste your token
export ORGANIZER_IDS="11111111"               # optional: extra organizers
python soccer_pot_bot.py
```

Then add the bot to your group chat (group → Add members → search its
username). Send `/newgame 10` and you're off.

Data is stored in a local `potbot.db` file next to the script — back it up if
you care about history. Override the path with `POTBOT_DB=/path/to/file.db`.

## 3. Keep it running 24/7

The script must stay running for the bot to respond. Options, cheapest first:

- **A spare machine / Raspberry Pi at home** — just leave the command running
  (use `tmux`, `screen`, or a `systemd` service so it survives reboots).
- **A free/cheap cloud host** — Railway, Render, Fly.io, or a $5/mo VPS
  (DigitalOcean, Hetzner). Set `BOT_TOKEN` as an environment variable there and
  run `python soccer_pot_bot.py` as the start command. On hosts with an
  ephemeral filesystem, attach a small persistent volume for `potbot.db`.

A minimal `systemd` unit (Linux):

```ini
# /etc/systemd/system/potbot.service
[Unit]
Description=Soccer Pot Bot
After=network.target

[Service]
WorkingDirectory=/home/you/potbot
Environment=BOT_TOKEN=123456:ABC-DEF...
ExecStart=/usr/bin/python3 /home/you/potbot/soccer_pot_bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now potbot
```

## Notes

- It's an honor-system tracker, not a payment processor — it doesn't move
  money. Pair it with whatever you already use (Interac e-Transfer, etc.).
- One open game per chat at a time; starting a new one archives the old.
- `/newgame` and `/close` are organizer-only (see above). Everything else is
  open to all members.
- To find your numeric user id for `ORGANIZER_IDS`, message `@userinfobot` on
  Telegram, or check the bot's logs after you tap a button.
