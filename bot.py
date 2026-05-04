COOKIE_FILE = "cookies.txt"
import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Pyrogram currently expects a default event loop during import on Windows/Python 3.14.
asyncio.set_event_loop(asyncio.new_event_loop())

from dotenv import load_dotenv
from pyrogram import Client, filters, idle
import pyrogram.errors as pyrogram_errors
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

if not hasattr(pyrogram_errors, "GroupcallForbidden"):
    pyrogram_errors.GroupcallForbidden = pyrogram_errors.BadRequest

from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from yt_dlp import YoutubeDL


YOUTUBE_RE = re.compile(r"^(https?://)?(www\.)?(youtube\.com|youtu\.be)/", re.I)


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    requested_by: str
    video: bool = False


@dataclass
class ChatState:
    queue: list[Track] = field(default_factory=list)
    current: Optional[Track] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ASSISTANT_SESSION = os.environ["ASSISTANT_SESSION"]
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "/")
DEBUG_LOGS = os.getenv("DEBUG_LOGS", "0").lower() in {"1", "true", "yes", "on"}
KNOWN_CHATS_FILE = Path(os.getenv("KNOWN_CHATS_FILE", "known_chats.json"))
AUTO_JOIN_ASSISTANT = os.getenv("AUTO_JOIN_ASSISTANT", "1").lower() in {"1", "true", "yes", "on"}
OWNER_IDS: set[int] = set()
for owner_id in os.getenv("OWNER_IDS", "").replace(";", ",").split(","):
    owner_id = owner_id.strip()
    if not owner_id:
        continue
    try:
        OWNER_IDS.add(int(owner_id))
    except ValueError:
        pass

bot = Client(
    "control_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)
assistant = Client(
    "assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=ASSISTANT_SESSION,
)
calls = PyTgCalls(assistant)

chat_states: dict[int, ChatState] = {}
assistant_user_id: Optional[int] = None
assistant_join_attempts: set[int] = set()
known_chats_lock = asyncio.Lock()


def command(names: str | list[str]):
    return filters.command(names, prefixes=COMMAND_PREFIX)


def get_chat_state(chat_id: int) -> ChatState:
    if chat_id not in chat_states:
        chat_states[chat_id] = ChatState()
    return chat_states[chat_id]


def load_known_chats() -> dict[str, dict[str, str]]:
    if not KNOWN_CHATS_FILE.exists():
        return {}
    try:
        return json.loads(KNOWN_CHATS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_known_chats(chats: dict[str, dict[str, str]]) -> None:
    KNOWN_CHATS_FILE.write_text(
        json.dumps(chats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_group_chat(message: Message) -> bool:
    if not message.chat:
        return False
    chat_type = str(getattr(message.chat, "type", "")).lower()
    return "group" in chat_type


def is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in OWNER_IDS)


async def remember_chat(message: Message) -> None:
    if not is_group_chat(message):
        return

    chat_id = str(message.chat.id)
    title = getattr(message.chat, "title", None) or chat_id

    async with known_chats_lock:
        chats = load_known_chats()
        chats[chat_id] = {"title": title}
        save_known_chats(chats)

    if AUTO_JOIN_ASSISTANT:
        asyncio.create_task(ensure_assistant_in_chat(message.chat.id, title))


async def ensure_assistant_in_chat(chat_id: int, title: str) -> None:
    if assistant_user_id is None or chat_id in assistant_join_attempts:
        return

    assistant_join_attempts.add(chat_id)
    try:
        try:
            await bot.get_chat_member(chat_id, assistant_user_id)
            return
        except Exception:
            pass

        invite = await bot.create_chat_invite_link(
            chat_id,
            name="Assistant auto join",
            creates_join_request=False,
        )
        await assistant.join_chat(invite.invite_link)
        print(f"Assistant ket komê: {title} ({chat_id})")
    except pyrogram_errors.FloodWait as exc:
        await asyncio.sleep(exc.value)
        assistant_join_attempts.discard(chat_id)
    except Exception as exc:
        if DEBUG_LOGS:
            print(f"Assistant nekarî bikeve komê: {title} ({chat_id}) - {exc}")


def mention(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Bikarhênerê nenas"
    return user.mention if user.username else user.first_name


def player_buttons(paused: bool = False) -> InlineKeyboardMarkup:
    pause_label = "Berdewam" if paused else "Sekinîne"
    pause_action = "resume" if paused else "pause"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Derbas", callback_data="player:skip"),
                InlineKeyboardButton(pause_label, callback_data=f"player:{pause_action}"),
            ],
            [
                InlineKeyboardButton("Bigire", callback_data="player:stop"),
                InlineKeyboardButton("Peyamê jê bibe", callback_data="player:delete"),
            ],
        ]
    )


def now_playing_text(track: Track, prefix: str = "Weşan dest pê kir") -> str:
    return (
        f"{prefix}\n\n"
        f"**{track.title}**\n"
        f"Cure: {'Vîdeo' if track.video else 'Deng'}\n"
        f"Daxwaz: {track.requested_by}"
    )


@bot.on_message(filters.text, group=-1)
async def debug_incoming(_: Client, message: Message) -> None:
    await remember_chat(message)
    if not DEBUG_LOGS:
        return
    text = message.text or ""
    if text.startswith(COMMAND_PREFIX):
        chat_title = getattr(message.chat, "title", None) or message.chat.id
        print(f"[command] chat={chat_title} text={text!r}")


async def resolve_youtube(query: str, *, video: bool, requested_by: str) -> Track:
    search = query if YOUTUBE_RE.search(query) else f"ytsearch1:{query}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[height<=720][vcodec!=none][acodec!=none]/best" if video else "bestaudio/best",
        "default_search": "ytsearch",
        "extract_flat": False,
        "cachedir": False,
        "cookiefile": "cookies.txt",
    }

    def extract() -> dict:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search, download=False)
            if "entries" in info:
                info = next(entry for entry in info["entries"] if entry)
            return info

    info = await asyncio.to_thread(extract)
    return Track(
        title=info.get("title", "Başlık bulunamadı"),
        webpage_url=info.get("webpage_url") or query,
        stream_url=info["url"],
        requested_by=requested_by,
        video=video,
    )


