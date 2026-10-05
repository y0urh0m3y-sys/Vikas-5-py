import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaWebPage,
    MessageMediaEmpty,
    MessageMediaUnsupported,
    Channel,
    Chat,
    User,
    PeerChannel,
    PeerChat,
    PeerUser,
    MessageActionChatEditTitle,
    MessageActionPinMessage
)
from telethon.tl.functions.channels import EditTitleRequest
from telethon.tl.functions.messages import EditChatTitleRequest

# =====================================================================
# 5 ACCOUNTS CONFIGURATION (API CREDENTIALS & ISOLATED SESSIONS)
# Supports Environment Variable overrides for cloud deployment (Render)
# =====================================================================
DEFAULT_ACCOUNTS = [
    {
        "id": 1,
        "name": "First Account",
        "api_id": 32657834,
        "api_hash": "33f16ce47db11ca3a9a9d54e5f9f4f9c",
        "phone": "+916296599646",
        "session": "session_916296599646"
    },
    {
        "id": 2,
        "name": "Second Account",
        "api_id": 32056594,
        "api_hash": "04ef0f504c97a006d0cafe0f45d345fc",
        "phone": "+916355570663",
        "session": "session_916355570663"
    },
    {
        "id": 3,
        "name": "Third Account",
        "api_id": 37839576,
        "api_hash": "b46191fae5afa280f86197eb18fe3a18",
        "phone": "+918310384715",
        "session": "session_918310384715"
    },
    {
        "id": 4,
        "name": "Fourth Account",
        "api_id": 37417907,
        "api_hash": "d8dc35f1ad9e872a9d502145b26a9f18",
        "phone": "+919866377797",
        "session": "session_919866377797"
    },
    {
        "id": 5,
        "name": "Fifth Account",
        "api_id": 33889697,
        "api_hash": "9dd5f71e95bc27b24af405ebf10e2ae6",
        "phone": "+918409670444",
        "session": "session_918409670444"
    }
]

# Allow dynamic overrides from Render environment variables
ACCOUNTS_CONFIG = []
for d_acc in DEFAULT_ACCOUNTS:
    aid = d_acc["id"]
    api_id_env = os.environ.get(f"API_ID_{aid}")
    api_hash_env = os.environ.get(f"API_HASH_{aid}")
    phone_env = os.environ.get(f"PHONE_{aid}")
    session_env = os.environ.get(f"SESSION_{aid}") or os.environ.get(f"SESSION_STRING_{aid}")

    acc_entry = {
        "id": aid,
        "name": os.environ.get(f"NAME_{aid}", d_acc["name"]),
        "api_id": int(api_id_env) if api_id_env and api_id_env.isdigit() else d_acc["api_id"],
        "api_hash": api_hash_env if api_hash_env else d_acc["api_hash"],
        "phone": phone_env if phone_env else d_acc["phone"],
        "session": session_env if session_env else d_acc["session"]
    }
    ACCOUNTS_CONFIG.append(acc_entry)

ACCOUNTS_BY_ID = {acc["id"]: acc for acc in ACCOUNTS_CONFIG}
FILE = "settings.json"

# Configurable Pin Reaction
PIN_REACTION = "🔥"

# Default 5 Continuous NC Titles (Cycles 1 to 5)
DEFAULT_NC_TITLES = [
    "🔥 ALPHA ZONE 🔥",
    "⚡ THUNDER REIGN ⚡",
    "👑 APEX EMPIRE 👑",
    "💎 NITRO KINGS 💎",
    "🚀 SUPREME RULE 🚀"
]

# 5 Custom Auto-Replies for Groups (Cycles 1 to 5)
DEFAULT_GC_REPLIES = [
    "Hello! Currently away, will reply shortly ⚡",
    "Active in group, message acknowledged 👍",
    "Busy at the moment, checking notifications later 🚀",
    "Auto-reply active. Stay tuned! 💬",
    "Ping received. I will review this soon 🔥"
]

# 5 Custom Auto-Replies for DMs (Cycles 1 to 5)
DEFAULT_DM_REPLIES = [
    "Hey! Currently away from DM, will respond soon ⚡",
    "Message received in private chat 👍",
    "Please hold on, reviewing DMs shortly 🚀",
    "Auto-responder active for direct messages 💬",
    "Thanks for reaching out! Will get back to you soon 🔥"
]

GC_REPLIES = list(DEFAULT_GC_REPLIES)
DM_REPLIES = list(DEFAULT_DM_REPLIES)
NC_TITLES = list(DEFAULT_NC_TITLES)
TEXT = GC_REPLIES[0]
NC_INT, VANISH, MAX_REP, COOLDOWN = 3.0, 1.5, 3, 1.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- MULTI-ACCOUNT STATE & TELETHON CLIENT POOL ---
ACCOUNT_STATES = {1: True, 2: False, 3: False, 4: False, 5: False}
CLIENTS = {}          # {acc_id: TelegramClient}
MY_IDS = {}           # {acc_id: int}
USER_INFOS = {}       # {acc_id: {"name": ..., "username": ..., "phone": ..., "id": ...}}
STOP_EVENT = None
RENDER_SERVER = None

# --- SHARED USERBOT STATE ---
REPLY_CHATS, MUTE_CHATS, LOCK_CHATS, UNMUTE_EXEMPTS = set(), set(), set(), set()
DEL_NAME_CHATS = set()  # Chats where all group title change messages are auto-deleted
PIN_CHATS, PIN_EXEMPTS = set(), set()
PIN_DEL_CHATS = set()
PIN_ALL = False
PIN_DEL_ALL = False
PIN_PROCESSED = set()
PIN_NOTIF_REACTED = set()
PIN_NOTIF_DELETED = set()
RECENT_DELETED = set()  # Anti-flood duplicate deletion cache

T_REPLY, T_MUTE, T_LOCK, T_NC_R, T_NC_M, T_NC_L = {}, {}, {}, {}, {}, {}
T_DELNAME = {}          # {cid: set(user_ids)}
CACHE, LAST_REP, REP_CNT, NC_TASKS, NC_SETUP = {}, {}, {}, {}, {}
GC_INDEX, DM_INDEX, NC_INDEX = {}, {}, {}
MUTE_ALL = False
USER_CACHE = {}         # Fast mention cache: {uid: mention}
ENTITY_CACHE = {}       # Pre-resolved peer input entities for instant title edits

def is_target_user(cid, uid):
    """Checks if a user ID belongs to any active target list in this chat."""
    if not uid:
        return False
    if cid in T_DELNAME and uid in T_DELNAME[cid]:
        return True
    if cid in T_NC_L and uid in T_NC_L[cid]:
        return True
    if cid in T_NC_M and uid in T_NC_M[cid]:
        return True
    if cid in T_NC_R and uid in T_NC_R[cid]:
        return True
    if cid in T_LOCK and uid in T_LOCK[cid]:
        return True
    if cid in T_MUTE and uid in T_MUTE[cid]:
        return True
    if cid in T_REPLY and uid in T_REPLY[cid]:
        return True
    return False

