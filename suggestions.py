"""
suggestions.py - PERSONAL suggestion chips for a new chat.

Every visit starts a fresh chat, but Laddu still "remembers" the customer through these chips:
the AI reads the customer's previous chats + recent orders and writes 4 things they are likely to
want next (e.g. "Order Soan Papdi again", "Track order PS-1001").

To save time and API credits, the chips are stored in MongoDB (`laddu_memory` collection) and only
re-generated when there is a newer chat or order than the ones they were based on.
New customers (no chats, no orders) or any error -> the default chips from Admin -> Site content.
"""

import ast
import json
import re

from fastapi import HTTPException

from core import db, now
from llm import call_llm
from prompt import build_suggestions_prompt

MAX_SUGGESTIONS = 4
PAST_CHATS = 3          # how many previous chats the AI reads
PAST_ORDERS = 3         # how many recent orders the AI reads
MAX_LINES = 40          # keep the AI input short (fast + cheap)
MAX_TOKENS = 6000       # this model "thinks" silently first; for this task it sometimes needs > 4096 tokens


def history_for_suggestions(user):
    """A short plain-text summary of the customer's recent chats and orders, for the AI to read."""
    lines = []
    chats = list(db.conversations.find({"user_id": user["_id"]}).sort("updated_at", -1).limit(PAST_CHATS))
    for n, chat in enumerate(chats, start=1):
        lines.append(f"--- Chat {n} ({'most recent' if n == 1 else 'older'}) ---")
        for m in chat["messages"]:
            if m["role"] == "user" and m.get("content"):
                lines.append(f"Customer: {m['content'][:150]}")
            elif m["role"] == "assistant" and m.get("content") and not m.get("tool_calls"):
                lines.append(f"Laddu: {m['content'][:150]}")
    orders = list(db.orders.find({"user_id": user["_id"]}).sort("created_at", -1).limit(PAST_ORDERS))
    if orders:
        lines.append("--- Recent orders ---")
        for o in orders:
            items = ", ".join(f"{i['qty']} x {i['name']}" for i in o["items"])
            lines.append(f"{o['order_id']} ({o['status']}): {items}")
    return "\n".join(lines[-MAX_LINES:])


def parse_suggestions(text):
    """Pull the list out of the AI's reply and clean it. Returns [] if it isn't usable."""
    match = re.search(r"\[.*\]", text or "", re.S)
    if not match:
        return []
    items = []
    for read in (json.loads, ast.literal_eval):   # JSON ["a", "b"] - or Python style ['a', 'b']
        try:
            items = read(match.group(0))
            break
        except (ValueError, SyntaxError):
            continue
    if not isinstance(items, list):
        return []
    clean = []
    for item in items:
        if isinstance(item, str) and item.strip() and item.strip() not in clean:
            clean.append(item.strip()[:40])
    return clean[:MAX_SUGGESTIONS]


def personal_suggestions(user, defaults):
    """4 suggestion chips for this customer, based on their previous chats and orders."""
    last_chat = db.conversations.find_one({"user_id": user["_id"]}, sort=[("updated_at", -1)])
    last_order = db.orders.find_one({"user_id": user["_id"]}, sort=[("created_at", -1)])
    if not last_chat and not last_order:
        return defaults                                   # brand-new customer
    latest = max(t for t in (last_chat and last_chat["updated_at"], last_order and last_order["created_at"]) if t)

    memo = db.laddu_memory.find_one({"_id": user["_id"]})
    if memo and memo["based_on"] >= latest:
        return memo["suggestions"]                        # nothing new since last time -> reuse (no AI call)

    brand = (db.site_content.find_one({"_id": "main"}) or {}).get("brand_name", "our sweet shop")
    try:
        reply = call_llm([{"role": "system", "content": build_suggestions_prompt(brand)},
                          {"role": "user", "content": history_for_suggestions(user)}], max_tokens=MAX_TOKENS)
    except HTTPException:
        reply = {"content": ""}
    suggestions = parse_suggestions(reply["content"])
    if len(suggestions) < 2:
        # No usable list (e.g. the AI ran out of tokens while "thinking"). Save NOTHING, so we simply
        # try again on the next visit; meanwhile show the last good chips or the defaults.
        return (memo or {}).get("suggestions") or defaults
    db.laddu_memory.update_one({"_id": user["_id"]},
                               {"$set": {"suggestions": suggestions, "based_on": latest, "updated_at": now()}},
                               upsert=True)
    return suggestions