async def start_track(chat_id: int, track: Track) -> None:
    state = get_chat_state(chat_id)
    state.current = track
    await calls.play(chat_id, track.stream_url)


async def play_next(chat_id: int) -> Optional[Track]:
    state = get_chat_state(chat_id)
    if not state.queue:
        state.current = None
        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass
        return None

    next_track = state.queue.pop(0)
    await start_track(chat_id, next_track)
    return next_track


async def pause_chat(chat_id: int) -> bool:
    return await calls.pause(chat_id)


async def resume_chat(chat_id: int) -> bool:
    return await calls.resume(chat_id)


async def stop_chat(chat_id: int) -> None:
    state = get_chat_state(chat_id)
    state.queue.clear()
    state.current = None
    try:
        await calls.leave_call(chat_id)
    except Exception:
        pass


async def add_or_start(message: Message, *, video: bool) -> None:
    if not message.chat:
        return

    query = message.text.split(maxsplit=1)
    if len(query) < 2:
        await message.reply_text(
            "Ji kerema xwe girêdanek YouTube an nivîsa lêgerînê binivîse.\n"
            f"Mînak: `{COMMAND_PREFIX}stran Ahmet Kaya Kum Gibi`"
        )
        return

    status = await message.reply_text("Daxwaz tê amadekirin...")
    try:
        track = await resolve_youtube(query[1], video=video, requested_by=mention(message))
    except Exception as exc:
        await status.edit_text(
            "Medya nehat amadekirin. Girêdan an nivîsa lêgerînê kontrol bike û careke din biceribîne.\n"
            f"`{exc}`"
        )
        return

    state = get_chat_state(message.chat.id)
    async with state.lock:
        if state.current is None:
            try:
                await start_track(message.chat.id, track)
            except Exception as exc:
                state.current = None
                await status.edit_text(
                    "Nikarin bikevim axaftina deng/vîdeo. Ji kerema xwe kontrol bike ku axaftin vekirî ye û hesabê assistant di komê de ye.\n"
                    f"`{exc}`"
                )
                return
            await status.edit_text(
                now_playing_text(track),
                reply_markup=player_buttons(),
            )
        else:
            state.queue.append(track)
            await status.edit_text(
                f"Li rêzê hat zêdekirin\n\n"
                f"**{track.title}**\n"
                f"Rêz: `{len(state.queue)}`\n"
                f"Daxwaz: {track.requested_by}"
            )


