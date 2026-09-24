"""
tools.py - the TOOLS: plain Python functions Laddu can ask us to run.

  - We describe each tool to the LLM with a JSON schema (tool_schemas()).
  - The LLM never runs code itself; it replies "please call search_products with {query: 'ladoo'}".
  - WE run the function (run_tool) and send the result back. That's what makes Laddu an agent.
  - `user` is always passed by OUR code (from the login), never by the AI, so Laddu can only
    ever touch the logged-in customer's own cart and orders.
"""

import json
import re

from fastapi import HTTPException
from pymongo import ASCENDING

from core import cart_add, cart_view, db, now, order_from_cart
from prompt import TOOL_DESCRIPTIONS


# ----- Fuzzy product search (customers don't spell like the database) -----------------------
def loose_regex(word):
    """Spelling-forgiving pattern: 'mothichur' matches 'Motichoor', 'laddu' matches 'Ladoo'.
    Vowels are interchangeable, 'h' is optional, doubled letters don't matter, v/w are the same."""
    word = re.sub(r"[^a-z]", "", word.lower())
    word = re.sub(r"(.)\1+", r"\1", word)             # laddoo -> lado
    if len(word) > 4 and word.endswith("s"):
        word = word[:-1]                               # ladoos -> lado
    pattern = ""
    for vowels, other in re.findall(r"([aeiouy]+)|([^aeiouy])", word):
        if vowels:
            pattern += "[aeiouy]+"
        elif other == "h":
            pattern += "h?"
        elif other in "vw":
            pattern += "[vw]+"
        else:
            pattern += other + "+"
    return pattern


def product_brief(prod):
    return {"name": prod["name"], "category": prod["category"], "price": prod["price"], "unit": prod["unit"],
            "stock": prod["stock"], "bestseller": prod.get("is_bestseller", False)}


def find_products(query="", category=""):
    words = [w for w in query.split() if len(re.sub(r"[^a-zA-Z]", "", w)) >= 2]
    base = {"category": category} if category else {}
    # Name: the word may be the start of a name word ("kaj" -> Kaju). Description: whole words only,
    # otherwise "laddu" would match "loaded" in some unrelated description.
    conditions = [{"$or": [{"name": {"$regex": rf"\b{loose_regex(w)}", "$options": "i"}},
                           {"description": {"$regex": rf"\b{loose_regex(w)}s?\b", "$options": "i"}}]}
                  for w in words]
    results = list(db.products.find({**base, "$and": conditions} if conditions else base).sort("name", ASCENDING))
    if not results and len(conditions) > 1:  # no product matches ALL words -> accept ANY word
        results = list(db.products.find({**base, "$or": conditions}).sort("name", ASCENDING))
    return results


