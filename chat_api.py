"""
chat_api.py - the web API the widget talks to, + chat memory stored in MongoDB.

  POST   /api/chat/start    called by the widget on EVERY visit: closes the old chat -> fresh chat + Laddu's texts
  GET    /api/chat/suggestions  personal suggestion chips from the customer's previous chats (suggestions.py)
  GET    /api/chat          Laddu's texts (greeting, popup, chips) + your saved chat
  POST   /api/chat          send a message -> runs the agent -> {reply, steps, cart_changed}
  DELETE /api/chat          start a new chat (the old one is kept for the admin)
  GET    /api/admin/chats   all conversations, for Admin -> Laddu Chats
  GET    /widget.js   the widget code  (these two let the browser load the widget files)
  GET    /widget.css  the widget styles

Each user has one "active" conversation in the `conversations` collection holding EVERY message
(incl. tool calls + results), so Laddu remembers across page refreshes and server restarts.

When does a NEW chat start? (the old one is kept, inactive, for Admin -> Laddu Chats)
  - EVERY visit / page load                    (the widget calls POST /api/chat/start)
  - the customer logs in or logs out           (main.py calls end_active_chats)
  - the chat was idle for CHAT_IDLE_MINUTES    (checked in active_conversation)
  - the customer presses ↻ in the widget      (DELETE /api/chat)
"""

from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core import current_user, db, now, require_admin
from agent import laddu_system_prompt, run_agent
from prompt import SEED_LADDU_TEXTS
from suggestions import personal_suggestions

router = APIRouter()
BASE_DIR = Path(__file__).parent

CHAT_HISTORY_LIMIT = 24                                  # how many past messages we send to the LLM
CHAT_IDLE_MINUTES = 30                                   # no messages for this long -> Laddu starts a fresh chat
LLM_FIELDS = ("role", "content", "tool_calls", "tool_call_id")


class ChatIn(BaseModel):
    message: str


# ----- Memory helpers --------------------------------------------------------------------------
def llm_view(stored):
    """Strip our extra fields (at, steps) - the LLM API only accepts the standard ones."""
    return [{k: m[k] for k in LLM_FIELDS if k in m} for m in stored]


def trim_history(stored, limit=CHAT_HISTORY_LIMIT):
    """The context window: keep only the recent messages, starting at a user message, so we never
    send a tool result without the assistant message that asked for it."""
    recent = stored[-limit:]
    for i, m in enumerate(recent):
        if m["role"] == "user":
            return recent[i:]
    return []


def visible_messages(stored):
    """What people see: user messages + Laddu's final answers (tool traffic is hidden)."""
    return [{"role": m["role"], "content": m["content"], "steps": m.get("steps", []),
             "at": m["at"].isoformat() if m.get("at") else None}
            for m in stored
            if m["role"] in ("user", "assistant") and m.get("content") and not m.get("tool_calls")]


def laddu_texts(user):
    """Laddu's name, greeting, popup and suggestion chips from MongoDB, personalised with {name}."""
    # Texts saved in MongoDB win; anything missing falls back to the defaults in prompt.py
    c = {**SEED_LADDU_TEXTS, **(db.site_content.find_one({"_id": "main"}) or {})}
    first = user["name"].split()[0]
    fill = lambda text: (text or "").replace("{name}", first)
    return {"name": c["laddu_name"], "tagline": c["laddu_tagline"],
            "popup": fill(c["laddu_popup"]), "greeting": fill(c["laddu_greeting"]),
            "suggestions": c["laddu_suggestions"], "avatar_emoji": c["laddu_avatar_emoji"]}


def end_active_chats(user):
    """Close this user's open chat. It is NOT deleted - it stays (inactive) for Admin -> Laddu Chats."""
    db.conversations.update_many({"user_id": user["_id"], "active": True},
                                 {"$set": {"active": False, "ended_at": now()}})


def active_conversation(user):
    """The user's open chat, or None. A chat idle for too long is closed, so Laddu starts fresh."""
    conv = db.conversations.find_one({"user_id": user["_id"], "active": True})
    if conv and now() - conv["updated_at"] > timedelta(minutes=CHAT_IDLE_MINUTES):
        end_active_chats(user)
        return None
    return conv


# ----- Chat routes -----------------------------------------------------------------------------
@router.post("/api/chat/start")
def start_visit(user=Depends(current_user)):
    """Every visit starts a fresh chat. Fast: returns the default chips; the personal ones come next."""
    end_active_chats(user)
    return {"laddu": laddu_texts(user), "messages": []}


@router.get("/api/chat/suggestions")
def chat_suggestions(user=Depends(current_user)):
    """Personal chips from the customer's previous chats + orders (may take a few seconds the first time)."""
    return {"suggestions": personal_suggestions(user, laddu_texts(user)["suggestions"])}


@router.get("/api/chat")
def chat_history(user=Depends(current_user)):
    conv = active_conversation(user)
    return {"laddu": laddu_texts(user), "messages": visible_messages(conv["messages"]) if conv else []}


@router.post("/api/chat")
def chat(body: ChatIn, user=Depends(current_user)):
    text = body.message.strip()
    if not 1 <= len(text) <= 1000:
        raise HTTPException(400, "Please type a message (up to 1000 characters)")
    conv = active_conversation(user)
    history = trim_history(conv["messages"]) if conv else []

    # 1. Build what the LLM sees: fresh rules + recent memory + the new message
    messages = [{"role": "system", "content": laddu_system_prompt(user)}] + llm_view(history)
    messages.append({"role": "user", "content": text})
    first_new = len(messages) - 1

    # 2. Run the agent loop (if Sarvam fails, an error is raised and nothing is saved)
    result = run_agent(user, messages)

    # 3. Save only the NEW messages (user msg + tool calls + tool results + final answer)
    new = messages[first_new:]
    new[0]["at"] = now()
    new[-1]["at"] = now()
    new[-1]["steps"] = result["steps"]
    if conv:
        db.conversations.update_one({"_id": conv["_id"]},
                                    {"$push": {"messages": {"$each": new}}, "$set": {"updated_at": now()}})
    else:
        db.conversations.insert_one({"user_id": user["_id"], "active": True, "messages": new,
                                     "created_at": now(), "updated_at": now()})
    # new_chat=True tells the widget this message started a fresh chat (e.g. the old one timed out),
    # so it clears the old bubbles from the screen.
    return {**result, "new_chat": conv is None}


@router.delete("/api/chat")
def new_chat(user=Depends(current_user)):
    """Start a fresh chat (the ↻ button). The old one is kept (inactive) so the admin can still read it."""
    end_active_chats(user)
    return {"ok": True}


@router.get("/api/admin/chats", dependencies=[Depends(require_admin)])
def admin_chats():
    chats = []
    for c in db.conversations.find().sort("updated_at", -1).limit(50):
        u = db.users.find_one({"_id": c["user_id"]}, {"name": 1, "email": 1}) or {}
        visible = visible_messages(c["messages"])
        chats.append({"id": str(c["_id"]), "user_name": u.get("name", "(deleted user)"),
                      "email": u.get("email", ""), "active": c.get("active", False),
                      "updated_at": c["updated_at"].isoformat(), "count": len(visible), "messages": visible})
    return chats


# ----- Serve the widget files to the browser ---------------------------------------------------
@router.get("/widget.js")
def widget_js():
    return FileResponse(BASE_DIR / "widget.js", media_type="application/javascript")


@router.get("/widget.css")
def widget_css():
    return FileResponse(BASE_DIR / "widget.css", media_type="text/css")