# =====================================================================
# RENDER HTTP HEALTH-CHECK DUMMY SERVER
# Render Web Services require an open listening port matching $PORT.
# If $PORT does not respond, Render will fail the deployment with:
# "Port scan timeout" or "Service failed to bind to port".
# This lightweight server responds 200 OK without external dependencies.
# =====================================================================
async def start_render_health_server():
    """Starts an asynchronous HTTP server to satisfy Render's port binding and health check requirements."""
    port_env = os.environ.get("PORT")
    port = int(port_env) if port_env and port_env.isdigit() else 10000

    async def handle_http_request(reader, writer):
        try:
            # Read first line (GET / HTTP/1.1)
            line = await reader.readline()
            while True:
                header = await reader.readline()
                if not header or header == b"\r\n":
                    break

            active_accs = [aid for aid, is_on in ACCOUNT_STATES.items() if is_on]
            body_dict = {
                "status": "healthy",
                "service": "Telethon Multi-Account Userbot",
                "active_accounts": active_accs,
                "connected_clients": list(CLIENTS.keys()),
                "timestamp": int(time.time()),
                "render_port": port
            }
            body = json.dumps(body_dict, indent=2).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                b"Access-Control-Allow-Origin: *\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            writer.write(response)
            await writer.drain()
        except Exception as exc:
            logging.debug(f"Render health check request error: {exc}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handle_http_request, "0.0.0.0", port)
        logging.info(f"🌐 [Render Health Server] Successfully listening on 0.0.0.0:{port} (Satisfies Render Web Service Port Requirement)")
        return server
    except Exception as exc:
        logging.warning(f"⚠️ [Render Health Server] Could not bind to port {port}: {exc}. If running as Background Worker, this can be safely ignored.")
        return None

# =====================================================================
# CONFIG PERSISTENCE (SAVES SCRIPT STATE & PREVENTS PHONE FREEZE)
# =====================================================================
def save_cfg():
    """
    Saves all userbot settings and individual account ON/OFF states into settings.json.
    When restarted, only active accounts boot up, saving mobile RAM/CPU and preventing phone freeze.
    """
    try:
        d = {
            "accounts": {str(k): bool(v) for k, v in ACCOUNT_STATES.items()},
            "r": list(REPLY_CHATS),
            "m": list(MUTE_CHATS),
            "l": list(LOCK_CHATS),
            "u": list(UNMUTE_EXEMPTS),
            "all_dm": MUTE_ALL,
            "del_name_chats": list(DEL_NAME_CHATS),
            "pin": list(PIN_CHATS),
            "pin_all": PIN_ALL,
            "pin_del": list(PIN_DEL_CHATS),
            "pin_del_all": PIN_DEL_ALL,
            "pin_exempt": list(PIN_EXEMPTS),
            "pin_reaction": PIN_REACTION,
            "gc_replies": GC_REPLIES,
            "dm_replies": DM_REPLIES,
            "nc_titles": NC_TITLES,
            "tr": {str(k): list(v) for k, v in T_REPLY.items()},
            "tm": {str(k): list(v) for k, v in T_MUTE.items()},
            "tl": {str(k): list(v) for k, v in T_LOCK.items()},
            "ncr": {str(k): list(v) for k, v in T_NC_R.items()},
            "ncm": {str(k): list(v) for k, v in T_NC_M.items()},
            "ncl": {str(k): list(v) for k, v in T_NC_L.items()},
            "tdel": {str(k): list(v) for k, v in T_DELNAME.items()}
        }
        with open(FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        logging.error(f"Error saving config: {exc}")

def load_cfg():
    """
    Loads saved configurations from settings.json.
    Restores account states, ensuring disabled accounts stay OFF to prevent mobile CPU overload.
    """
    global MUTE_ALL, GC_REPLIES, DM_REPLIES, NC_TITLES
    global PIN_ALL, PIN_DEL_ALL, PIN_REACTION
    if not os.path.exists(FILE):
        return
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            d = json.load(f)

        saved_accounts = d.get("accounts", {})
        for acc in ACCOUNTS_CONFIG:
            aid = acc["id"]
            if str(aid) in saved_accounts:
                ACCOUNT_STATES[aid] = bool(saved_accounts[str(aid)])
            elif aid in saved_accounts:
                ACCOUNT_STATES[aid] = bool(saved_accounts[aid])

        REPLY_CHATS.clear(); REPLY_CHATS.update(d.get("r", []))
        MUTE_CHATS.clear(); MUTE_CHATS.update(d.get("m", []))
        LOCK_CHATS.clear(); LOCK_CHATS.update(d.get("l", []))
        UNMUTE_EXEMPTS.clear(); UNMUTE_EXEMPTS.update(d.get("u", []))
        DEL_NAME_CHATS.clear(); DEL_NAME_CHATS.update(d.get("del_name_chats", []))
        MUTE_ALL = d.get("all_dm", False)

        PIN_CHATS.clear(); PIN_CHATS.update(d.get("pin", []))
        PIN_ALL = bool(d.get("pin_all", False))
        PIN_DEL_CHATS.clear(); PIN_DEL_CHATS.update(d.get("pin_del", []))
        PIN_DEL_ALL = bool(d.get("pin_del_all", False))
        PIN_EXEMPTS.clear(); PIN_EXEMPTS.update(d.get("pin_exempt", []))
        PIN_REACTION = str(d.get("pin_reaction", "🔥"))
        
        saved_gc = d.get("gc_replies", [])
        saved_dm = d.get("dm_replies", [])
        saved_nc = d.get("nc_titles", [])
        if isinstance(saved_gc, list) and len(saved_gc) >= 5:
            GC_REPLIES[:] = saved_gc[:5]
        if isinstance(saved_dm, list) and len(saved_dm) >= 5:
            DM_REPLIES[:] = saved_dm[:5]
        if isinstance(saved_nc, list) and len(saved_nc) >= 2:
            NC_TITLES[:] = saved_nc[:10]

        for k, v in d.get("tr", {}).items(): T_REPLY[int(k)] = set(v)
        for k, v in d.get("tm", {}).items(): T_MUTE[int(k)] = set(v)
        for k, v in d.get("tl", {}).items(): T_LOCK[int(k)] = set(v)
        for k, v in d.get("ncr", {}).items(): T_NC_R[int(k)] = set(v)
        for k, v in d.get("ncm", {}).items(): T_NC_M[int(k)] = set(v)
        for k, v in d.get("ncl", {}).items(): T_NC_L[int(k)] = set(v)
        for k, v in d.get("tdel", {}).items(): T_DELNAME[int(k)] = set(v)
    except Exception as exc:
        logging.error(f"Error loading config: {exc}")

# =====================================================================
# FAST ASYNC HELPERS (ZERO EVENT LOOP FREEZING)
# =====================================================================
async def _delayed_delete(msg, delay):
    """Deletes message in background without blocking command loop."""
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except Exception:
        pass

async def fb(e, txt, d=VANISH):
    """Fast non-blocking feedback. Returns immediately and self-destructs asynchronously."""
    try:
        target = await e.edit(txt, parse_mode="md")
        if d:
            asyncio.create_task(_delayed_delete(target or e, d))
    except Exception:
        try:
            target = await e.edit(txt, parse_mode=None)
            if d:
                asyncio.create_task(_delayed_delete(target or e, d))
        except Exception:
            pass

class TargetUser:
    """Lightweight user target container ensuring ID resolution works even without entity cache."""
    def __init__(self, uid, first_name=None, username=None):
        self.id = uid
        self.first_name = first_name or f"User_{uid}"
        self.username = username
        self.title = None

async def get_targets(client, e, arg=""):
    """Accurately extracts target users from reply or arguments without failing on uncached IDs."""
    targets = []
    
    if e.is_reply:
        try:
            r = await e.get_reply_message()
            if r and r.sender_id:
                s = getattr(r, '_sender', None) or getattr(r, 'sender', None)
                if not s:
                    try:
                        s = await r.get_sender()
                    except Exception:
                        s = None
                if s:
                    targets.append(s)
                else:
                    targets.append(TargetUser(r.sender_id))
        except Exception:
            pass

    tokens = [p.strip(",;@ ") for p in (arg or "").split() if p.strip(",;@ ")]
    for tok in tokens:
        clean_tok = tok.lstrip("-")
        if clean_tok.isdigit():
            uid = int(tok)
            try:
                ent = await client.get_entity(uid)
                if ent and ent not in targets:
                    targets.append(ent)
            except Exception:
                targets.append(TargetUser(uid))
        else:
            try:
                ent = await client.get_entity(tok)
                if ent and ent not in targets:
                    targets.append(ent)
            except Exception:
                pass

    return targets

def uname(u):
    """Clean markdown safe username or mention string."""
    if not u:
        return "@User"
    if getattr(u, "username", None):
        return f"@{u.username}"
    uid = getattr(u, "id", None)
    raw_name = (getattr(u, "first_name", "") or getattr(u, "title", "") or f"User_{uid or ''}").strip()
    clean_name = re.sub(r"[\[\]\(\)*_`]", "", raw_name).strip() or f"User_{uid}"
    if uid:
        return f"[{clean_name}](tg://user?id={uid})"
    return f"@{clean_name}"

async def get_mention(e):
    """High-speed mention resolver with local memory cache to eliminate API lag."""
    sid = e.sender_id
    if not sid:
        return "@there"
    if sid in USER_CACHE:
        return USER_CACHE[sid]

    s = getattr(e, '_sender', None) or getattr(e, 'sender', None)
    if not s:
        try:
            s = await e.get_sender()
        except Exception:
            s = None

    mention = uname(s) if s else f"[User](tg://user?id={sid})"
    USER_CACHE[sid] = mention
    return mention

def is_media(m):
    return m and not isinstance(m, (MessageMediaWebPage, MessageMediaEmpty, MessageMediaUnsupported))

def cache(m):
    if not m or not getattr(m, "chat_id", None) or not getattr(m, "id", None):
        return
    CACHE[(m.chat_id, m.id)] = {
        "cid": m.chat_id,
        "sid": getattr(m, "sender_id", None),
        "text": m.text or m.raw_text or "",
        "media": m.media if is_media(m.media) else None
    }

async def toggle_target(client, e, arg, dic, label, is_stop=False):
    """Toggles target rules with instant feedback and clean state persistence."""
    cid = e.chat_id
    t = await get_targets(client, e, arg)

    if is_stop:
        if t and cid in dic:
            for x in t:
                dic[cid].discard(x.id)
            msg = f"🛑 {label} OFF: " + ", ".join(uname(x) for x in t)
        elif cid in dic:
            dic[cid].clear()
            msg = f"🛑 All {label} cleared for this chat."
        else:
            msg = f"ℹ️ No active {label} in this chat."
    else:
        if not t:
            return await fb(e, "⚠️ **Specify @username, ID, or reply to a message.**")
        dic.setdefault(cid, set()).update(x.id for x in t)
        msg = f"🎯 **{label} ON** ({len(t)}): " + ", ".join(uname(x) for x in t)

    save_cfg()
    return await fb(e, msg)

# =====================================================================
# MESSAGE PINNING ENGINE (ZERO DELAY & SAFE EXCEPTION HANDLING)
# =====================================================================
def should_auto_pin(cid):
    """Determines whether automatic pinning should occur for a given chat ID."""
    if not cid:
        return False
    if cid in PIN_EXEMPTS:
        return False
    if PIN_ALL:
        return True
    return cid in PIN_CHATS

def should_delete_pin_notification(cid):
    """Determines whether pin notification messages should be deleted for a given chat ID."""
    if not cid:
        return False
    if PIN_DEL_ALL:
        return True
    return cid in PIN_DEL_CHATS

def is_pin_action(m):
    """Detects whether a message or event represents a pin service notification."""
    if not m:
        return False
    act = getattr(m, "action", None)
    if act is not None:
        if isinstance(act, MessageActionPinMessage) or type(act).__name__ == "MessageActionPinMessage":
            return True
    if getattr(m, "new_pin", False):
        return True
    return False

async def pin_own_message(client, msg):
    """
    Asynchronously and immediately pins the sent message without delay.
    Handles permissions, flood waits, and API errors cleanly.
    """
    if not msg or not getattr(msg, "id", None) or not getattr(msg, "chat_id", None):
        return
    cid = msg.chat_id
    mid = msg.id
    key = (cid, mid)
    if key in PIN_PROCESSED:
        return
    PIN_PROCESSED.add(key)
    
    if len(PIN_PROCESSED) > 3000:
        items = list(PIN_PROCESSED)
        PIN_PROCESSED.clear()
        PIN_PROCESSED.update(items[1500:])

    try:
        if hasattr(msg, "pin"):
            try:
                await msg.pin(notify=False)
                logging.info(f"Auto-pinned own message {mid} in chat {cid} via msg.pin()")
                return
            except Exception as e_inner:
                logging.debug(f"msg.pin() fallback ({e_inner}), trying client.pin_message()")

        await client.pin_message(cid, mid, notify=False)
        logging.info(f"Auto-pinned own message {mid} in chat {cid} via client.pin_message()")
    except errors.ChatAdminRequiredError:
        logging.warning(f"Cannot auto-pin in chat {cid}: Admin 'Pin Messages' permission required!")
    except errors.FloodWaitError as err:
        logging.warning(f"Telegram FloodWait on auto-pin (chat {cid}): wait {err.seconds}s")
    except errors.rpcerrorlist.MessageNotModifiedError:
        pass
    except (errors.rpcerrorlist.MessageIdInvalidError, errors.rpcerrorlist.MessageDeleteForbiddenError):
        pass
    except errors.RPCError as rpce:
        logging.warning(f"Telegram RPCError on auto-pin (chat {cid}): {rpce}")
    except Exception as exc:
        logging.error(f"Unexpected auto-pin error in chat {cid}: {exc}")

async def toggle_pin(client, e, arg, is_stop=False):
    """Toggles automatic pinning for the current chat or multiple specified chats."""
    cid = e.chat_id
    tokens = [p.strip(",;@ ") for p in (arg or "").split() if p.strip(",;@ ")]
    clean_tokens = [t for t in tokens if t.lower() not in ("on", "off")]

    if not clean_tokens:
        if is_stop:
            PIN_CHATS.discard(cid)
            save_cfg()
            return await fb(e, "🛑 **PIN OFF**")
        else:
            PIN_CHATS.add(cid)
            PIN_EXEMPTS.discard(cid)
            save_cfg()
            return await fb(e, "✅ **PIN ON**")

    resolved_chats = []
    unresolved_chats = []
    for tok in clean_tokens:
        clean_tok = tok.lstrip("-")
        try:
            if clean_tok.isdigit():
                target_id = int(tok)
                ent = await client.get_entity(target_id)
                chat_id = getattr(ent, "id", target_id)
                name = getattr(ent, "title", None) or getattr(ent, "first_name", None) or str(target_id)
                resolved_chats.append((chat_id, name))
            else:
                ent = await client.get_entity(tok)
                chat_id = getattr(ent, "id", None)
                if chat_id:
                    name = getattr(ent, "title", None) or getattr(ent, "first_name", None) or tok
                    resolved_chats.append((chat_id, name))
                else:
                    unresolved_chats.append(tok)
        except Exception:
            unresolved_chats.append(tok)

    if not resolved_chats:
        err_msg = f"⚠️ Could not resolve specified chat(s): {', '.join(unresolved_chats)}"
        return await fb(e, err_msg)

    if is_stop:
        for ch_id, _ in resolved_chats:
            PIN_CHATS.discard(ch_id)
            PIN_CHATS.discard(ch_id if ch_id < 0 else -ch_id)
        msg = f"🛑 **PIN OFF** ({len(resolved_chats)} chats): " + ", ".join(name for _, name in resolved_chats)
    else:
        for ch_id, _ in resolved_chats:
            PIN_CHATS.add(ch_id)
            PIN_EXEMPTS.discard(ch_id)
        msg = f"✅ **PIN ON** ({len(resolved_chats)} chats): " + ", ".join(name for _, name in resolved_chats)

    if unresolved_chats:
        msg += f"\n⚠️ Unresolved: {', '.join(unresolved_chats)}"

    save_cfg()
    return await fb(e, msg)

# =====================================================================
# PIN NOTIFICATION HANDLER & REACTION
# =====================================================================
async def react_to_pin_notification(client, m):
    """
    Attempts to react to a pin service notification message using PIN_REACTION.
    Catches all restrictions cleanly so failure never interrupts bot execution.
    """
    try:
        cid = getattr(m, "chat_id", None)
        mid = getattr(m, "id", None)
        if not cid or not mid:
            return

        if hasattr(m, "react"):
            try:
                await m.react(PIN_REACTION)
                logging.info(f"Reacted {PIN_REACTION} to pin notification {mid} in {cid} via msg.react()")
                return
            except Exception:
                pass

        if hasattr(client, "send_reaction"):
            try:
                await client.send_reaction(cid, mid, PIN_REACTION)
                logging.info(f"Reacted {PIN_REACTION} to pin notification {mid} in {cid} via send_reaction")
                return
            except Exception:
                pass

        try:
            from telethon.tl.functions.messages import SendReactionRequest
            from telethon.tl.types import ReactionEmoji
            ent = await client.get_input_entity(cid)
            await client(SendReactionRequest(
                peer=ent,
                msg_id=mid,
                reaction=[ReactionEmoji(emoticon=PIN_REACTION)]
            ))
            logging.info(f"Reacted {PIN_REACTION} to pin notification {mid} in {cid} via SendReactionRequest")
        except Exception as exc:
            logging.warning(f"Could not react to service pin notification {mid} in {cid}: {exc}")
    except Exception as exc:
        logging.warning(f"Reaction handler exception for message {getattr(m, 'id', None)}: {exc}")

async def handle_pin_notification(client, acc_id, m):
    """
    Handles Telegram pin service notifications:
    1. If generated by our own pin operation: react immediately with PIN_REACTION.
    2. If another person pinned a message and notification deletion is enabled: auto-delete notification.
    """
    if not m or not getattr(m, "chat_id", None) or not getattr(m, "id", None):
        return
    cid = m.chat_id
    mid = m.id
    sid = getattr(m, "sender_id", None) or getattr(m, "user_id", None)
    my_id = MY_IDS.get(acc_id)
    is_out = getattr(m, "out", False) or (sid == my_id)

    if is_out:
        if (cid, mid) in PIN_NOTIF_REACTED:
            return
        PIN_NOTIF_REACTED.add((cid, mid))
        if len(PIN_NOTIF_REACTED) > 2000:
            items = list(PIN_NOTIF_REACTED)
            PIN_NOTIF_REACTED.clear()
            PIN_NOTIF_REACTED.update(items[1000:])
        asyncio.create_task(react_to_pin_notification(client, m))
        return

    if should_delete_pin_notification(cid):
        if (cid, mid) in PIN_NOTIF_DELETED:
            return
        PIN_NOTIF_DELETED.add((cid, mid))
        if len(PIN_NOTIF_DELETED) > 2000:
            items = list(PIN_NOTIF_DELETED)
            PIN_NOTIF_DELETED.clear()
            PIN_NOTIF_DELETED.update(items[1000:])
        try:
            await m.delete()
            logging.info(f"Auto-deleted pin service notification {mid} in chat {cid}")
        except Exception as exc:
            logging.debug(f"Failed to delete pin notification {mid} in {cid}: {exc}")

# =====================================================================
# CONTINUOUS NAME CHANGER (NC) ENGINE
# =====================================================================
async def execute_title_change(client, cid, title):
    """Executes chat title change with pre-resolved entity cache and error diagnostics."""
    try:
        cached_info = ENTITY_CACHE.get(cid)
        if not cached_info:
            entity = await client.get_entity(cid)
            is_channel = getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False) or isinstance(entity, Channel)
            input_entity = await client.get_input_entity(cid)
            cached_info = {"input": input_entity, "is_channel": is_channel, "raw_id": getattr(entity, "id", abs(cid))}
            ENTITY_CACHE[cid] = cached_info

        if cached_info["is_channel"]:
            await client(EditTitleRequest(channel=cached_info["input"], title=title))
        else:
            await client(EditChatTitleRequest(chat_id=cached_info["raw_id"], title=title))
        return True, "Success"
    except errors.FloodWaitError as err:
        logging.warning(f"Telegram FloodWait on NC (chat {cid}): waiting {err.seconds}s")
        return False, f"FloodWait: {err.seconds}s"
    except errors.ChatAdminRequiredError:
        logging.error(f"Cannot change title in {cid}: Admin permission 'Change Group Info' required!")
        return False, "ChatAdminRequired"
    except errors.ChatNotModifiedError:
        return True, "Title already set"
    except Exception as exc:
        logging.error(f"Title change error in {cid}: {exc}")
        ENTITY_CACHE.pop(cid, None)
        return False, str(exc)

async def nc_worker(client, cid, names):
    """High performance NC loop with adaptive delay and admin permission check."""
    i = 0
    try:
        while cid in NC_TASKS:
            title = names[i % len(names)]
            i += 1
            ok, reason = await execute_title_change(client, cid, title)
            if not ok:
                if reason == "ChatAdminRequired":
                    try:
                        await client.send_message(cid, "⚠️ **Auto-NC Stopped:** You need 'Change Group Info' admin rights in this group.")
                    except Exception:
                        pass
                    break
                elif "FloodWait" in reason:
                    try:
                        wait_sec = int(reason.split(":")[1].replace("s", "").strip())
                        await asyncio.sleep(wait_sec + 1)
                    except Exception:
                        await asyncio.sleep(10)
            await asyncio.sleep(NC_INT)
    except asyncio.CancelledError:
        pass
    finally:
        NC_TASKS.pop(cid, None)

def start_nc(client, cid, names):
    """Starts or replaces continuous title changer."""
    stop_nc_task(cid)
    clean_names = [n.strip() for n in names if n.strip()]
    if not clean_names:
        clean_names = list(DEFAULT_NC_TITLES)
    NC_TASKS[cid] = asyncio.create_task(nc_worker(client, cid, clean_names))

def stop_nc_task(cid):
    """Stops continuous title changer and cleans up any setup state."""
    if cid in NC_TASKS:
        try:
            NC_TASKS[cid].cancel()
            del NC_TASKS[cid]
        except Exception:
            pass
    if cid in NC_SETUP:
        del NC_SETUP[cid]

# =====================================================================
# 5-ACCOUNT ON/OFF MANAGEMENT ENGINE (ANTI-FREEZE & RENDER READY)
# =====================================================================
async def start_account(acc_id):
    """
    Connects and starts a specific account client.
    Ensures safe initialization, isolated session usage, and registers event handlers.
    Gracefully handles headless environments (Render) where stdin is not available.
    """
    conf = ACCOUNTS_BY_ID.get(acc_id)
    if not conf:
        return False, f"Account {acc_id} does not exist"

    cl = CLIENTS.get(acc_id)
    if cl and cl.is_connected():
        return True, "Already connected"

    raw_session = conf["session"]
    api_id = conf["api_id"]
    api_hash = conf["api_hash"]
    phone = conf["phone"]

    # Support Telethon StringSession (essential for ephemeral cloud hosts like Render)
    is_string_session = isinstance(raw_session, str) and len(raw_session) > 50 and not raw_session.endswith(".session")
    if is_string_session:
        session_instance = StringSession(raw_session)
    else:
        session_instance = raw_session

    if not cl:
        cl = TelegramClient(session_instance, api_id, api_hash)
        CLIENTS[acc_id] = cl
        register_handlers(cl, acc_id)

    logging.info(f"Connecting Account {acc_id} ({conf['name']} | {phone})...")

    # Connect client
    try:
        await cl.connect()
    except Exception as exc:
        logging.error(f"Failed to connect to Telegram servers for Account {acc_id}: {exc}")
        return False, f"Connection failed: {exc}"

    # Check if authorized without blocking on stdin (prevents EOFError on Render)
    if not await cl.is_user_authorized():
        # In a headless environment (Render), interactive input() raises EOFError.
        # We test if stdin is a TTY before attempting interactive start.
        if not sys.stdin.isatty():
            err_msg = (
                f"Account {acc_id} ({phone}) is not authorized and cannot prompt for OTP in headless Render. "
                f"Please generate a session string/file locally and provide it via SESSION_{acc_id} environment variable."
            )
            logging.warning(f"⚠️ {err_msg}")
            return False, err_msg
        else:
            try:
                await cl.start(phone=phone)
            except (EOFError, KeyboardInterrupt):
                return False, "Interactive login cancelled/EOF"
            except Exception as exc:
                return False, f"Auth error: {exc}"

    me = await cl.get_me()
    MY_IDS[acc_id] = me.id
    USER_INFOS[acc_id] = {
        "id": me.id,
        "name": me.first_name,
        "username": me.username or "No Username",
        "phone": phone
    }
    logging.info(f"✅ Account {acc_id} Connected: {me.first_name} (@{me.username or 'None'}) [ID: {me.id}]")
    return True, f"Connected as {me.first_name}"

async def stop_account(acc_id):
    """
    Disconnects and cleanly stops a specific account client.
    Frees up memory, network sockets, and CPU cycles on mobile phones and cloud containers.
    """
    cl = CLIENTS.get(acc_id)
    if cl:
        try:
            if cl.is_connected():
                await cl.disconnect()
            logging.info(f"🛑 Account {acc_id} disconnected cleanly.")
        except Exception as exc:
            logging.warning(f"Error disconnecting Account {acc_id}: {exc}")
    MY_IDS.pop(acc_id, None)
    USER_INFOS.pop(acc_id, None)

async def toggle_account_cmd(e, acc_num, turn_on):
    """
    Toggles a specific account ON or OFF manually.
    Saves state in settings.json so next restart respects the configuration and prevents phone freezes.
    """
    if acc_num not in (1, 2, 3, 4, 5):
        return await fb(e, "⚠️ **Invalid account number! Use `*acc <1-5> on` or `*acc <1-5> off`**")

    conf = ACCOUNTS_BY_ID[acc_num]
    phone = conf["phone"]

    if turn_on:
        ACCOUNT_STATES[acc_num] = True
        save_cfg()
        await fb(e, f"⏳ **Starting Account {acc_num}** (`{phone}`)...\n_Connecting in background without freezing phone._")
        try:
            ok, res = await start_account(acc_num)
            if ok:
                uinfo = USER_INFOS.get(acc_num, {})
                name = uinfo.get("name", f"Acc {acc_num}")
                uid = uinfo.get("id", "Unknown")
                return await fb(e, f"✅ **Account {acc_num} ON**\n👤 {name} (`{phone}`) | ID: `{uid}`\n⚡ Userbot running actively.")
            else:
                return await fb(e, f"⚠️ **Could not connect Account {acc_num}:** {res}")
        except Exception as exc:
            logging.error(f"Error turning ON Account {acc_num}: {exc}")
            return await fb(e, f"❌ **Error starting Account {acc_num}:** {exc}")
    else:
        ACCOUNT_STATES[acc_num] = False
        save_cfg()
        await stop_account(acc_num)
        return await fb(e, f"🛑 **Account {acc_num} OFF** (`{phone}`)\n💾 Saved to `settings.json`.\n🧹 Session disconnected & phone memory freed.")

async def toggle_all_accounts_cmd(e, turn_on):
    """Toggles all accounts ON or OFF (with safety stagger to prevent phone freezes)."""
    if turn_on:
        await fb(e, "⏳ **Enabling All 5 Accounts...**\n_Staggering connections to prevent CPU freeze._")
        results = []
        for aid in (1, 2, 3, 4, 5):
            ACCOUNT_STATES[aid] = True
            try:
                ok, _ = await start_account(aid)
                results.append(f"Acc {aid}: {'🟢' if ok else '⚠️'}")
                await asyncio.sleep(1.0)
            except Exception:
                results.append(f"Acc {aid}: ❌")
        save_cfg()
        return await fb(e, f"✅ **All Accounts Enabled:** " + " | ".join(results))
    else:
        caller_acc = None
        for aid, cl in CLIENTS.items():
            if cl is e.client:
                caller_acc = aid
                break
        caller_acc = caller_acc or 1
        for aid in (1, 2, 3, 4, 5):
            if aid != caller_acc:
                ACCOUNT_STATES[aid] = False
                await stop_account(aid)
        save_cfg()
        return await fb(e, f"🛑 **Accounts 2-5 Turned OFF**\nAccount {caller_acc} remains active for controls.\n💾 Settings saved.")

async def show_accounts_status(e):
    """Displays a clean real-time status dashboard of all 5 accounts."""
    lines = ["📱 **ACCOUNTS DASHBOARD (5 ACCOUNTS)**"]
    for acc in ACCOUNTS_CONFIG:
        aid = acc["id"]
        phone = acc["phone"]
        is_enabled = ACCOUNT_STATES.get(aid, False)
        cl = CLIENTS.get(aid)
        is_conn = cl is not None and cl.is_connected()
        uinfo = USER_INFOS.get(aid)

        if is_enabled and is_conn and uinfo:
            status_badge = f"🟢 **ACTIVE** ({uinfo['name']} | ID: `{uinfo['id']}`)"
        elif is_enabled:
            status_badge = "🟡 **ENABLED** (Connecting or Idle)"
        else:
            status_badge = "🔴 **OFF** (Sleeping • Prevents phone freeze)"

        lines.append(f"• **Account {aid}** (`{phone}`): {status_badge}")

    lines.append("\n💡 **Account Toggle Commands:**")
    lines.append("• `*acc <1-5> on` ➔ Turn ON account userbot")
    lines.append("• `*acc <1-5> off` ➔ Turn OFF account userbot")
    lines.append("• `*acc all on` ➔ Turn ON all accounts")
    lines.append("• `*acc all off` ➔ Sleep unused accounts to save phone RAM")
    return await fb(e, "\n".join(lines), d=None)

# =====================================================================
# HELP MENU
# =====================================================================
HELP = """📋 **USERBOT ALL COMMANDS & CONTROLS**

📱 **5-ACCOUNT MANAGEMENT (ANTI-FREEZE PHONE CONTROLLER):**
• `*acc <1-5> on` ➔ Turn ON userbot for specific account (e.g., `*acc 2 on`)
• `*acc <1-5> off` ➔ Turn OFF userbot for specific account (e.g., `*acc 2 off`)
• `*acc status` / `*accs` ➔ View status of all 5 accounts
• `*acc all on` ➔ Turn ON all accounts with anti-freeze stagger
• `*acc all off` ➔ Turn OFF secondary accounts to free phone RAM & battery

📌 **MESSAGE PINNING:**
• `*pin` / `*pin on` ➔ Enable automatic pinning of own sent messages in current chat
• `*stop pin` / `*unpin` / `*pin off` ➔ Disable automatic pinning in current chat
• `*pin <IDs/@usernames>` ➔ Enable automatic pinning across specified chats
• `*stop pin <IDs/@usernames>` ➔ Disable automatic pinning for specified chats
• `*pinall` ➔ Enable global auto-pinning across all chats
• `*stop pinall` / `*unpinall` ➔ Disable global auto-pinning
• `*pin except` ➔ Exempt current chat from global auto-pinning
• `*stop pin except` ➔ Remove current chat from pin exceptions
• `*pindel` / `*pindel on` ➔ Auto-delete pin service notifications in current chat
• `*stop pindel` / `*pindel off` ➔ Disable pin notification deletion in current chat
• `*pindelall` ➔ Auto-delete pin service notifications globally
• `*stop pindelall` ➔ Disable global pin notification deletion

🔄 **CONTINUOUS TITLE CHANGER (NC):**
• `*nc` ➔ Start 5-title cycle immediately (default or active titles)
• `*nc n1 | n2 | n3 | n4 | n5` ➔ Start with custom names (pipe/comma separated)
• `*nc setup` ➔ Interactive step-by-step setup wizard
• `*stopnc` / `*stop nc` / `*unnc` ➔ Stop title loop

🎯 **TARGET NC ALERTS & LOCK:**
• `*target nc reply <@user/ID>` ➔ Set target for NC reply callouts
• `*stop target nc reply` ➔ Disable target NC reply
• `*target nc mute <@user/ID>` ➔ Auto-delete target messages + NC trigger
• `*stop target nc mute` ➔ Disable target NC mute
• `*target nc lock <@user/ID>` ➔ Lock chat against target
• `*stop target nc lock` ➔ Disable target NC lock
• `*target delname <@user/ID>` ➔ Auto-delete group name change messages of target
• `*stop target delname [@user]` ➔ Disable target name change message deletion
• `*delname` ➔ Auto-delete ALL "changed group name" service messages in this chat
• `*stop delname` ➔ Disable auto-delete for group name change messages

🎯 **TARGET MESSAGES (Multi-User):**
• `*target reply <@user/ID>` ➔ Auto-reply (1-5 rotation) to target
• `*stop target reply [@user]` ➔ Stop target reply
• `*target mute <@user/ID>` ➔ Auto-delete all messages from target
• `*stop target mute [@user]` ➔ Stop target mute
• `*target lock <@user/ID>` ➔ Lock down target user
• `*stop target lock [@user]` ➔ Stop target lock

👥 **GROUP COMMANDS (5 Custom Rotating Auto-Replies with @ProfileName):**
• `*replyall` ➔ Toggle reply to all group members (1-5 rotation)
• `*stop replyall` (or `*unreplyall`) ➔ Stop replyall
• `*gcmsgs` ➔ View current 5 group auto-replies
• `*setgc <1-5> <msg>` ➔ Edit a specific group reply slot
• `*mutegc` ➔ Auto-delete all incoming group messages
• `*stop mutegc` (or `*unmutegc`) ➔ Stop mutegc
• `*lockall` ➔ Full group lockdown
• `*stop lockall` (or `*unlockall`) ➔ Disable lockall

💬 **DM COMMANDS (5 Custom Rotating Auto-Replies with @ProfileName):**
• `*reply` ➔ Toggle DM auto-reply (1-5 rotation)
• `*stop reply` (or `*unreply`) ➔ Stop DM reply
• `*dmmsgs` ➔ View current 5 DM auto-replies
• `*setdm <1-5> <msg>` ➔ Edit a specific DM reply slot
• `*mute` ➔ Mute incoming messages in this DM
• `*stop mute` (or `*unmute`) ➔ Unmute this DM
• `*muteall` ➔ Global DM mute for all private chats
• `*stop muteall` ➔ Disable global DM mute
• `*lock` ➔ Lock this DM
• `*stop lock` (or `*unlock`) ➔ Unlock this DM

🛑 **STOP & STATUS CONTROLS:**
• `*stopall` / `*stop all` ➔ Emergency stop: kill NC, clear pin & all rules in chat
• `*status` ➔ Real-time dashboard of active features & rules"""

# =====================================================================
# EVENT HANDLER FACTORY (BINDS TO EACH ACCOUNT CLIENT SEPARATELY)
# =====================================================================
def register_handlers(client, acc_id):
    """Registers unified command, incoming, and service event handlers for a specific account client."""

    @client.on(events.NewMessage(outgoing=True))
    @client.on(events.MessageEdited(outgoing=True))
    async def on_cmd(e):
        global MUTE_ALL, GC_REPLIES, DM_REPLIES, NC_TITLES
        global PIN_ALL, PIN_DEL_ALL, PIN_REACTION

        txt = (e.raw_text or "").strip()
        cid = e.chat_id
        if cid is None:
            return

        # Check for outgoing pin action notifications
        if is_pin_action(e):
            asyncio.create_task(handle_pin_notification(client, acc_id, e))
            return

        # Interactive Step-by-Step 5-Name NC Setup Wizard
        if cid in NC_SETUP and not txt.startswith("*"):
            st = NC_SETUP[cid]
            lines = [l.strip() for l in (txt.split("\n") if "\n" in txt else txt.split("|")) if l.strip()]
            for l in (lines or [txt]):
                if len(st["names"]) < 5 and l:
                    st["names"].append(l)
            try:
                await e.delete()
            except Exception:
                pass

            if len(st["names"]) < 5:
                n = len(st["names"]) + 1
                saved = "\n".join([f" {i+1}. `{x}`" for i, x in enumerate(st["names"])])
                msg = f"📝 **[Auto-NC Step {n}/5]**\nSaved ({len(st['names'])}/5):\n{saved}\n\n👉 **Send Name {n} of 5 (or send multiple separated by |):**"
                try:
                    pm = await client.get_messages(cid, ids=st.get("pid")) if st.get("pid") else None
                    if pm:
                        await pm.edit(msg)
                    else:
                        st["pid"] = (await client.send_message(cid, msg)).id
                except Exception:
                    st["pid"] = (await client.send_message(cid, msg)).id
                return
            else:
                names = st["names"][:5]
                NC_TITLES[:] = names
                save_cfg()
                del NC_SETUP[cid]
                start_nc(client, cid, names)
                saved = "\n".join([f" {i+1}. `{x}`" for i, x in enumerate(names)])
                msg = f"🚀 **[Auto-NC Started]**\n{saved}\n\n🔄 Continuously cycling every {NC_INT}s. Type `*stopnc` to stop."
                try:
                    pm = await client.get_messages(cid, ids=st.get("pid")) if st.get("pid") else None
                    if pm:
                        await pm.edit(msg)
                    else:
                        await client.send_message(cid, msg)
                except Exception:
                    await client.send_message(cid, msg)
                return

        # Normal outgoing message from user account: auto-pin immediately if enabled
        if not txt.startswith("*"):
            if e.action is None and should_auto_pin(cid):
                asyncio.create_task(pin_own_message(client, e))
            return

        c = txt[1:].strip()
        cl = c.lower()

        # =============================================================
        # 5-ACCOUNT ON/OFF COMMAND DISPATCHER
        # =============================================================
        if cl in ("accs", "acc status", "accounts", "account status", "acc list"):
            return await show_accounts_status(e)

        if cl in ("acc all on", "accs all on", "accounts all on"):
            return await toggle_all_accounts_cmd(e, True)

        if cl in ("acc all off", "accs all off", "accounts all off"):
            return await toggle_all_accounts_cmd(e, False)

        acc_match = re.match(r"^(?:acc|account|bot)\s*([1-5])\s+(on|off)$", cl)
        if not acc_match:
            acc_match = re.match(r"^(?:acc|account|bot)\s+(on|off)\s+([1-5])$", cl)
            if acc_match:
                state_str = acc_match.group(1)
                num_str = acc_match.group(2)
            else:
                num_str, state_str = None, None
        else:
            num_str = acc_match.group(1)
            state_str = acc_match.group(2)

        if num_str and state_str:
            acc_num = int(num_str)
            turn_on = (state_str == "on")
            return await toggle_account_cmd(e, acc_num, turn_on)

        # Help
        if cl in ("help", "all commands", "all_commands", "commands", "cmds"):
            return await fb(e, HELP, d=None)

        # 5 Group Auto-Reply View & Edit
        if cl in ("gcmsgs", "gc_msgs", "gc reply", "gcreplies", "gc replies"):
            list_txt = "\n".join([f"**{i+1}.** {m}" for i, m in enumerate(GC_REPLIES)])
            return await fb(e, f"👥 **[5 GROUP AUTO-REPLY MESSAGES]**\n{list_txt}\n\n✏️ Change: `*setgc <1-5> <new message>`", d=None)

        if cl.startswith("setgc"):
            parts = c.split(maxsplit=2)
            if len(parts) >= 3 and parts[1].isdigit() and 1 <= int(parts[1]) <= 5:
                idx = int(parts[1]) - 1
                new_msg = parts[2].strip()
                GC_REPLIES[idx] = new_msg
                save_cfg()
                return await fb(e, f"✅ **Group Auto-Reply #{idx+1} updated:**\n`{new_msg}`")
            else:
                return await fb(e, "⚠️ **Usage:** `*setgc <1-5> <your custom reply message>`")

        # 5 DM Auto-Reply View & Edit
        if cl in ("dmmsgs", "dm_msgs", "dm reply", "dmreplies", "dm replies"):
            list_txt = "\n".join([f"**{i+1}.** {m}" for i, m in enumerate(DM_REPLIES)])
            return await fb(e, f"💬 **[5 DM AUTO-REPLY MESSAGES]**\n{list_txt}\n\n✏️ Change: `*setdm <1-5> <new message>`", d=None)

        if cl.startswith("setdm"):
            parts = c.split(maxsplit=2)
            if len(parts) >= 3 and parts[1].isdigit() and 1 <= int(parts[1]) <= 5:
                idx = int(parts[1]) - 1
                new_msg = parts[2].strip()
                DM_REPLIES[idx] = new_msg
                save_cfg()
                return await fb(e, f"✅ **DM Auto-Reply #{idx+1} updated:**\n`{new_msg}`")
            else:
                return await fb(e, "⚠️ **Usage:** `*setdm <1-5> <your custom reply message>`")

        # Stop All in current chat
        if cl in ("stopall", "stop all", "stopchat", "stop chat", "clearall", "clear all"):
            stop_nc_task(cid)
            REPLY_CHATS.discard(cid)
            MUTE_CHATS.discard(cid)
            LOCK_CHATS.discard(cid)
            DEL_NAME_CHATS.discard(cid)
            PIN_CHATS.discard(cid)
            PIN_DEL_CHATS.discard(cid)
            PIN_EXEMPTS.discard(cid)
            UNMUTE_EXEMPTS.add(cid)
            for dic in (T_REPLY, T_MUTE, T_LOCK, T_NC_R, T_NC_M, T_NC_L, T_DELNAME):
                dic.pop(cid, None)
            save_cfg()
            return await fb(e, "🛑 **All features & target rules stopped for this chat.**")

        # Stop Continuous NC
        if cl in ("stopnc", "stop_nc", "stop nc", "ncstop", "nc off", "nc stop", "unnc"):
            stop_nc_task(cid)
            return await fb(e, "🛑 **Continuous NC Stopped.**")

        # Start Continuous NC
        if cl.startswith("nc") and not cl.startswith(("nc reply", "nc mute", "nc lock", "ncr", "ncm", "ncl")):
            sub_arg = c[2:].strip()
            if sub_arg.lower() in ("setup", "wizard", "add"):
                stop_nc_task(cid)
                NC_SETUP[cid] = {"names": [], "pid": e.id}
                return await fb(e, f"📝 **[Auto-NC Setup]** Send 5 names one by one (or separate with |).\n👉 **Send Name 1 of 5:**", d=None)

            raw = []
            if sub_arg:
                if "\n" in sub_arg:
                    raw = [x.strip() for x in sub_arg.split("\n") if x.strip()]
                elif "|" in sub_arg:
                    raw = [x.strip() for x in sub_arg.split("|") if x.strip()]
                elif "," in sub_arg:
                    raw = [x.strip() for x in sub_arg.split(",") if x.strip()]
                else:
                    raw = [sub_arg]

            if not raw:
                raw = list(NC_TITLES)

            if len(raw) >= 2:
                start_nc(client, cid, raw)
                titles_display = "\n".join([f"• `{t}`" for t in raw])
                return await fb(e, f"🚀 **[Auto-NC Started]** ({len(raw)} titles cycling every {NC_INT}s):\n{titles_display}\n\nType `*stopnc` to stop.")
            else:
                stop_nc_task(cid)
                NC_SETUP[cid] = {"names": raw, "pid": e.id}
                return await fb(e, f"📝 **[Auto-NC Setup]** Send names (or separate with |).\n👉 **Send Name {len(raw)+1} of 5:**", d=None)

        # Global Pin Toggle
        if cl in ("pinall", "pin all"):
            PIN_ALL = True
            save_cfg()
            return await fb(e, "📌 **Global Pin ON** (All chats will auto-pin sent messages)")

        if cl in ("stop pinall", "stop pin all", "unpinall", "pinall stop", "pinall off"):
            PIN_ALL = False
            save_cfg()
            return await fb(e, "🛑 **Global Pin OFF**")

        # Pin Exception
        if cl in ("pin except", "pinexcept", "pin_except"):
            PIN_EXEMPTS.add(cid)
            PIN_CHATS.discard(cid)
            save_cfg()
            return await fb(e, "🛡️ **Pin Exception ON** (This chat will NOT be auto-pinned)")

        if cl in ("stop pin except", "stop pinexcept", "unpin except", "unpinexcept", "pin except off"):
            PIN_EXEMPTS.discard(cid)
            save_cfg()
            return await fb(e, "🛑 **Pin Exception OFF**")

        # Pin Notification Deletion Controls
        if cl in ("pindelall", "pindel all", "delpinall", "delpin all"):
            PIN_DEL_ALL = True
            save_cfg()
            return await fb(e, "🗑️ **Global Pin-Notification Delete ON**")

        if cl in ("stop pindelall", "stop pindel all", "unpindelall", "pindelall off", "pindelall stop"):
            PIN_DEL_ALL = False
            save_cfg()
            return await fb(e, "🛑 **Global Pin-Notification Delete OFF**")

        if cl in ("stop pindel", "stop_pindel", "unpindel", "pindel off", "pindel stop", "stop delpin"):
            PIN_DEL_CHATS.discard(cid)
            save_cfg()
            return await fb(e, "🛑 **Pin Notification Delete OFF**")

        if cl in ("pindel", "pindel on", "delpin", "delpin on"):
            PIN_DEL_CHATS.add(cid)
            save_cfg()
            return await fb(e, "✅ **Pin Notification Delete ON**")

        # Chat-Specific / Multi-Chat Pin Commands
        if cl.startswith(("stop pin", "unpin")):
            if cl in ("unpin", "stop pin"):
                return await toggle_pin(client, e, "", is_stop=True)
            arg = c[8:].strip() if cl.startswith("stop pin") else c[5:].strip()
            return await toggle_pin(client, e, arg, is_stop=True)

        if cl == "pin" or cl == "pin on":
            return await toggle_pin(client, e, "", is_stop=False)

        if cl == "pin off":
            return await toggle_pin(client, e, "", is_stop=True)

        if cl.startswith("pin ") and not cl.startswith(("pinall", "pin except", "pindel", "pinexcept")):
            arg = c[4:].strip()
            if arg.lower() == "on":
                return await toggle_pin(client, e, "", is_stop=False)
            elif arg.lower() == "off":
                return await toggle_pin(client, e, "", is_stop=True)
            return await toggle_pin(client, e, arg, is_stop=False)

        # Target NC Stops
        if cl.startswith(("stop target nc reply", "stop nc reply", "untarget nc reply", "target nc stop reply", "stop target ncreply", "untarget ncreply")):
            return await toggle_target(client, e, c.split("reply", 1)[1] if "reply" in c else "", T_NC_R, "Target NC Reply", True)
        if cl.startswith(("stop target nc mute", "stop nc mute", "untarget nc mute", "target nc stop mute", "stop target ncmute", "untarget ncmute")):
            return await toggle_target(client, e, c.split("mute", 1)[1] if "mute" in c else "", T_NC_M, "Target NC Mute", True)
        if cl.startswith(("stop target nc lock", "stop nc lock", "untarget nc lock", "target nc stop lock", "stop target nclock", "untarget nclock")):
            return await toggle_target(client, e, c.split("lock", 1)[1] if "lock" in c else "", T_NC_L, "Target NC Lock", True)
        if cl.startswith(("stop target delname", "stop target del name", "untarget delname", "stop target ncdel", "untarget ncdel", "stop target name", "untarget target delname")):
            arg = c.split("delname", 1)[1] if "delname" in c else (c.split("name", 1)[1] if "name" in c else "")
            return await toggle_target(client, e, arg, T_DELNAME, "Target Auto-Delete Name Changes", True)

        # Target NC Starts
        if cl.startswith("target nc reply"):
            return await toggle_target(client, e, c[15:], T_NC_R, "Target NC Reply")
        if cl.startswith("target nc mute"):
            return await toggle_target(client, e, c[14:], T_NC_M, "Target NC Mute")
        if cl.startswith("target nc lock"):
            return await toggle_target(client, e, c[14:], T_NC_L, "Target NC Lock")
        if cl.startswith(("target delname", "target del name", "target delnc", "target ncdel", "target delete name")):
            arg = c.split("delname", 1)[1] if "delname" in c else (c.split("delnc", 1)[1] if "delnc" in c else c.split("name", 1)[1])
            return await toggle_target(client, e, arg, T_DELNAME, "Target Auto-Delete Name Changes")

        # Chat-Wide Auto-Delete for group title changes
        if cl in ("stop delname", "stop del_name", "undelname", "delname off", "delnc off", "stop delncmsg", "stop deltitle"):
            DEL_NAME_CHATS.discard(cid)
            save_cfg()
            return await fb(e, "🛑 **Auto-Delete Changed Name Messages OFF**")
        if cl in ("delname", "del_name", "delncmsg", "autodelname", "deltitle", "delnames"):
            DEL_NAME_CHATS.add(cid)
            save_cfg()
            return await fb(e, "✅ **Auto-Delete All Changed Name Messages ON**")

        # Target Messages Stops
        if cl.startswith(("stop target reply", "untarget reply", "target stop reply", "stop reply @")) or (cl.startswith("stop reply") and len(cl) > 10):
            return await toggle_target(client, e, c.split("reply", 1)[1] if "reply" in c else "", T_REPLY, "Target Reply", True)
        if cl.startswith(("stop target mute", "untarget mute", "target stop mute", "stop mute @")) or (cl.startswith("stop mute") and len(cl) > 9):
            return await toggle_target(client, e, c.split("mute", 1)[1] if "mute" in c else "", T_MUTE, "Target Mute", True)
        if cl.startswith(("stop target lock", "untarget lock", "target stop lock", "stop lock @")) or (cl.startswith("stop lock") and len(cl) > 9):
            return await toggle_target(client, e, c.split("lock", 1)[1] if "lock" in c else "", T_LOCK, "Target Lock", True)

        # Target Messages Starts
        if cl.startswith("target reply"):
            return await toggle_target(client, e, c[12:], T_REPLY, "Target Reply")
        if cl.startswith("target mute"):
            return await toggle_target(client, e, c[11:], T_MUTE, "Target Mute")
        if cl.startswith("target lock"):
            return await toggle_target(client, e, c[11:], T_LOCK, "Target Lock")

        # Group ReplyAll & Stop
        if cl in ("stop replyall", "stop reply_all", "stopreplyall", "unreplyall", "replyall stop", "replyall off"):
            REPLY_CHATS.discard(cid)
            save_cfg()
            return await fb(e, "🛑 GC **ReplyAll OFF**")
        if cl in ("replyall", "reply_all"):
            REPLY_CHATS.add(cid)
            save_cfg()
            return await fb(e, "✅ GC **ReplyAll ON (5 Custom Messages Cycling with @ProfileName)**")

        # Group MuteGC & Stop
        if cl in ("stop mutegc", "stop mute_gc", "stopmutegc", "unmutegc", "mutegc stop", "mutegc off", "stop mutegroup"):
            MUTE_CHATS.discard(cid)
            UNMUTE_EXEMPTS.add(cid)
            save_cfg()
            return await fb(e, "🛑 GC **MuteGC OFF**")
        if cl in ("mutegc", "mute_gc", "mutegroup"):
            MUTE_CHATS.add(cid)
            UNMUTE_EXEMPTS.discard(cid)
            save_cfg()
            return await fb(e, "✅ GC **MuteGC ON (Auto-deleting member messages)**")

        # Group LockAll & Stop
        if cl in ("stop lockall", "stop lock_all", "stoplockall", "unlockall", "lockall stop", "lockall off", "stop lockgroup"):
            LOCK_CHATS.discard(cid)
            save_cfg()
            return await fb(e, "🛑 GC **LockAll OFF**")
        if cl in ("lockall", "lock_all", "lockgroup"):
            LOCK_CHATS.add(cid)
            save_cfg()
            return await fb(e, "🛡️ GC **LockAll ON**")

        # DM Reply & Stop
        if cl in ("stop reply", "stop_reply", "stopreply", "unreply", "reply stop", "reply off"):
            REPLY_CHATS.discard(cid)
            save_cfg()
            return await fb(e, "🛑 DM **Reply OFF**")
        if cl == "reply":
            REPLY_CHATS.add(cid)
            save_cfg()
            return await fb(e, "✅ DM **Reply ON (5 Custom Messages Cycling with @ProfileName)**")

        # DM Mute & Stop (*unmute)
        if cl in ("stop mute", "stop_mute", "stopmute", "unmute", "mute stop", "mute off"):
            MUTE_CHATS.discard(cid)
            UNMUTE_EXEMPTS.add(cid)
            save_cfg()
            return await fb(e, "🛑 **Unmuted** (Exempted from Global Mute)" if MUTE_ALL else "🛑 DM **Mute OFF**")
        if cl == "mute":
            MUTE_CHATS.add(cid)
            UNMUTE_EXEMPTS.discard(cid)
            save_cfg()
            return await fb(e, "✅ DM **Mute ON**")

        # DM Mute All DMs & Stop
        if cl in ("stop muteall", "stop mute all", "stop_muteall", "stopmuteall", "unmuteall", "unmute all", "muteall stop", "muteall off"):
            MUTE_ALL = False
            UNMUTE_EXEMPTS.clear()
            save_cfg()
            return await fb(e, "🔓 **Mute All DMs OFF**")
        if cl in ("muteall", "mute all", "mute_all"):
            MUTE_ALL = True
            save_cfg()
            return await fb(e, "🔒 **Mute All DMs ON** (Send `*unmute` in a DM to exempt it)")

        # DM Lock & Stop
        if cl in ("stop lock", "stop_lock", "stoplock", "unlock", "lock stop", "lock off"):
            LOCK_CHATS.discard(cid)
            save_cfg()
            return await fb(e, "🛑 DM **Lock OFF**")
        if cl == "lock":
            LOCK_CHATS.add(cid)
            save_cfg()
            return await fb(e, "🛡️ DM **Lock ON**")

        # Status Overview
        if cl == "status":
            r = "🟢 ON" if cid in REPLY_CHATS else "🔴 OFF"
            m = "🟢 ON" if (cid in MUTE_CHATS or (e.is_private and MUTE_ALL and cid not in UNMUTE_EXEMPTS)) else "🔴 OFF"
            l = "🟢 ON" if cid in LOCK_CHATS else "🔴 OFF"
            nc = "🟢 ON" if cid in NC_TASKS else "🔴 OFF"
            ex = " (Exempted)" if cid in UNMUTE_EXEMPTS else ""
            t_r_count = len(T_REPLY.get(cid, set()))
            t_m_count = len(T_MUTE.get(cid, set()))
            t_l_count = len(T_LOCK.get(cid, set()))
            t_del_count = len(T_DELNAME.get(cid, set()))
            del_st = "🟢 ON" if (cid in DEL_NAME_CHATS or t_del_count > 0) else "🔴 OFF"
            pin_auto = "🟢 ON" if should_auto_pin(cid) else "🔴 OFF"
            pin_glob = "🟢 ON" if PIN_ALL else "🔴 OFF"
            pin_exc = "🟢 YES" if cid in PIN_EXEMPTS else "🔴 NO"
            pindel_st = "🟢 ON" if (cid in PIN_DEL_CHATS or PIN_DEL_ALL) else "🔴 OFF"
            pindel_glob = "🟢 ON" if PIN_DEL_ALL else "🔴 OFF"

            active_accs = [str(k) for k, v in ACCOUNT_STATES.items() if v]

            return await fb(
                e,
                f"⚙️ **CHAT STATUS (Account {acc_id})**\n"
                f"• Continuous NC: {nc}\n"
                f"• Auto-Reply: {r} (5 Msg Rotator)\n"
                f"• Mute State: {m}{ex} (Targets: {t_m_count})\n"
                f"• Lock State: {l} (Targets: {t_l_count})\n"
                f"• Auto-Del Name Msgs: {del_st} (Targets: {t_del_count})\n"
                f"• Global DM Mute: {'🟢 ON' if MUTE_ALL else '🔴 OFF'}\n"
                f"• Target Replies: {t_r_count} active\n"
                f"• GC Replies: {len(GC_REPLIES)} slots | DM Replies: {len(DM_REPLIES)} slots\n"
                f"📌 Automatic Pin: {pin_auto}\n"
                f"📌 Global Pin: {pin_glob}\n"
                f"📌 Pin Exception: {pin_exc}\n"
                f"🗑️ Pin Notification Delete: {pindel_st}\n"
                f"🗑️ Global Pin Delete: {pindel_glob}\n"
                f"🔥 Pin Reaction: {PIN_REACTION}\n"
                f"📱 Active Accounts: {', '.join(active_accs) if active_accs else 'None'} / 5",
                d=None
            )

    @client.on(events.NewMessage(incoming=True))
    @client.on(events.MessageEdited(incoming=True))
    async def on_incoming(e):
        if not ACCOUNT_STATES.get(acc_id, False):
            return

        cid = e.chat_id
        sid = e.sender_id
        my_id = MY_IDS.get(acc_id)
        if cid is None or sid == my_id:
            return

        cache(e)

        action = getattr(e, "action", None)
        if action is not None:
            action_name = type(action).__name__
            if isinstance(action, MessageActionChatEditTitle) or action_name == "MessageActionChatEditTitle" or hasattr(action, "title"):
                target_hit = is_target_user(cid, sid)
                chat_del = cid in DEL_NAME_CHATS
                if target_hit or chat_del:
                    try:
                        await e.delete()
                    except Exception:
                        pass
                    if target_hit and (cid in NC_TASKS or cid in T_NC_L or cid in T_NC_M):
                        revert_title = NC_TITLES[0] if NC_TITLES else "🔥 ALPHA ZONE 🔥"
                        asyncio.create_task(execute_title_change(client, cid, revert_title))
                    return

            if is_pin_action(e):
                await handle_pin_notification(client, acc_id, e)
                return

        should_delete = False
        if cid in T_NC_L and sid in T_NC_L[cid]:
            should_delete = True
        elif cid in T_NC_M and sid in T_NC_M[cid]:
            should_delete = True
            asyncio.create_task(execute_title_change(client, cid, f"🚫 MUTED: User {sid}"))
        elif cid in T_LOCK and sid in T_LOCK[cid]:
            should_delete = True
        elif cid in LOCK_CHATS:
            should_delete = True
        elif cid in T_MUTE and sid in T_MUTE[cid]:
            should_delete = True
        elif cid in MUTE_CHATS or (e.is_private and MUTE_ALL and cid not in UNMUTE_EXEMPTS):
            should_delete = True

        if should_delete:
            del_key = (cid, e.id)
            if del_key not in RECENT_DELETED:
                RECENT_DELETED.add(del_key)
                if len(RECENT_DELETED) > 2000:
                    RECENT_DELETED.clear()
                try:
                    await e.delete()
                except Exception:
                    pass
            return

        if cid in T_NC_R and sid in T_NC_R[cid]:
            try:
                mention = await get_mention(e)
                idx = GC_INDEX.get(cid, 0)
                msg_text = GC_REPLIES[idx % len(GC_REPLIES)]
                GC_INDEX[cid] = (idx + 1) % len(GC_REPLIES)
                await e.reply(f"🎯 {mention} {msg_text}", parse_mode="md")
                asyncio.create_task(execute_title_change(client, cid, f"🎯 TARGET ALERT: {sid}"))
            except Exception:
                pass

        is_chat_reply = cid in REPLY_CHATS
        is_target_reply = cid in T_REPLY and sid in T_REPLY[cid]

        if is_chat_reply or is_target_reply:
            now = time.time()
            last_t = LAST_REP.get((cid, sid), 0)
            if now - last_t < COOLDOWN:
                return
            LAST_REP[(cid, sid)] = now

            try:
                mention = await get_mention(e)
                if e.is_private:
                    idx = DM_INDEX.get(cid, 0)
                    msg_text = DM_REPLIES[idx % len(DM_REPLIES)]
                    DM_INDEX[cid] = (idx + 1) % len(DM_REPLIES)
                    try:
                        await e.reply(f"{mention} {msg_text}", parse_mode="md")
                    except Exception:
                        await e.reply(f"{mention} {msg_text}", parse_mode=None)
                else:
                    idx = GC_INDEX.get(cid, 0)
                    msg_text = GC_REPLIES[idx % len(GC_REPLIES)]
                    GC_INDEX[cid] = (idx + 1) % len(GC_REPLIES)
                    try:
                        await e.reply(f"{mention} {msg_text}", parse_mode="md")
                    except Exception:
                        await e.reply(f"{mention} {msg_text}", parse_mode=None)
            except Exception as exc:
                logging.error(f"Auto-reply exception in {cid}: {exc}")

    @client.on(events.ChatAction)
    async def on_chat_action(e):
        if not ACCOUNT_STATES.get(acc_id, False):
            return

        cid = e.chat_id
        if not cid:
            return

        is_name_change = bool(e.new_title or isinstance(getattr(e, "action", None), MessageActionChatEditTitle))
        if is_name_change:
            sid = getattr(e, "user_id", None) or getattr(e, "sender_id", None)
            target_hit = is_target_user(cid, sid)
            chat_del = cid in DEL_NAME_CHATS
            if target_hit or chat_del:
                try:
                    await e.delete()
                except Exception:
                    pass
                if target_hit and (cid in NC_TASKS or cid in T_NC_L or cid in T_NC_M):
                    revert_title = NC_TITLES[0] if NC_TITLES else "🔥 ALPHA ZONE 🔥"
                    asyncio.create_task(execute_title_change(client, cid, revert_title))
            return

        if is_pin_action(e):
            target_msg = getattr(e, "action_message", None) or e
            await handle_pin_notification(client, acc_id, target_msg)

# =====================================================================
# MAIN INITIALIZATION (RENDER & MOBILE COMPATIBLE)
# =====================================================================
async def main():
    global STOP_EVENT, RENDER_SERVER
    STOP_EVENT = asyncio.Event()
    load_cfg()

    print("\n" + "="*65)
    print("  ⚡ TELETHON 5-ACCOUNT USERBOT (RENDER & CLOUD READY)")
    print("  Dedicated session files, StringSession & persistent state")
    print("="*65)

    # 1. Start Render HTTP Dummy Health Check Server
    # Render Web Services require a process listening on 0.0.0.0:$PORT
    RENDER_SERVER = await start_render_health_server()

    # Ensure at least Account 1 is active if no accounts are marked true
    if not any(ACCOUNT_STATES.values()):
        ACCOUNT_STATES[1] = True
        save_cfg()

    enabled_ids = [aid for aid, is_on in ACCOUNT_STATES.items() if is_on]
    print(f"  Active accounts in {FILE}: {enabled_ids}")
    print("  Starting clients with staggered pause to prevent CPU spikes...\n")

    started_count = 0
    for acc in ACCOUNTS_CONFIG:
        aid = acc["id"]
        if ACCOUNT_STATES.get(aid, False):
            try:
                print(f"  👉 Starting Account {aid} ({acc['phone']})...")
                ok, res = await start_account(aid)
                if ok:
                    uinfo = USER_INFOS.get(aid, {})
                    print(f"     ✅ Connected as: {uinfo.get('name')} (ID: {uinfo.get('id')})")
                    started_count += 1
                else:
                    print(f"     ⚠️ Account {aid} status: {res}")
                await asyncio.sleep(1.0)
            except Exception as exc:
                print(f"     ❌ Error starting Account {aid}: {exc}")
        else:
            print(f"  💤 Account {aid} ({acc['phone']}) is OFF in settings.json. (Skipped)")

    print("\n" + "="*65)
    print(f"  🚀 USERBOT RUNNING ({started_count} ACTIVE ACCOUNTS)")
    port_disp = os.environ.get('PORT', '10000')
    print(f"  🌐 Render Web Service Health Server: Port {port_disp}")
    print("  Commands:")
    print("    *acc status       ➔ View all 5 accounts status")
    print("    *acc <1-5> on     ➔ Enable specific account")
    print("    *acc <1-5> off    ➔ Disable specific account")
    print("    *help             ➔ Complete commands cheat sheet")
    print("="*65 + "\n")

    # Run continuously until stop event is triggered
    await STOP_EVENT.wait()

if __name__ == "__main__":
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Setup graceful signal handlers for cloud container shutdown (SIGTERM on Render)
        def handle_signal():
            logging.info("Received termination signal. Exiting gracefully...")
            if STOP_EVENT and not STOP_EVENT.is_set():
                STOP_EVENT.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handle_signal)
            except (NotImplementedError, AttributeError):
                pass

        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        save_cfg()
        print("\nStopping userbot cleanly and disconnecting sessions...")
        for aid, cl in list(CLIENTS.items()):
            try:
                if cl.is_connected():
                    loop.run_until_complete(cl.disconnect())
            except Exception:
                pass
        if RENDER_SERVER:
            try:
                RENDER_SERVER.close()
                loop.run_until_complete(RENDER_SERVER.wait_closed())
            except Exception:
                pass
        print("All settings saved to settings.json. Exited cleanly.")