@bot.on_message(command(["start", "help", "yardim", "yardım", "alikari", "alîkarî"]))
async def help_handler(_: Client, message: Message) -> None:
    await message.reply_text(
        "**Telegram Video Chat Assistant**\n\n"
        "Girêdanên YouTube di axaftinên deng/vîdeo yên koman de diweşîne.\n\n"
        "**Lêdan**\n"
        f"`{COMMAND_PREFIX}play`, `{COMMAND_PREFIX}cal`, `{COMMAND_PREFIX}stran` - Weşana deng\n"
        f"`{COMMAND_PREFIX}vplay`, `{COMMAND_PREFIX}video`, `{COMMAND_PREFIX}vstran` - Weşana vîdeo\n\n"
        "**Kontrol**\n"
        f"`{COMMAND_PREFIX}pause`, `{COMMAND_PREFIX}duraklat`, `{COMMAND_PREFIX}sekinine` - Sekinîne\n"
        f"`{COMMAND_PREFIX}resume`, `{COMMAND_PREFIX}devam`, `{COMMAND_PREFIX}berdewam` - Berdewam bike\n"
        f"`{COMMAND_PREFIX}skip`, `{COMMAND_PREFIX}atla`, `{COMMAND_PREFIX}derbas` - Derbasî ya din bibe\n"
        f"`{COMMAND_PREFIX}stop`, `{COMMAND_PREFIX}kapat`, `{COMMAND_PREFIX}bigire` - Weşanê bigire\n\n"
        "**Agahî**\n"
        f"`{COMMAND_PREFIX}queue`, `{COMMAND_PREFIX}kuyruk`, `{COMMAND_PREFIX}liste` - Rêzê nîşan bide\n"
        f"`{COMMAND_PREFIX}now`, `{COMMAND_PREFIX}simdi`, `{COMMAND_PREFIX}niha` - Ya niha tê weşandin\n\n"
        "**Rêvebirî**\n"
        f"`{COMMAND_PREFIX}duyuru`, `{COMMAND_PREFIX}broadcast`, `{COMMAND_PREFIX}ragihandin` - Ragihandin\n"
        f"`{COMMAND_PREFIX}yardim`, `{COMMAND_PREFIX}alikari` - Alîkarî"
    )


@bot.on_message(command("ping"))
async def ping_handler(_: Client, message: Message) -> None:
    await message.reply_text("Bot amade ye.")


