"""Telethon userbot that locks and cleans every administered group.

Commands (send from your own account):

  .doall  In every group where the account has admin rights:
            1. Disables all messages from non-admins.
            2. Deletes the messages of every non-admin who posted.
            3. Bans the non-admins who posted media (text-only
               senders are not banned -- their messages are just
               deleted).

  .dela   In every group where the account has admin rights:
            1. Deletes join/leave service messages (joined via link,
               joined group, left group, joined by request).
            2. Promotes the account to anonymous admin.
            3. Sends and pins a "Group cleaned!" message.
          Reports how many groups succeeded and which ones failed.

  .ban    Ban a user from every group the account administers.
          Target the user by `.ban @username`, `.ban <user_id>`, or
          by replying to one of their messages with `.ban`.
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
from telethon.tl.functions.channels import EditAdminRequest, EditBannedRequest
from telethon.tl.functions.messages import EditChatDefaultBannedRightsRequest
from telethon.tl.types import (
    ChannelParticipantsAdmins,
    ChatAdminRights,
    ChatBannedRights,
    MessageActionChatAddUser,
    MessageActionChatDeleteUser,
    MessageActionChatJoinedByLink,
    MessageActionChatJoinedByRequest,
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

# Service-message actions removed by `.dela` (membership churn).
JOIN_LEAVE_ACTIONS = (
    MessageActionChatJoinedByLink,
    MessageActionChatJoinedByRequest,
    MessageActionChatAddUser,
    MessageActionChatDeleteUser,
)

# Admin rights granted when promoting the account to an anonymous admin.
ANON_ADMIN_RIGHTS = ChatAdminRights(
    change_info=True,
    delete_messages=True,
    ban_users=True,
    invite_users=True,
    pin_messages=True,
    add_admins=False,
    anonymous=True,
    manage_call=True,
)
CLEANED_MESSAGE = "Group cleaned!"


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


async def classify_non_admin_senders(
    chat, admin_ids: set[int]
) -> tuple[set[int], set[int]]:
    """Classify non-admin human senders by what they posted.

    Returns (text_only, media_senders):
      - media_senders: posted at least one media message.
      - text_only: only ever posted text (never media).
    Service messages (joins/leaves) are ignored here.
    """
    media_senders: set[int] = set()
    text_senders: set[int] = set()
    is_human: dict[int, bool] = {}

    async for message in client.iter_messages(chat):
        # Skip service messages; those are handled by `.dela`.
        if getattr(message, "action", None) is not None:
            continue

        sender_id = message.sender_id
        if sender_id is None or sender_id < 0 or sender_id in admin_ids:
            continue

        if sender_id not in is_human:
            sender = await message.get_sender()
            is_human[sender_id] = isinstance(sender, User) and not sender.bot
        if not is_human[sender_id]:
            continue

        if message.media:
            media_senders.add(sender_id)
        elif message.text:
            text_senders.add(sender_id)

    text_only = text_senders - media_senders
    return text_only, media_senders


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
    """Lock, purge, and ban in one group. Return banned, deleted, locked.

    Text-only senders have their messages deleted. Media senders have their
    messages deleted and are also banned.
    """
    locked = await lock_group(chat)

    admin_ids = await get_admin_ids(chat)
    admin_ids.add(me_id)
    text_only, media_senders = await classify_non_admin_senders(chat, admin_ids)

    # Delete messages from every non-admin poster (text-only and media alike).
    deleted = await delete_offender_messages(chat, text_only | media_senders)

    # Ban only the media senders.
    banned = 0
    for user_id in media_senders:
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


async def delete_service_messages(chat) -> int:
    """Delete join/leave service messages from a group's history."""
    deleted = 0
    batch: list[int] = []
    async for message in client.iter_messages(chat):
        action = getattr(message, "action", None)
        if not isinstance(action, JOIN_LEAVE_ACTIONS):
            continue
        batch.append(message.id)
        if len(batch) == DELETE_BATCH_SIZE:
            deleted += await delete_message_batch(chat, batch)
            batch = []

    if batch:
        deleted += await delete_message_batch(chat, batch)
    return deleted


async def promote_to_anonymous(chat, user_id: int) -> bool:
    """Promote the account to an anonymous admin. Best-effort."""
    while True:
        try:
            await client(
                EditAdminRequest(
                    chat,
                    user_id,
                    ANON_ADMIN_RIGHTS,
                    rank="",
                )
            )
            return True
        except FloodWaitError as error:
            log.warning("Flood wait %ss promoting to anonymous", error.seconds)
            await asyncio.sleep(error.seconds + 1)
        except Exception as error:  # noqa: BLE001
            log.warning("Could not promote to anonymous: %s", error)
            return False


async def send_and_pin(chat) -> bool:
    """Send the 'cleaned' notice and pin it. Best-effort."""
    while True:
        try:
            message = await client.send_message(chat, CLEANED_MESSAGE)
            await client.pin_message(chat, message, notify=False)
            return True
        except FloodWaitError as error:
            log.warning("Flood wait %ss sending/pinning notice", error.seconds)
            await asyncio.sleep(error.seconds + 1)
        except Exception as error:  # noqa: BLE001
            log.warning("Could not send/pin notice: %s", error)
            return False


