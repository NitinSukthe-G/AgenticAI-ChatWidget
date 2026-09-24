"""
llm.py - talking to the LLM (Sarvam AI).

  - An LLM only does one thing: you send it a list of messages, it sends back ONE new message.
  - It remembers NOTHING between calls. "Memory" = we send the whole conversation every time.
  - We use plain HTTP (httpx) instead of an SDK, so you can see the real JSON going back and forth.
"""

import json
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException

from prompt import SORRY

load_dotenv(Path(__file__).parent / ".env")   # the .env file in the project folder
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
SARVAM_MODEL = os.getenv("SARVAM_MODEL", "sarvam-105b")
SARVAM_URL = "https://api.sarvam.ai/v1/chat/completions"
THINK_RE = re.compile(r"<think>.*?</think>", re.S)  # some models "think out loud" - we hide that part
DEBUG_RAW = False     # set by the terminal mode's --raw flag


def call_llm(messages, tools=None, debug=False, max_tokens=4096):
    """ONE call to the LLM: send the conversation, get back one assistant message (a dict)."""
    if not SARVAM_API_KEY:
        raise HTTPException(503, "Laddu is sleeping: SARVAM_API_KEY is missing in .env")
    payload = {
        "model": SARVAM_MODEL,
        "messages": messages,
        "temperature": 0.3,          # low = focused and consistent, high = creative and random
        "max_tokens": max_tokens,    # room for the model's hidden thinking AND the answer
        "reasoning_effort": "low",   # sarvam-105b can "think" before answering; low = faster
    }
    if tools:
        payload["tools"] = tools

    for attempt in range(2):  # retry once if the model returns an empty reply
        if debug:
            print(f"   -> sending {len(messages)} messages to {SARVAM_MODEL}"
                  + (f" with {len(tools)} tools" if tools else ""))
        try:
            res = httpx.post(SARVAM_URL, json=payload, timeout=90,
                             headers={"Authorization": f"Bearer {SARVAM_API_KEY}"})
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Could not reach Sarvam AI: {e}")
        if res.status_code != 200:
            raise HTTPException(502, f"Sarvam AI error {res.status_code}: {res.text[:300]}")

        data = res.json()
        choice = data["choices"][0]
        message = choice["message"]
        if debug:
            usage = data.get("usage", {})
            # finish_reason "length" = the model ran out of max_tokens before finishing
            print(f"   <- reply | finish_reason={choice.get('finish_reason')} | tokens: "
                  f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')}")
            if DEBUG_RAW:
                print("   RAW:", json.dumps(message, ensure_ascii=False, indent=2)[:1500])

        # Keep only the fields we need, in the standard shape, so we can send it back next time.
        clean_msg = {"role": "assistant", "content": THINK_RE.sub("", message.get("content") or "").strip()}
        if message.get("tool_calls"):
            clean_msg["tool_calls"] = message["tool_calls"]
        if clean_msg["content"] or clean_msg.get("tool_calls"):
            return clean_msg
        if debug:
            print("   (empty reply - retrying once)")
    return {"role": "assistant", "content": SORRY}
