"""
Telethon userbot for group administration.

What it does (only in groups where YOUR account is an admin with the
matching rights -- Telegram rejects the actions otherwise):

  .unlock         Allow non-admins to send messages in the current group
  .unlockall      Do the same across every group you administer
  .scan           Scan the current group's history and ban non-admins
                  who posted any media
  .scanall        Same scan across every group you administer
  .guard on|off   Toggle real-time banning of non-admins who post media
  .help           Show the command list

Setup:
  1. cp .env.example .env  and fill in API_ID / API_HASH
     (get them from https://my.telegram.org -> API development tools)
  2. pip install -r requirements.txt
  3. python userbot.py   (first run asks for your phone + login code)

Note: automating a personal account ("userbot"/"self-bot") is against
Telegram's Terms of Service. Only run this on groups you own or
administer, and use it responsibly.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import (
    ChatAdminRequiredError,
    FloodWaitError,
    UserAdminInvalidError,
    UserNotParticipantError,
)
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.functions.messages import EditChatDefaultBannedRightsRequest
from telethon.tl.types import (
    ChannelParticipantsAdmins,
    ChatBannedRights,
    PeerChannel,
    User,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("userbot")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_NAME = os.getenv("SESSION_NAME", "userbot")
PREFIX = os.getenv("PREFIX", ".")
GUARD_ON_START = os.getenv("GUARD_ON_START", "false").lower() == "true"

if not API_ID or not API_HASH:
    raise SystemExit(
        "API_ID / API_HASH missing. Copy .env.example to .env and fill them in "
        "(get them from https://my.telegram.org)."
    )

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# Rights object that fully bans (kicks + blocks) a user.
BAN_RIGHTS = ChatBannedRights(until_date=None, view_messages=True)

# Set of chat ids where the real-time media guard is active.
guarded_chats: set[int] = set()
guard_enabled = GUARD_ON_START

# Cache of admin id sets per chat so the real-time guard stays cheap.
_admin_cache: dict[int, set[int]] = {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def get_admin_ids(chat) -> set[int]:
    """Return the set of user ids that are admins/creator of a chat."""
    admins: set[int] = set()
    try:
        async for user in client.iter_participants(
            chat, filter=ChannelParticipantsAdmins
        ):
            admins.add(user.id)
    except (ChatAdminRequiredError, ValueError):
        # Fall back to scanning participants for admin flags.
        async for user in client.iter_participants(chat):
            p = getattr(user, "participant", None)
            if p and p.__class__.__name__ in (
                "ChannelParticipantAdmin",
                "ChannelParticipantCreator",
            ):
                admins.add(user.id)
    return admins


async def i_am_admin(chat) -> bool:
    """Check whether the logged-in account has admin rights in the chat."""
    try:
        perms = await client.get_permissions(chat, "me")
        return bool(perms and perms.is_admin)
    except Exception:  # noqa: BLE001 - any failure means "can't confirm admin"
        return False


async def unlock_group(chat) -> None:
    """Clear the 'only admins can send messages' restriction for a chat."""
    rights = ChatBannedRights(until_date=None, send_messages=False)
    await client(EditChatDefaultBannedRightsRequest(peer=chat, banned_rights=rights))


async def ban_user(chat, user_id: int) -> bool:
    """Ban a single user, tolerating flood waits. Returns True on success."""
    while True:
        try:
            await client(EditBannedRequest(chat, user_id, BAN_RIGHTS))
            return True
        except FloodWaitError as e:
            log.warning("Flood wait %ss while banning %s -- sleeping", e.seconds, user_id)
            await asyncio.sleep(e.seconds + 1)
        except (UserAdminInvalidError, UserNotParticipantError):
            return False
        except ChatAdminRequiredError:
            log.warning("No ban permission in chat; skipping user %s", user_id)
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("Could not ban %s: %s", user_id, e)
            return False


async def scan_and_ban(chat) -> tuple[int, int]:
    """
    Scan a chat's message history and ban every non-admin who posted media.
    Returns (banned_count, scanned_messages).
    """
    admins = await get_admin_ids(chat)
    me = await client.get_me()
    admins.add(me.id)

    offenders: set[int] = set()
    scanned = 0
    async for msg in client.iter_messages(chat):
        scanned += 1
        if not msg.media:
            continue
        sender_id = msg.sender_id
        # Skip service messages, anonymous/channel senders, and admins.
        if sender_id is None or sender_id < 0 or sender_id in admins:
            continue
        sender = await msg.get_sender()
        if not isinstance(sender, User) or sender.bot:
            continue
        offenders.add(sender_id)

    banned = 0
    for uid in offenders:
        if await ban_user(chat, uid):
            banned += 1
    return banned, scanned


def iter_target_groups():
    """Async generator over dialogs that are groups (not private chats/channels)."""
    async def _gen():
        async for dialog in client.iter_dialogs():
            if dialog.is_group:
                yield dialog.entity

    return _gen()


# --------------------------------------------------------------------------- #
# Command handlers (only respond to messages sent by the account itself)
# --------------------------------------------------------------------------- #
def cmd(command: str):
    pattern = rf"^{PREFIX}{command}(?:\s+(.*))?$"
    return events.NewMessage(outgoing=True, pattern=pattern)


@client.on(cmd("help"))
async def _help(event):
    await event.edit(
        "**Userbot commands**\n"
        f"`{PREFIX}unlock` — let non-admins send messages here\n"
        f"`{PREFIX}unlockall` — same, every group you admin\n"
        f"`{PREFIX}scan` — ban non-admins who posted media here\n"
        f"`{PREFIX}scanall` — same, every group you admin\n"
        f"`{PREFIX}guard on|off` — real-time media banning\n"
    )


@client.on(cmd("unlock"))
async def _unlock(event):
    if not event.is_group:
        return await event.edit("Run this inside a group.")
    if not await i_am_admin(event.chat_id):
        return await event.edit("I'm not an admin here — can't change settings.")
    try:
        await unlock_group(await event.get_chat())
        await event.edit("Non-admins can now send messages here.")
    except Exception as e:  # noqa: BLE001
        await event.edit(f"Failed: {e}")


@client.on(cmd("unlockall"))
async def _unlockall(event):
    await event.edit("Unlocking messaging in every group you administer…")
    done = 0
    async for entity in iter_target_groups():
        if not await i_am_admin(entity):
            continue
        try:
            await unlock_group(entity)
            done += 1
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:  # noqa: BLE001
            log.warning("unlockall skip %s: %s", getattr(entity, "id", "?"), e)
    await event.edit(f"Unlocked messaging in {done} group(s).")


@client.on(cmd("scan"))
async def _scan(event):
    if not event.is_group:
        return await event.edit("Run this inside a group.")
    if not await i_am_admin(event.chat_id):
        return await event.edit("I'm not an admin here — can't ban.")
    await event.edit("Scanning history for non-admin media posters…")
    banned, scanned = await scan_and_ban(await event.get_chat())
    await event.edit(f"Scanned {scanned} messages. Banned {banned} non-admin(s).")


@client.on(cmd("scanall"))
async def _scanall(event):
    await event.edit("Scanning every group you administer… this can take a while.")
    total_banned = 0
    groups = 0
    async for entity in iter_target_groups():
        if not await i_am_admin(entity):
            continue
        groups += 1
        try:
            banned, _ = await scan_and_ban(entity)
            total_banned += banned
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:  # noqa: BLE001
            log.warning("scanall skip %s: %s", getattr(entity, "id", "?"), e)
    await event.edit(
        f"Done. Scanned {groups} group(s), banned {total_banned} non-admin(s)."
    )


@client.on(cmd("guard"))
async def _guard(event):
    global guard_enabled
    arg = (event.pattern_match.group(1) or "").strip().lower()
    if arg == "on":
        guard_enabled = True
        await event.edit("Real-time media guard: ON.")
    elif arg == "off":
        guard_enabled = False
        await event.edit("Real-time media guard: OFF.")
    else:
        await event.edit(f"Usage: `{PREFIX}guard on` or `{PREFIX}guard off`.")


# --------------------------------------------------------------------------- #
# Real-time guard: ban non-admins who post media as it happens
# --------------------------------------------------------------------------- #
@client.on(events.NewMessage(incoming=True))
async def _media_guard(event):
    if not guard_enabled or not event.is_group or not event.media:
        return

    chat_id = event.chat_id
    sender_id = event.sender_id
    if sender_id is None or sender_id < 0:
        return

    # Refresh admin cache lazily per chat.
    admins = _admin_cache.get(chat_id)
    if admins is None:
        admins = await get_admin_ids(await event.get_chat())
        me = await client.get_me()
        admins.add(me.id)
        _admin_cache[chat_id] = admins

    if sender_id in admins:
        return
    if not await i_am_admin(chat_id):
        return

    if await ban_user(await event.get_chat(), sender_id):
        try:
            await event.delete()
        except Exception:  # noqa: BLE001
            pass
        log.info("Guard banned %s in chat %s for posting media", sender_id, chat_id)


async def main():
    await client.start()
    me = await client.get_me()
    log.info("Logged in as %s (id %s). Guard=%s. Prefix=%r",
             me.first_name, me.id, guard_enabled, PREFIX)
    log.info("Send %shelp from your own account in any chat to see commands.", PREFIX)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
