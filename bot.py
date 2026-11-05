import os
import sqlite3
import time
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(__file__).with_name("members.db")
DAY = 86400

# Joins/leaves need the privileged "Server Members Intent" (enable it in the
# Discord Developer Portal -> Bot -> Privileged Gateway Intents).
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ---------------------------------------------------------------- storage ---
# One row per member holding running counters, updated as events happen, so
# lookups never need to re-read chat history.
db = sqlite3.connect(DB_PATH)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS members (
        guild_id  INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        joins     INTEGER NOT NULL DEFAULT 0,
        leaves    INTEGER NOT NULL DEFAULT 0,
        last_join INTEGER,              -- unix time; "days since join" is derived from it
        PRIMARY KEY (guild_id, user_id)
    );
    -- joins seen live, so a later backfill doesn't count the same join twice
    CREATE TABLE IF NOT EXISTS live_joins (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, ts INTEGER NOT NULL
    );
    -- last message scanned per channel, so /backfill only reads newer messages
    CREATE TABLE IF NOT EXISTS backfill_cursor (
        channel_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS seen_messages (message_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS settings (
        guild_id   INTEGER PRIMARY KEY,
        channel_id INTEGER
    );
    """
)


def _migrate_events():
    """Fold the old per-event table (earlier version of this bot) into counters."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='events'").fetchone():
        return
    db.executescript(
        """
        INSERT INTO members (guild_id, user_id, joins, leaves, last_join)
            SELECT guild_id, user_id, SUM(kind='join'), SUM(kind='leave'),
                   MAX(CASE WHEN kind='join' THEN ts END)
            FROM events GROUP BY guild_id, user_id
            ON CONFLICT(guild_id, user_id) DO NOTHING;
        INSERT OR IGNORE INTO seen_messages SELECT message_id FROM events WHERE message_id IS NOT NULL;
        INSERT INTO live_joins SELECT guild_id, user_id, ts FROM events
            WHERE kind='join' AND message_id IS NULL;
        ALTER TABLE events RENAME TO events_migrated;
        """
    )


_migrate_events()
db.commit()


def bump(guild_id, user_id, joins=0, leaves=0, last_join=None):
    """Increment a member's counters and keep the latest join time."""
    db.execute(
        "INSERT INTO members (guild_id, user_id, joins, leaves, last_join) VALUES (?,?,?,?,?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
        "joins = joins + excluded.joins, leaves = leaves + excluded.leaves, "
        "last_join = MAX(COALESCE(last_join, 0), COALESCE(excluded.last_join, 0))",
        (guild_id, user_id, joins, leaves, last_join),
    )


def get_member_row(guild_id, user_id):
    return db.execute(
        "SELECT joins, leaves, last_join FROM members WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    ).fetchone()


def last_join(guild_id, user_id):
    row = get_member_row(guild_id, user_id)
    return (row[2] or None) if row else None


def stats(guild_id, user_id, present: bool):
    """Return (joins, leaves). Backfilled history only has joins, so leaves are
    inferred: every join except the current one (if still here) ended in a leave."""
    joins, leaves, _ = get_member_row(guild_id, user_id) or (0, 0, None)
    leaves = max(leaves, joins - 1 if present else joins)
    return joins, leaves


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, DAY)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} day{'s' if days != 1 else ''}, {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def announce_channel(guild: discord.Guild):
    row = db.execute("SELECT channel_id FROM settings WHERE guild_id=?", (guild.id,)).fetchone()
    channel = guild.get_channel(row[0]) if row and row[0] else None
    return channel or guild.system_channel


# ----------------------------------------------------------------- events ---
@client.event
async def on_member_join(member: discord.Member):
    now = int(time.time())
    db.execute("INSERT INTO live_joins VALUES (?,?,?)", (member.guild.id, member.id, now))
    bump(member.guild.id, member.id, joins=1, last_join=now)
    db.commit()


@client.event
async def on_member_remove(member: discord.Member):
    now = int(time.time())
    guild = member.guild

    # If we never saw this person join, fall back to Discord's own join date.
    if get_member_row(guild.id, member.id) is None and member.joined_at:
        bump(guild.id, member.id, joins=1, last_join=int(member.joined_at.timestamp()))

    joined = last_join(guild.id, member.id)
    bump(guild.id, member.id, leaves=1)
    db.commit()
    _, leaves = stats(guild.id, member.id, present=False)

    lasted = f"They lasted **{fmt_duration(now - joined)}** since their last join." if joined else \
        "I don't know when they joined."
    channel = announce_channel(guild)
    if channel:
        await channel.send(
            f"👋 **{member}** (<@{member.id}>) has left the server for the "
            f"**{ordinal(leaves)}** time. {lasted}",
            allowed_mentions=discord.AllowedMentions.none(),
        )


# --------------------------------------------------------------- commands ---
@tree.command(name="leaves", description="Show how many times someone has joined/left the server")
@app_commands.describe(
    member="Who to look up (defaults to you)",
    user_id="Or a Discord user ID (works for people who already left)",
)
@app_commands.guild_only()
async def leaves_cmd(
    interaction: discord.Interaction,
    member: discord.Member | None = None,
    user_id: str | None = None,
):
    if user_id is not None:
        try:
            uid = int(user_id.strip())
        except ValueError:
            await interaction.response.send_message("That isn't a valid user ID.", ephemeral=True)
            return
        target = interaction.guild.get_member(uid)
    else:
        target = member or interaction.user
        uid = target.id

    present = target is not None
    joins, leaves = stats(interaction.guild_id, uid, present=present)
    if joins == 0 and leaves == 0 and not present:
        await interaction.response.send_message(
            f"I have no records for user ID `{uid}`.", ephemeral=True
        )
        return

    joined = last_join(interaction.guild_id, uid)
    if joined is None and present and target.joined_at:
        joined = int(target.joined_at.timestamp())
        joins = max(joins, 1)

    if present:
        name = target.display_name
        streak = f"Time since most recent join: **{fmt_duration(time.time() - joined)}**." if joined \
            else "Time since most recent join: unknown."
    else:
        user = client.get_user(uid)
        name = str(user) if user else f"User {uid}"
        streak = "They are **not currently in the server**."

    await interaction.response.send_message(
        f"**{name}** has joined **{joins}** time{'s' if joins != 1 else ''} "
        f"and left **{leaves}** time{'s' if leaves != 1 else ''}.\n{streak}",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="setchannel", description="Choose where leave announcements are posted")
@app_commands.describe(channel="Channel for leave messages")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    db.execute(
        "INSERT INTO settings (guild_id, channel_id) VALUES (?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
        (interaction.guild_id, channel.id),
    )
    db.commit()
    await interaction.response.send_message(f"Leave messages will be posted in {channel.mention}.")


@tree.command(name="backfill", description="Count past joins from the server's welcome messages")
@app_commands.describe(channel="Channel containing the join messages (defaults to the system channel)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def backfill(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    channel = channel or interaction.guild.system_channel
    if channel is None:
        await interaction.response.send_message("No channel given and no system channel is set.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)

    async def reply(text: str):
        # The interaction token expires after 15 minutes; long scans outlive it.
        try:
            await interaction.followup.send(text)
        except discord.HTTPException:
            target = interaction.channel or channel
            await target.send(f"<@{interaction.user.id}> {text}")

    # Resume after the last message scanned in this channel instead of rereading it all.
    row = db.execute("SELECT message_id FROM backfill_cursor WHERE channel_id=?", (channel.id,)).fetchone()
    after = discord.Object(id=row[0]) if row else None

    scanned = added = seen = 0
    last_id = row[0] if row else None
    forbidden = False
    try:
        async for msg in channel.history(limit=None, after=after, oldest_first=True):
            last_id = msg.id
            seen += 1
            if seen % 1000 == 0:  # checkpoint so an interrupted scan can resume
                db.execute("INSERT OR REPLACE INTO backfill_cursor VALUES (?,?)", (channel.id, last_id))
                db.commit()
            if msg.type is not discord.MessageType.new_member:
                continue
            scanned += 1
            ts = int(msg.created_at.timestamp())
            if db.execute("SELECT 1 FROM seen_messages WHERE message_id=?", (msg.id,)).fetchone():
                continue
            db.execute("INSERT INTO seen_messages VALUES (?)", (msg.id,))
            # Skip joins the bot already counted live (same user, within 2 minutes).
            if db.execute(
                "SELECT 1 FROM live_joins WHERE guild_id=? AND user_id=? AND ABS(ts-?) < 120",
                (interaction.guild_id, msg.author.id, ts),
            ).fetchone():
                continue
            bump(interaction.guild_id, msg.author.id, joins=1, last_join=ts)
            added += 1
    except discord.Forbidden:
        forbidden = True
    finally:
        if last_id is not None:
            db.execute("INSERT OR REPLACE INTO backfill_cursor VALUES (?,?)", (channel.id, last_id))
        db.commit()

    if forbidden:
        await reply(f"I can't read the history of {channel.mention}.")
        return
    await reply(
        f"Scanned {channel.mention}: found **{scanned}** new join messages, counted **{added}**.\n"
        "Next `/backfill` will only read messages newer than this one.\n"
        "Past leaves are inferred (joins − 1 for current members), since old leave times aren't recorded."
    )


@tree.command(name="ping", description="Check if the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(client.latency * 1000)}ms")


@tree.command(name="hello", description="Say hello")
@app_commands.describe(name="Who to greet")
async def hello(interaction: discord.Interaction, name: str):
    await interaction.response.send_message(f"Hello, {name}!")


@client.event
async def on_ready():
    await tree.sync()  # registers the slash commands with Discord
    print(f"Logged in as {client.user}")


client.run(os.environ["DISCORD_TOKEN"])
