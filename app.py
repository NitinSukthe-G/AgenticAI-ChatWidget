"""
app.py - run LADDU ON ITS OWN (the AI chat widget's server, without the website).

    python app.py                                  Laddu's API on http://localhost:8001  (+ /docs)
    python app.py chat you@example.com             chat with Laddu in the terminal
    python app.py chat you@example.com --no-memory --raw

It serves the chat API (/api/chat...), the widget files (/widget.js, /widget.css) and allows other
websites to call it (CORS), so any website can embed Laddu. It uses the same MongoDB as the shop
website (products, users + logins, carts, orders).

The full Paddu's Sweets website runs with `python main.py`, which includes Laddu too.
"""

import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pymongo import ASCENDING
from pymongo.errors import PyMongoError

from core import DB_NAME, client, db
from chat_api import router as laddu_router

PORT = int(os.getenv("LADDU_PORT", "8001"))
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("LADDU_ALLOWED_ORIGINS", "*").split(",") if o.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        client.admin.command("ping")
        db.conversations.create_index([("user_id", ASCENDING), ("active", ASCENDING)])
        print(f"Laddu connected to MongoDB (database: {DB_NAME}).")
    except PyMongoError as e:
        sys.exit(f"ERROR: could not connect to MongoDB. Check MONGODB_URI in .env "
                 f"(and Atlas Network Access). Details: {e}")
    yield


app = FastAPI(title="Laddu - AI chat widget API", lifespan=lifespan)

# CORS: lets a website on another address call Laddu from the browser.
# The login token is sent in the Authorization header (not cookies), so allowing "*" is safe for a demo.
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["*"], allow_headers=["*"])
app.include_router(laddu_router)   # /api/chat/start, /api/chat, /api/chat/suggestions, /api/admin/chats, /widget.*


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "chat":
        if len(args) < 2:
            sys.exit("Usage: python app.py chat you@example.com [--no-memory] [--raw]")
        from terminal import laddu_terminal
        client.admin.command("ping")
        laddu_terminal(args[1], memory="--no-memory" not in args, raw="--raw" in args)
    else:
        print(f"Starting Laddu at http://localhost:{PORT}  (API docs: http://localhost:{PORT}/docs)")
        uvicorn.run(app, host="127.0.0.1", port=PORT)
