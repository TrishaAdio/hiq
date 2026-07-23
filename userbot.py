"""
Telethon userbot: one command does everything.

Send `.doall` from your own account (in any chat) and it will, for every
group where your account is an admin:
  1. Turn off the "only admins can send messages" restriction
  2. Scan the group's history and ban every non-admin who posted media

Telegram only allows these actions where you're an admin with the matching
rights, so it silently skips groups where you aren't.

Setup:
  1. cp .env.example .env  and fill in API_ID / API_HASH
     (get them from https://my.telegram.org -> API development tools)
  2. pip install -r requirements.txt
  3. python userbot.py   (first run asks for your phone + login code)

Note: automating a personal account ("userbot"/"self-bot") is against
Telegram's Terms of Service. Only use it on groups you administer.
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
from telethon.tl.types import ChannelParticipantsAdmins, ChatBannedRights, User

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

if not API_ID or not API_HASH:
    raise SystemExit(
        "API_ID / API_HASH missing. Copy .env.example to .env and fill them in "
        "(get them from https://my.telegram.org)."
    )

client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# Rights object that fully bans (kicks + blocks) a user.
BAN_RIGHTS = ChatBannedRights(until_date=None, view_messages=True)


async def i_am_admin(chat) -> bool:
    try:
        perms = await client.get_permissions(chat, "me")
        return bool(perms and perms.is_admin)
    except Exception:  # noqa: BLE001
        return False


async def get_admin_ids(chat) -> set[int]:
    admins: set[int] = set()
    try:
        async for user in client.iter_participants(
            chat, filter=ChannelParticipantsAdmins
        ):
            admins.add(user.id)
    except (ChatAdminRequiredError, ValueError):
        pass
    return admins


async def unlock_group(chat) -> None:
    """Remove the 'only admins can send messages' restriction."""
    rights = ChatBannedRights(until_date=None, send_messages=False)
    await client(EditChatDefaultBannedRightsRequest(peer=chat, banned_rights=rights))


async def ban_user(chat, user_id: int) -> bool:
    while True:
        try:
            await client(EditBannedRequest(chat, user_id, BAN_RIGHTS))
            return True
        except FloodWaitError as e:
            log.warning("Flood wait %ss banning %s -- sleeping", e.seconds, user_id)
            await asyncio.sleep(e.seconds + 1)
        except (UserAdminInvalidError, UserNotParticipantError, ChatAdminRequiredError):
            return False
        except Exception as e:  # noqa: BLE001
            log.warning("Could not ban %s: %s", user_id, e)
            return False


async def process_group(chat, me_id: int) -> tuple[int, bool]:
    """Unlock + scan-and-ban one group. Returns (banned_count, unlocked)."""
    unlocked = False
    try:
        await unlock_group(chat)
        unlocked = True
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
    except Exception as e:  # noqa: BLE001
        log.warning("unlock failed: %s", e)

    admins = await get_admin_ids(chat)
    admins.add(me_id)

    offenders: set[int] = set()
    async for msg in client.iter_messages(chat):
        if not msg.media:
            continue
        sender_id = msg.sender_id
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
    return banned, unlocked


@client.on(events.NewMessage(outgoing=True, pattern=rf"^{PREFIX}doall$"))
async def _doall(event):
    await event.edit("Working through every group you administer…")
    me = await client.get_me()

    groups = 0
    unlocked = 0
    total_banned = 0
    async for dialog in client.iter_dialogs():
        if not dialog.is_group:
            continue
        entity = dialog.entity
        if not await i_am_admin(entity):
            continue
        groups += 1
        try:
            banned, was_unlocked = await process_group(entity, me.id)
            total_banned += banned
            unlocked += int(was_unlocked)
            log.info("%s: unlocked=%s banned=%s", dialog.name, was_unlocked, banned)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except Exception as e:  # noqa: BLE001
            log.warning("skip %s: %s", dialog.name, e)

    await event.edit(
        f"Done. {groups} group(s) processed — "
        f"unlocked {unlocked}, banned {total_banned} non-admin(s)."
    )


async def main():
    await client.start()
    me = await client.get_me()
    log.info("Logged in as %s (id %s). Send %sdoall to run.", me.first_name, me.id, PREFIX)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