async def clean_group(chat, me_id: int) -> tuple[int, bool, bool]:
    """Delete churn, go anonymous, pin notice. Return deleted, anon, pinned."""
    deleted = await delete_service_messages(chat)
    anonymous = await promote_to_anonymous(chat, me_id)
    pinned = await send_and_pin(chat)
    return deleted, anonymous, pinned


@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=rf"^{re.escape(PREFIX)}dela$",
    )
)
async def dela(event):
    await event.edit("Cleaning groups…")
    me = await client.get_me()

    groups = 0
    succeeded = 0
    total_deleted = 0
    failed: list[str] = []

    async for dialog in client.iter_dialogs():
        if not dialog.is_group or not await i_am_admin(dialog.entity):
            continue

        groups += 1
        try:
            deleted, anonymous, pinned = await clean_group(dialog.entity, me.id)
            total_deleted += deleted
            if anonymous and pinned:
                succeeded += 1
            else:
                failed.append(dialog.name)
            log.info(
                "%s: deleted=%s anonymous=%s pinned=%s",
                dialog.name,
                deleted,
                anonymous,
                pinned,
            )
        except FloodWaitError as error:
            await asyncio.sleep(error.seconds + 1)
            failed.append(dialog.name)
        except Exception as error:  # noqa: BLE001
            log.warning("Skipped %s: %s", dialog.name, error)
            failed.append(dialog.name)

    report = (
        f"Done. Groups: {groups} | Cleaned: {succeeded} | "
        f"Service msgs deleted: {total_deleted}"
    )
    if failed:
        preview = ", ".join(failed[:10]) + ("…" if len(failed) > 10 else "")
        report += f"\nFailed ({len(failed)}): {preview}"
    await event.edit(report)


async def resolve_user_id(user_id: int):
    """Resolve a bare numeric user id into a bannable input entity.

    A userbot session usually lacks a user's access hash, so `get_entity(id)`
    fails even if you have chatted with them. This tries, in order:
      1. the session cache,
      2. priming the cache by loading dialogs (covers DMs), then retrying,
      3. scanning the participants of every group for a matching id.
    """
    try:
        return await client.get_input_entity(user_id)
    except (ValueError, TypeError):
        pass

    try:
        await client.get_dialogs()
        return await client.get_input_entity(user_id)
    except Exception:  # noqa: BLE001
        pass

    async for dialog in client.iter_dialogs():
        if not dialog.is_group:
            continue
        try:
            async for participant in client.iter_participants(dialog.entity):
                if participant.id == user_id:
                    return participant
        except Exception:  # noqa: BLE001
            continue
    return None


async def resolve_target(event):
    """Resolve a ban target from the command argument or a replied message.

    Returns a user entity/input-entity suitable for banning, or None.
    """
    arg = (event.pattern_match.group(1) or "").strip()

    if arg:
        if arg.lstrip("-").isdigit():
            return await resolve_user_id(int(arg))
        # @username, t.me link, or a resolvable name.
        try:
            return await client.get_entity(arg)
        except Exception as error:  # noqa: BLE001
            log.warning("Could not resolve %r: %s", arg, error)
            return None

    reply = await event.get_reply_message()
    if reply and reply.sender_id and reply.sender_id > 0:
        try:
            return await reply.get_sender()
        except Exception:  # noqa: BLE001
            return await resolve_user_id(reply.sender_id)
    return None


@client.on(
    events.NewMessage(
        outgoing=True,
        pattern=rf"^{re.escape(PREFIX)}ban(?:\s+(.+))?$",
    )
)
async def ban_everywhere(event):
    target = await resolve_target(event)
    if target is None:
        await event.edit(
            f"Usage: {PREFIX}ban <@username|id> — or reply to a user with {PREFIX}ban"
        )
        return

    username = getattr(target, "username", None)
    label = (
        f"@{username}"
        if username
        else getattr(target, "id", None) or getattr(target, "user_id", None) or "user"
    )
    await event.edit(f"Banning {label} across every group you administer…")

    groups = 0
    banned = 0
    failed: list[str] = []

    async for dialog in client.iter_dialogs():
        if not dialog.is_group or not await i_am_admin(dialog.entity):
            continue

        groups += 1
        try:
            if await ban_user(dialog.entity, target):
                banned += 1
            else:
                failed.append(dialog.name)
        except FloodWaitError as error:
            await asyncio.sleep(error.seconds + 1)
            failed.append(dialog.name)
        except Exception as error:  # noqa: BLE001
            log.warning("Ban failed in %s: %s", dialog.name, error)
            failed.append(dialog.name)

    report = f"Done. Banned {label} in {banned}/{groups} group(s)."
    if failed:
        preview = ", ".join(failed[:10]) + ("…" if len(failed) > 10 else "")
        report += f"\nFailed ({len(failed)}): {preview}"
    await event.edit(report)


async def main():
    await client.start()
    me = await client.get_me()
    log.info(
        "Logged in as %s (id %s). Commands: %sdoall  %sdela  %sban",
        me.first_name,
        me.id,
        PREFIX,
        PREFIX,
        PREFIX,
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
