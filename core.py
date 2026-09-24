"""
core.py - things SHARED by the website (main.py) and Laddu (chat_api.py, agent.py, tools.py, ...).

Why does this file exist?
  Laddu's tools need the database and the website's cart/order logic, and main.py needs Laddu.
  If Laddu's files imported from main.py while main.py imported them, Python would load main.py twice
  (a "circular import"). So the shared pieces live here, and imports only go one way:

      main.py (website) ──► Laddu's files (chat_api, agent, tools...) ──► core.py
          └──────────────────────────────────────────────────────────────► core.py

What's inside:
  1. Config (.env) + MongoDB connection
  2. Small helpers: now(), clean(), oid()
  3. Login checks used by routes: current_user(), require_admin()
  4. Cart + order logic used by BOTH the website and Laddu
"""

import hmac
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from fastapi import Header, HTTPException
from pymongo import MongoClient, ReturnDocument

# ---------------------------------------------------------------------------
# 1. Config + database connection
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = os.getenv("DB_NAME", "paddus_sweets")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
SESSION_DAYS = 7

if not MONGODB_URI:
    sys.exit("ERROR: MONGODB_URI is missing. Copy .env.example to .env and paste your MongoDB URL.")

# tz_aware=True: dates come back marked as UTC, so browsers convert them to the viewer's local time correctly.
client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000, tz_aware=True)
db = client[DB_NAME]


# ---------------------------------------------------------------------------
# 2. Small helpers
# ---------------------------------------------------------------------------
def now():
    return datetime.now(timezone.utc)


def clean(doc):
    """MongoDB returns ObjectId for _id; the browser needs a plain string called 'id'."""
    if doc is None:
        return None
    doc = dict(doc)
    doc["id"] = str(doc.pop("_id"))
    for key in ("user_id", "product_id"):
        if isinstance(doc.get(key), ObjectId):
            doc[key] = str(doc[key])
    for key, value in doc.items():
        if isinstance(value, datetime):
            doc[key] = value.isoformat()
    return doc


def oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(404, "Not found")


# ---------------------------------------------------------------------------
# 3. Login checks (FastAPI "dependencies")
# ---------------------------------------------------------------------------
def current_user(authorization: str = Header(None)):
    """Dependency: every protected route gets the logged-in user from the Bearer token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Please log in")
    session = db.sessions.find_one({"token": authorization[7:]})
    user = session and db.users.find_one({"_id": session["user_id"]})
    if not user:
        raise HTTPException(401, "Session expired, please log in again")
    return user


def require_admin(x_admin_password: str = Header(None)):
    if not x_admin_password or not hmac.compare_digest(x_admin_password, ADMIN_PASSWORD):
        raise HTTPException(401, "Wrong admin password")


# ---------------------------------------------------------------------------
# 4. Cart + orders - shared by the website AND Laddu, so both behave exactly the same.
#    Totals are calculated HERE on the server (never trust prices from the browser or the AI).
# ---------------------------------------------------------------------------
def cart_view(user):
    """Returns the cart with LIVE prices from the products collection."""
    cart = db.carts.find_one({"_id": user["_id"]}) or {"items": []}
    items, total = [], 0
    for it in cart["items"]:
        prod = db.products.find_one({"_id": it["product_id"]})
        if not prod:
            continue
        line = prod["price"] * it["qty"]
        total += line
        items.append({"product_id": str(prod["_id"]), "name": prod["name"], "image": prod["image"],
                      "price": prod["price"], "unit": prod["unit"], "qty": it["qty"],
                      "stock": prod["stock"], "line_total": line})
    return {"items": items, "total": total, "count": sum(i["qty"] for i in items)}


def cart_add(user, pid: ObjectId, qty: int):
    """Shared by the website AND Laddu: add qty units of a product to this user's cart."""
    if qty < 1 or not db.products.find_one({"_id": pid}):
        raise HTTPException(400, "Invalid product or quantity")
    # If the item is already in the cart, increase qty; otherwise push a new item.
    res = db.carts.update_one({"_id": user["_id"], "items.product_id": pid},
                              {"$inc": {"items.$.qty": qty}, "$set": {"updated_at": now()}})
    if res.matched_count == 0:
        db.carts.update_one({"_id": user["_id"]},
                            {"$push": {"items": {"product_id": pid, "qty": qty}},
                             "$set": {"updated_at": now()}}, upsert=True)
    return cart_view(user)


def next_order_id():
    counter = db.counters.find_one_and_update({"_id": "order"}, {"$inc": {"seq": 1}},
                                              upsert=True, return_document=ReturnDocument.AFTER)
    return f"PS-{1000 + counter['seq']}"


def create_order(user, items, name, phone, fulfilment, address="", note="", source="website"):
    """items: list of {"product_id": ObjectId, "qty": int}. Checks + reserves stock, saves the order."""
    phone_digits = re.sub(r"\D", "", phone)[-10:]
    if len(phone_digits) != 10:
        raise HTTPException(400, "Please enter a valid 10-digit phone number")
    if fulfilment not in ("pickup", "delivery"):
        raise HTTPException(400, "Choose pickup or delivery")
    if fulfilment == "delivery" and len(address.strip()) < 8:
        raise HTTPException(400, "Please enter a full delivery address")
    if not items:
        raise HTTPException(400, "Your cart is empty")

    reserved, lines, total = [], [], 0
    for it in items:
        # Atomic: only decrease stock if enough is available.
        prod = db.products.find_one_and_update(
            {"_id": it["product_id"], "stock": {"$gte": it["qty"]}},
            {"$inc": {"stock": -it["qty"]}})
        if not prod:
            for r in reserved:  # roll back what we already reserved
                db.products.update_one({"_id": r["product_id"]}, {"$inc": {"stock": r["qty"]}})
            name_ = (db.products.find_one({"_id": it["product_id"]}) or {}).get("name", "An item")
            raise HTTPException(400, f"Sorry, {name_} does not have enough stock")
        reserved.append(it)
        line = prod["price"] * it["qty"]
        total += line
        lines.append({"product_id": prod["_id"], "name": prod["name"], "price": prod["price"],
                      "unit": prod["unit"], "qty": it["qty"], "line_total": line})

    order = {
        "order_id": next_order_id(), "user_id": user["_id"], "customer_name": name.strip(),
        "email": user["email"], "phone": phone_digits, "fulfilment": fulfilment,
        "address": address.strip(), "note": note.strip(), "items": lines, "total": total,
        "payment": "Cash on delivery / pay at pickup", "status": "placed", "source": source,
        "created_at": now(),
    }
    db.orders.insert_one(order)
    return order


def order_out(order):
    order = clean(order)
    for line in order["items"]:
        line["product_id"] = str(line["product_id"])
    return order


def order_from_cart(user, name, phone, fulfilment, address="", note="", source="website"):
    """Shared by the website AND Laddu: turn this user's cart into an order, then empty the cart."""
    cart = db.carts.find_one({"_id": user["_id"]}) or {"items": []}
    order = create_order(user, cart["items"], name, phone, fulfilment, address, note, source)
    db.carts.update_one({"_id": user["_id"]}, {"$set": {"items": []}})
    return order_out(order)
