"""
terminal.py - chat with Laddu in the terminal (the learning playground).

    python main.py laddu you@example.com               normal chat, shows every AI call + 🔧 tool step
    python main.py laddu you@example.com --no-memory   history is thrown away each turn -> Laddu forgets
    python main.py laddu you@example.com --raw         also prints the raw JSON from Sarvam

Terminal chats are NOT saved to MongoDB (only chats from the website widget are).
"""

import sys

from fastapi import HTTPException

from core import db
import llm
from agent import laddu_system_prompt, run_agent


def laddu_terminal(email, memory=True, raw=False):
    """Chat with Laddu in the terminal as a website user (main.py seeds the database first)."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # so ₹ and emoji print on Windows
    llm.DEBUG_RAW = raw
    user = db.users.find_one({"email": email.strip().lower()})
    if not user:
        sys.exit(f"No user with email '{email}'. Sign up on the website first, then use that email.")

    system = {"role": "system", "content": laddu_system_prompt(user)}
    messages = [system]
    print(f"\nLaddu is ready! Chatting as {user['name']}. "
          f"Memory is {'ON' if memory else 'OFF'}. Type 'exit' to quit.\n")
    while True:
        text = input("You: ").strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break
        if not memory:
            messages = [system]  # throw away the history -> watch Laddu forget!
        before = len(messages)
        messages.append({"role": "user", "content": text})
        try:
            result = run_agent(user, messages, debug=True)
        except HTTPException as e:
            print("ERROR:", e.detail)
            del messages[before:]  # undo this turn so the history stays clean
            continue
        tools_used = f"   (tools used: {', '.join(result['steps'])})" if result["steps"] else ""
        print(f"\nLaddu: {result['reply']}\n{tools_used}\n")
