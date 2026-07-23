"""Telethon userbot that locks and cleans every administered group.

Send `.doall` from your own account. In every group where the account has
sufficient admin rights, it:
  1. Disables all messages from non-admins.
  2. Finds non-admins who have posted media.
  3. Deletes every text and media message those users sent.
  4. Bans those users.
"""

import asyncio
import logging
import os
import re

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

# Default restrictions for non-admins. Explicit content flags cover both older
# and newer Telegram API layers, including plain text and every media type.
LOCK_RIGHTS = ChatBannedRights(
    until_date=None,
    send_messages=True,
    send_media=True,
    send_stickers=True,
    send_gifs=True,
    send_games=True,
    send_inline=True,
    embed_links=True,
    send_polls=True,
    send_photos=True,
    send_videos=True,
    send_roundvideos=True,
    send_audios=True,
    send_voices=True,
    send_docs=True,
    send_plain=True,
)
BAN_RIGHTS = ChatBannedRights(until_date=None, view_messages=True)
DELETE_BATCH_SIZE = 100


async def i_am_admin(chat) -> bool:
    try:
        permissions = await client.get_permissions(chat, "me")
        return bool(permissions and permissions.is_admin)
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


async def lock_group(chat) -> bool:
    """Disable text and media posting for all non-admin members."""
    while True:
        try:
            await client(
                EditChatDefaultBannedRightsRequest(
                    peer=chat,
                    banned_rights=LOCK_RIGHTS,
                )
            )
            return True
        except FloodWaitError as error:
            log.warning("Flood wait %ss locking group", error.seconds)
            await asyncio.sleep(error.seconds + 1)
        except Exception as error:  # noqa: BLE001
            log.warning("Could not lock group: %s", error)
            return False


async def ban_user(chat, user_id: int) -> bool:
    while True:
        try:
            await client(EditBannedRequest(chat, user_id, BAN_RIGHTS))
            return True
        except FloodWaitError as error:
            log.warning(
                "Flood wait %ss banning %s -- sleeping",
                error.seconds,
                user_id,
            )
            await asyncio.sleep(error.seconds + 1)
        except (
            UserAdminInvalidError,
            UserNotParticipantError,
            ChatAdminRequiredError,
        ):
            return False
        except Exception as error:  # noqa: BLE001
            log.warning("Could not ban %s: %s", user_id, error)
            return False


async def find_media_offenders(chat, admin_ids: set[int]) -> set[int]:
    """Find non-admin human users who posted at least one media message."""
    offenders: set[int] = set()
    checked_users: set[int] = set()

    async for message in client.iter_messages(chat):
        sender_id = message.sender_id
        if (
            not message.media
            or sender_id is None
            or sender_id < 0
            or sender_id in admin_ids
            or sender_id in checked_users
        ):
            continue

        checked_users.add(sender_id)
        sender = await message.get_sender()
        if isinstance(sender, User) and not sender.bot:
            offenders.add(sender_id)

    return offenders


async def delete_message_batch(chat, message_ids: list[int]) -> int:
    """Delete one batch and return the number accepted by Telegram."""
    while True:
        try:
            await client.delete_messages(chat, message_ids, revoke=True)
            return len(message_ids)
        except FloodWaitError as error:
            log.warning("Flood wait %ss deleting messages", error.seconds)
            await asyncio.sleep(error.seconds + 1)
        except Exception as error:  # noqa: BLE001
            log.warning("Could not delete %s message(s): %s", len(message_ids), error)
            return 0


async def delete_offender_messages(chat, offenders: set[int]) -> int:
    """Delete every text, media, and service message sent by offenders."""
    if not offenders:
        return 0

    deleted = 0
    batch: list[int] = []
    async for message in client.iter_messages(chat):
        if message.sender_id not in offenders:
            continue
        batch.append(message.id)
        if len(batch) == DELETE_BATCH_SIZE:
            deleted += await delete_message_batch(chat, batch)
            batch = []

    if batch:
        deleted += await delete_message_batch(chat, batch)
    return deleted


async def process_group(chat, me_id: int) -> tuple[int, int, bool]:
    """Lock, purge, and ban in one group. Return banned, deleted, locked."""
    locked = await lock_group(chat)

    admin_ids = await get_admin_ids(chat)
    admin_ids.add(me_id)
    offenders = await find_media_offenders(chat, admin_ids)

    deleted = await delete_offender_messages(chat, offenders)
    banned = 0
    for user_id in offenders:
        if await ban_user(chat, user_id):
            banned += 1

    return banned, deleted, locked


@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=rf"^{re.escape(PREFIX)}doall$",
    )
)
async def doall(event):
    await event.edit("Processing groups…")
    me = await client.get_me()

    groups = 0
    locked = 0
    total_banned = 0
    total_deleted = 0

    async for dialog in client.iter_dialogs():
        if not dialog.is_group or not await i_am_admin(dialog.entity):
            continue

        groups += 1
        try:
            banned, deleted, was_locked = await process_group(dialog.entity, me.id)
            total_banned += banned
            total_deleted += deleted
            locked += int(was_locked)
            log.info(
                "%s: locked=%s deleted=%s banned=%s",
                dialog.name,
                was_locked,
                deleted,
                banned,
            )
        except FloodWaitError as error:
            await asyncio.sleep(error.seconds + 1)
        except Exception as error:  # noqa: BLE001
            log.warning("Skipped %s: %s", dialog.name, error)

    await event.edit(
        f"Done. Groups: {groups} | Locked: {locked} | "
        f"Deleted: {total_deleted} | Banned: {total_banned}"
    )


async def main():
    await client.start()
    me = await client.get_me()
    log.info(
        "Logged in as %s (id %s). Send %sdoall to run.",
        me.first_name,
        me.id,
        PREFIX,
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