@bot.on_message(command("id"))
async def id_handler(_: Client, message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "Nayê zanîn"
    chat_id = message.chat.id if message.chat else "Nayê zanîn"
    await message.reply_text(
        f"**Agahiyên Nasnameyê**\n"
        f"ID ya bikarhêner: `{user_id}`\n"
        f"ID ya axaftinê: `{chat_id}`"
    )


@bot.on_message(command(["groups", "gruplar", "kom", "komal", "komalên"]))
async def groups_handler(_: Client, message: Message) -> None:
    if not OWNER_IDS:
        user_id = message.from_user.id if message.from_user else "Nayê zanîn"
        await message.reply_text(
            "Pêşî divê xwediyê botê di pelê `.env` de were danîn.\n"
            f"ID ya te: `{user_id}`\n"
            "Mînak: `OWNER_IDS=123456789`"
        )
        return

    if not is_owner(message):
        await message.reply_text("Tenê xwediyê botê dikare vê fermanê bikar bîne.")
        return

    chats = load_known_chats()
    if not chats:
        await message.reply_text("Hêj komeke tomarbûyî tune ye.")
        return

    lines = [
        f"{idx}. {chat['title']} (`{chat_id}`)"
        for idx, (chat_id, chat) in enumerate(chats.items(), start=1)
    ]
    await message.reply_text("**Komên Tomarbûyî**\n" + "\n".join(lines[:30]))


@bot.on_message(command(["assistant", "asistan", "alîkar", "alikar"]))
async def assistant_handler(_: Client, message: Message) -> None:
    if not message.chat or not is_group_chat(message):
        await message.reply_text("Ev ferman tenê di koman de tê bikaranîn.")
        return

    if not OWNER_IDS:
        user_id = message.from_user.id if message.from_user else "Nayê zanîn"
        await message.reply_text(
            "Ji bo vê çalakiyê pêşî `OWNER_IDS` li `.env` zêde bike.\n"
            f"ID ya te: `{user_id}`"
        )
        return

    if not is_owner(message):
        await message.reply_text("Tenê xwediyê botê dikare vê fermanê bikar bîne.")
        return

    if assistant_user_id is None:
        await message.reply_text("Hesabê assistant hîn amade nîne. Botê ji nû ve bide destpêkirin û careke din biceribîne.")
        return

    assistant_join_attempts.discard(message.chat.id)
    await ensure_assistant_in_chat(message.chat.id, getattr(message.chat, "title", str(message.chat.id)))
    await message.reply_text(
        "Hat ceribandin ku assistant bikeve komê. Heke nekeve, botê admin bike û destûra vexwendina bikarhêneran bide."
    )


@bot.on_message(command(["broadcast", "duyuru", "ragihandin", "ragihîne", "ragihine"]))
async def broadcast_handler(client: Client, message: Message) -> None:
    if not OWNER_IDS:
        user_id = message.from_user.id if message.from_user else "Nayê zanîn"
        await message.reply_text(
            "Ji bo şandina ragihandinê pêşî `OWNER_IDS` li `.env` zêde bike.\n"
            f"ID ya te: `{user_id}`\n"
            "Mînak: `OWNER_IDS=123456789`"
        )
        return

    if not is_owner(message):
        await message.reply_text("Tenê xwediyê botê dikare vê fermanê bikar bîne.")
        return

    parts = (message.text or "").split(maxsplit=1)
    text = parts[1] if len(parts) > 1 else ""
    replied_message = message.reply_to_message

    if not text and not replied_message:
        await message.reply_text(
            "Nivîsa ragihandinê binivîse an bersiva peyama ku dixwazî bişînî bide.\n"
            f"Mînak: `{COMMAND_PREFIX}ragihandin Îşev saet 21:00 weşan heye.`"
        )
        return

    chats = load_known_chats()
    if not chats:
        await message.reply_text("Komeke tomarbûyî ji bo ragihandinê tune ye.")
        return

    status = await message.reply_text(f"Ragihandin tê şandin... Armanc: `{len(chats)}` kom")
    sent = 0
    failed: list[str] = []

    for chat_id, chat in chats.items():
        try:
            if text:
                await client.send_message(int(chat_id), text)
            else:
                await replied_message.copy(int(chat_id))
            sent += 1
            await asyncio.sleep(0.2)
        except pyrogram_errors.FloodWait as exc:
            await asyncio.sleep(exc.value)
            try:
                if text:
                    await client.send_message(int(chat_id), text)
                else:
                    await replied_message.copy(int(chat_id))
                sent += 1
            except Exception:
                failed.append(chat.get("title", chat_id))
        except Exception:
            failed.append(chat.get("title", chat_id))

    result = f"Ragihandin qediya.\nSerkeftî: `{sent}`\nNeserkeftî: `{len(failed)}`"
    if failed:
        result += "\n\nNehatin şandin:\n" + "\n".join(f"- {title}" for title in failed[:10])
    await status.edit_text(result)


@bot.on_message(command(["play", "cal", "çal", "oynat", "stran", "lêde", "lede"]))
async def play_handler(_: Client, message: Message) -> None:
    await add_or_start(message, video=False)


@bot.on_message(command(["vplay", "video", "vcal", "vçal", "voynat", "vstran"]))
async def vplay_handler(_: Client, message: Message) -> None:
    await add_or_start(message, video=True)


@bot.on_message(command(["pause", "duraklat", "beklet", "sekinine", "sekinîne"]))
async def pause_handler(_: Client, message: Message) -> None:
    if not message.chat:
        return
    try:
        paused = await pause_chat(message.chat.id)
    except Exception as exc:
        await message.reply_text(f"Weşan nehat sekinandin.\n`{exc}`")
        return
    await message.reply_text("Weşan hate sekinandin." if paused else "Weşaneke çalak ji bo sekinandinê tune ye.")


@bot.on_message(command(["resume", "devam", "surdur", "sürdür", "berdewam"]))
async def resume_handler(_: Client, message: Message) -> None:
    if not message.chat:
        return
    try:
        resumed = await resume_chat(message.chat.id)
    except Exception as exc:
        await message.reply_text(f"Weşan nekarî berdewam bike.\n`{exc}`")
        return
    await message.reply_text("Weşan berdewam e." if resumed else "Weşaneke sekinandî ji bo berdewamkirinê tune ye.")


@bot.on_message(command(["skip", "atla", "gec", "geç", "derbas"]))
async def skip_handler(_: Client, message: Message) -> None:
    if not message.chat:
        return
    state = get_chat_state(message.chat.id)
    async with state.lock:
        track = await play_next(message.chat.id)
    if track:
        await message.reply_text(
            now_playing_text(track, "Weşana din dest pê kir"),
            reply_markup=player_buttons(),
        )
    else:
        await message.reply_text("Rêz qediya. Weşan hate girtin.")


@bot.on_message(command(["stop", "kapat", "durdur", "bitir", "bigire"]))
async def stop_handler(_: Client, message: Message) -> None:
    if not message.chat:
        return
    state = get_chat_state(message.chat.id)
    async with state.lock:
        await stop_chat(message.chat.id)
    await message.reply_text("Weşan hate girtin. Rêz hate paqijkirin.")


@bot.on_message(command(["queue", "kuyruk", "liste", "sira", "sıra", "rêz", "rez"]))
async def queue_handler(_: Client, message: Message) -> None:
    if not message.chat:
        return
    state = get_chat_state(message.chat.id)
    if not state.queue:
        await message.reply_text("Di rêzê de tiştê li bendê tune ye.")
        return
    lines = [f"{idx}. **{track.title}** - {track.requested_by}" for idx, track in enumerate(state.queue, start=1)]
    await message.reply_text("**Rêz**\n" + "\n".join(lines[:15]))


@bot.on_message(command(["now", "simdi", "şimdi", "calan", "çalan", "niha", "aniha"]))
async def now_handler(_: Client, message: Message) -> None:
    if not message.chat:
        return
    state = get_chat_state(message.chat.id)
    if state.current is None:
        await message.reply_text("Niha weşaneke çalak tune ye.")
        return
    await message.reply_text(
        now_playing_text(state.current, "Weşana çalak"),
        reply_markup=player_buttons(),
    )


@bot.on_callback_query(filters.regex(r"^player:"))
async def player_callback(_: Client, callback: CallbackQuery) -> None:
    message = callback.message
    if not message or not message.chat:
        await callback.answer("Axaftin nehat dîtin.", show_alert=True)
        return

    chat_id = message.chat.id
    action = callback.data.split(":", 1)[1]
    state = get_chat_state(chat_id)

    if action == "delete":
        try:
            await message.delete()
        except Exception as exc:
            await callback.answer(f"Peyam nehat jêbirin: {exc}", show_alert=True)
        return

    if action == "pause":
        try:
            paused = await pause_chat(chat_id)
        except Exception as exc:
            await callback.answer(f"Nehat sekinandin: {exc}", show_alert=True)
            return
        if paused:
            await callback.answer("Weşan hate sekinandin.")
            try:
                await message.edit_reply_markup(reply_markup=player_buttons(paused=True))
            except Exception:
                pass
        else:
            await callback.answer("Weşaneke çalak tune ye.", show_alert=True)
        return

    if action == "resume":
        try:
            resumed = await resume_chat(chat_id)
        except Exception as exc:
            await callback.answer(f"Nekarî berdewam bike: {exc}", show_alert=True)
            return
        if resumed:
            await callback.answer("Weşan berdewam e.")
            try:
                await message.edit_reply_markup(reply_markup=player_buttons(paused=False))
            except Exception:
                pass
        else:
            await callback.answer("Weşaneke sekinandî tune ye.", show_alert=True)
        return

    if action == "stop":
        async with state.lock:
            await stop_chat(chat_id)
        await callback.answer("Weşan hate girtin.")
        try:
            await message.edit_text("Weşan hate girtin. Rêz hate paqijkirin.")
        except Exception:
            pass
        return

    if action == "skip":
        async with state.lock:
            try:
                track = await play_next(chat_id)
            except Exception as exc:
                await callback.answer(f"Nekarî derbas bike: {exc}", show_alert=True)
                return
        if track:
            await callback.answer("Derbasî weşana din bû.")
            try:
                await message.edit_text(
                    now_playing_text(track, "Weşana din dest pê kir"),
                    reply_markup=player_buttons(),
                )
            except Exception:
                pass
        else:
            await callback.answer("Rêz qediya.")
            try:
                await message.edit_text("Rêz qediya. Weşan hate girtin.")
            except Exception:
                pass


@calls.on_update(call_filters.stream_end())
async def stream_end_handler(_: PyTgCalls, update) -> None:
    chat_id = getattr(update, "chat_id", None)
    if chat_id is None:
        return
    state = get_chat_state(chat_id)
    async with state.lock:
        await play_next(chat_id)


async def main() -> None:
    global assistant_user_id
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    await assistant.start()
    assistant_me = await assistant.get_me()
    assistant_user_id = assistant_me.id
    await calls.start()
    await bot.start()
    me = await bot.get_me()
    print(f"Bot dixebite: @{me.username}")
    print(f"Assistant amade ye: @{assistant_me.username or assistant_me.id}")
    print(f"Pêşgira fermanê: {COMMAND_PREFIX!r}. Ji bo rawestandinê Ctrl+C.")
    await idle()
    await bot.stop()
    await calls.stop()
    await assistant.stop()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
