"""Smoke test for the chat layer + ps_send atomicity. Run while the wrapper
is running:

    python tests/smoke_chat.py

Prints PASS/FAIL per assertion, exits 0 if all pass.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


_PROVENANCE_RE = re.compile(r"\[(Claudia|Claude Code) \d{2}:\d{2}:\d{2}\] running:")


URL = "http://127.0.0.1:8765/mcp"


def _data(result) -> dict | str:
    """Pull the structured payload out of a CallToolResult."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    txt = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return txt


def _check(label: str, ok: bool, detail: object = "") -> bool:
    print(("PASS" if ok else "FAIL"), label, "—", detail if detail else "")
    return ok


async def _run() -> int:
    failures = 0
    async with streamablehttp_client(URL) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()

            # 1. chat_participants
            r = _data(await client.call_tool("chat_participants", {}))
            keys = set(r.get("participants", {}).keys())
            failures += 0 if _check(
                "chat_participants returns serinety/claudia/code",
                {"serinety", "claudia", "code"} <= keys,
                keys,
            ) else 1
            failures += 0 if _check(
                "chat_participants broadcast_keyword='all'",
                r.get("broadcast_keyword") == "all",
            ) else 1

            # 2. chat_send direct
            r = _data(await client.call_tool("chat_send", {
                "sender": "claudia", "to": "code", "text": "hi from claudia (smoke)",
            }))
            failures += 0 if _check("chat_send direct ok=true", r.get("ok") is True, r) else 1
            direct_id = r.get("id")

            # 3. chat_send broadcast
            r = _data(await client.call_tool("chat_send", {
                "sender": "code", "to": "all", "text": "team check (smoke)",
            }))
            failures += 0 if _check("chat_send broadcast ok=true", r.get("ok") is True, r) else 1

            # 4. chat_inbox retrieves and marks read
            r = _data(await client.call_tool("chat_inbox", {"reader": "code"}))
            texts = [m["text"] for m in r.get("messages", [])]
            failures += 0 if _check(
                "chat_inbox(code) sees direct + broadcast",
                "hi from claudia (smoke)" in texts and "team check (smoke)" in texts,
                texts,
            ) else 1
            r2 = _data(await client.call_tool("chat_inbox", {"reader": "code"}))
            failures += 0 if _check(
                "chat_inbox(code) second call empty (marked read)",
                r2.get("count") == 0,
                r2,
            ) else 1

            # 5. chat_history returns regardless of read state
            r = _data(await client.call_tool("chat_history", {"limit": 50}))
            history_texts = [m["text"] for m in r.get("messages", [])]
            failures += 0 if _check(
                "chat_history includes already-read messages",
                "hi from claudia (smoke)" in history_texts,
                len(history_texts),
            ) else 1

            # 6. Validation: unknown sender
            r = _data(await client.call_tool("chat_send", {
                "sender": "bob", "to": "all", "text": "shouldn't land",
            }))
            failures += 0 if _check(
                "chat_send unknown sender rejected",
                r.get("ok") is False and r.get("error") == "unknown_sender",
                r,
            ) else 1

            # 7. Validation: unknown to
            r = _data(await client.call_tool("chat_send", {
                "sender": "claudia", "to": "ghost", "text": "shouldn't land",
            }))
            failures += 0 if _check(
                "chat_send unknown to rejected",
                r.get("ok") is False and r.get("error") == "unknown_recipient",
                r,
            ) else 1

            # 8. Validation: sender='all' rejected
            r = _data(await client.call_tool("chat_send", {
                "sender": "all", "to": "code", "text": "shouldn't land",
            }))
            failures += 0 if _check(
                "chat_send sender='all' rejected",
                r.get("ok") is False and r.get("error") == "unknown_sender",
                r,
            ) else 1

            # 9. Concurrent ps_send atomicity (AC#5)
            #
            # We anchor the parse on the unique "running:" provenance pattern
            # (chat_send doesn't produce that), and on unique command tokens.
            # Drain any pending PSReadLine echo from earlier chat_sends before
            # capturing seq_before so we don't pick up stale "[Claude Code"
            # text from prior broadcasts.
            await asyncio.sleep(0.3)
            status_before = _data(await client.call_tool("ps_status", {}))
            seq_before = status_before["buffer_tail_seq"]

            await asyncio.gather(
                client.call_tool("ps_send", {"text": "echo A_FROM_CLAUDIA", "sender": "claudia"}),
                client.call_tool("ps_send", {"text": "echo B_FROM_CODE", "sender": "code"}),
            )
            await asyncio.sleep(1.0)

            buf = _data(await client.call_tool("ps_read", {
                "since_seq": seq_before, "max_bytes": 131072, "strip_ansi": True,
            }))
            text = buf["data"]

            prov_senders = set(_PROVENANCE_RE.findall(text))
            both_outputs = "A_FROM_CLAUDIA" in text and "B_FROM_CODE" in text
            failures += 0 if _check(
                "concurrent ps_send: both provenance + both outputs present",
                {"Claudia", "Claude Code"} <= prov_senders and both_outputs,
                f"provenance_senders={prov_senders} "
                f"A_present={'A_FROM_CLAUDIA' in text} "
                f"B_present={'B_FROM_CODE' in text}",
            ) else 1

            # Verify pair ordering: each sender's provenance precedes its
            # command token in the buffer (no command-before-provenance).
            order_ok = True
            for sender, token in (("Claudia", "A_FROM_CLAUDIA"), ("Claude Code", "B_FROM_CODE")):
                prov_pos = text.find(f"[{sender}")
                # Walk forward to the matching "running:" within ~120 chars.
                while prov_pos != -1:
                    nearby = text[prov_pos:prov_pos + 120]
                    if "running:" in nearby:
                        break
                    prov_pos = text.find(f"[{sender}", prov_pos + 1)
                cmd_pos = text.find(token)
                if prov_pos < 0 or cmd_pos < 0 or prov_pos >= cmd_pos:
                    order_ok = False
                    break
            failures += 0 if _check(
                "each provenance precedes its own command",
                order_ok,
            ) else 1

    return failures


def main() -> int:
    try:
        failures = asyncio.run(_run())
    except Exception as e:
        print("FAIL connection:", repr(e))
        return 1
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