def find_one_product(name):
    """Exact name first, then fuzzy. Raises a helpful error if it's missing or ambiguous."""
    exact = db.products.find_one({"name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}})
    if exact:
        return exact
    matches = find_products(name)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise HTTPException(404, f"No product called '{name}'. Use search_products to see what we have.")
    raise HTTPException(400, "Several products match - ask the customer which one: "
                        + ", ".join(m["name"] for m in matches))


# ----- The 8 tools ------------------------------------------------------------------------------
def tool_search_products(user, query="", category=""):
    results = find_products(query, category)
    # "no" = the option number Laddu shows, so the customer can simply reply "5" to pick one.
    return {"count": len(results),
            "products": [{"no": i, **product_brief(x)} for i, x in enumerate(results, start=1)]}


def tool_get_product_details(user, name):
    prod = find_one_product(name)
    return {**product_brief(prod), "description": prod.get("description", "")}


def tool_get_shop_info(user):
    shop = db.shop_info.find_one({"_id": "main"}, {"_id": 0, "map_embed": 0}) or {}
    content = db.site_content.find_one({"_id": "main"}) or {}
    return {**shop, "about": content.get("about_text", ""),
            "categories": [c["name"] for c in db.categories.find().sort("order", ASCENDING)],
            "payment": "Cash on delivery / pay at pickup (no online payment)"}


def tool_view_cart(user):
    cart = cart_view(user)
    return {"items": [{"name": i["name"], "qty": i["qty"], "unit": i["unit"], "price": i["price"],
                       "line_total": i["line_total"]} for i in cart["items"]], "total": cart["total"]}


def tool_add_to_cart(user, product_name, qty=1):
    prod = find_one_product(product_name)
    qty = int(qty)
    if qty > prod["stock"]:
        raise HTTPException(400, f"Only {prod['stock']} x {prod['unit']} of {prod['name']} left in stock")
    cart_add(user, prod["_id"], qty)
    return {"added": f"{qty} x {prod['unit']} {prod['name']}", "cart": tool_view_cart(user)}


def tool_place_order(user, name, phone, fulfilment, address="", note="", confirmed=False):
    # GUARDRAIL in code (not just in the prompt): no order without the customer's explicit yes.
    if confirmed not in (True, "true", "True", "yes"):
        return {"error": "NOT placed. First show the customer the order summary (items, total, pickup/delivery, "
                         "phone) and ask them to confirm. Only after they say yes, call again with confirmed=true."}
    order = order_from_cart(user, name, phone, fulfilment, address, note, source="laddu")
    return {"placed": True, "order_id": order["order_id"], "total": order["total"], "status": order["status"],
            "items": [f"{i['qty']} x {i['unit']} {i['name']}" for i in order["items"]],
            "payment": order["payment"]}


def tool_get_my_orders(user):
    orders = db.orders.find({"user_id": user["_id"]}).sort("created_at", -1).limit(5)
    return {"orders": [{"order_id": o["order_id"], "status": o["status"], "total": o["total"],
                        "fulfilment": o["fulfilment"], "placed_at": o["created_at"].astimezone().strftime("%d %b %Y %I:%M %p"),
                        "items": [f"{i['qty']} x {i['name']}" for i in o["items"]]} for o in orders]}


def tool_request_callback(user, phone, reason):
    digits = re.sub(r"\D", "", phone)[-10:]
    if len(digits) != 10:
        raise HTTPException(400, "Please ask for a valid 10-digit phone number")
    db.contact_messages.insert_one({"name": user["name"], "email": user["email"], "phone": digits,
                                    "message": f"[Callback request via Laddu] {reason}", "source": "laddu",
                                    "user_id": user["_id"], "created_at": now()})
    return {"saved": True, "note": "The shop team will call the customer back soon."}


# ----- Tool registry: how the AI learns about the tools ----------------------------------------
def _prop(type_, description, **extra):
    return {"type": type_, "description": description, **extra}


# name -> (python function, description for the LLM, parameters, required parameters)
LADDU_TOOLS = {
    "search_products": (tool_search_products, TOOL_DESCRIPTIONS["search_products"],
        {"query": _prop("string", "1-3 keywords, e.g. 'ladoo', 'kaju', 'sugar'. Empty = everything."),
         "category": _prop("string", "Optional filter", enum=["sweets", "namkeen", "hampers"])}, []),
    "get_product_details": (tool_get_product_details, TOOL_DESCRIPTIONS["get_product_details"],
        {"name": _prop("string", "Product name, e.g. 'Kaju Katli'")}, ["name"]),
    "get_shop_info": (tool_get_shop_info, TOOL_DESCRIPTIONS["get_shop_info"], {}, []),
    "view_cart": (tool_view_cart, TOOL_DESCRIPTIONS["view_cart"], {}, []),
    "add_to_cart": (tool_add_to_cart, TOOL_DESCRIPTIONS["add_to_cart"],
        {"product_name": _prop("string", "Exact product name from search_products"),
         "qty": _prop("integer", "Number of units (e.g. 500g packs or boxes). 1 kg = 2 packs of 500g.")},
        ["product_name", "qty"]),
    "place_order": (tool_place_order, TOOL_DESCRIPTIONS["place_order"],
        {"name": _prop("string", "Customer name"),
         "phone": _prop("string", "10-digit mobile number"),
         "fulfilment": _prop("string", "pickup or delivery", enum=["pickup", "delivery"]),
         "address": _prop("string", "Full address (required for delivery)"),
         "note": _prop("string", "Optional note, e.g. gift wrap"),
         "confirmed": _prop("boolean", "true only after the customer confirmed the summary")},
        ["name", "phone", "fulfilment", "confirmed"]),
    "get_my_orders": (tool_get_my_orders, TOOL_DESCRIPTIONS["get_my_orders"], {}, []),
    "request_callback": (tool_request_callback, TOOL_DESCRIPTIONS["request_callback"],
        {"phone": _prop("string", "10-digit mobile number"), "reason": _prop("string", "What they need")},
        ["phone", "reason"]),
}
CART_TOOLS = {"add_to_cart", "place_order"}   # if one of these succeeds, the website cart must refresh


def tool_schemas():
    """Turn LADDU_TOOLS into the JSON format the LLM understands."""
    return [{"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required}}}
            for name, (fn, desc, props, required) in LADDU_TOOLS.items()]


def run_tool(user, name, raw_args):
    """Run one tool the LLM asked for. Errors are returned (not raised) so Laddu can explain them."""
    if name not in LADDU_TOOLS:
        return {}, {"error": f"Unknown tool '{name}'"}
    fn, _, props, _ = LADDU_TOOLS[name]
    try:
        args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else dict(raw_args or {})
    except json.JSONDecodeError:
        return {}, {"error": "Tool arguments were not valid JSON"}
    args = {k: v for k, v in args.items() if k in props}  # ignore made-up arguments
    try:
        return args, fn(user, **args)
    except HTTPException as e:
        return args, {"error": e.detail}
    except Exception as e:  # a bug in a tool must not crash the chat
        return args, {"error": f"{type(e).__name__}: {e}"}
