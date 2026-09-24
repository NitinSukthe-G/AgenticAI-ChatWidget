"""
agent.py - THE AGENT LOOP, the heart of agentic AI.

    ask the LLM ──► it asks for tools? ──yes──► run them, send results back ──┐
        ▲                  │ no                                                │
        │                  ▼                                                   │
        │           final answer                                               │
        └──────────────────────────────────────────────────────────────────────┘
                 (at most MAX_AGENT_STEPS rounds per customer message)
"""

import json
from datetime import datetime

from core import db
from llm import call_llm
from prompt import SORRY, build_system_prompt
from tools import CART_TOOLS, run_tool, tool_schemas

MAX_AGENT_STEPS = 6   # safety limit: the agent may call tools at most this many rounds per message


def laddu_system_prompt(user):
    """Fill Laddu's rules (prompt.py) with live values: shop name from MongoDB, customer from the login."""
    content = db.site_content.find_one({"_id": "main"}) or {}
    brand = content.get("brand_name", "our sweet shop")
    today = datetime.now().strftime("%A, %d %B %Y, %I:%M %p")
    return build_system_prompt(brand, user["name"], user["email"], today)


def run_agent(user, messages, debug=False):
    """THE AGENT LOOP. `messages` is the conversation so far (it is extended in place)."""
    steps, cart_changed, schemas = [], False, tool_schemas()
    for _ in range(MAX_AGENT_STEPS):
        reply = call_llm(messages, tools=schemas, debug=debug)
        messages.append(reply)
        if not reply.get("tool_calls"):          # plain text = final answer, we're done
            return {"reply": reply["content"], "steps": steps, "cart_changed": cart_changed}
        for call in reply["tool_calls"]:         # the LLM asked us to run tools
            name = call["function"]["name"]
            args, result = run_tool(user, name, call["function"].get("arguments"))
            steps.append(name)
            if name in CART_TOOLS and "error" not in result:
                cart_changed = True
            if debug:
                print(f"   🔧 {name}({json.dumps(args, ensure_ascii=False)})"
                      f" -> {json.dumps(result, ensure_ascii=False, default=str)[:160]}")
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
    messages.append({"role": "assistant", "content": SORRY})  # hit the safety limit
    return {"reply": SORRY, "steps": steps, "cart_changed": cart_changed}
