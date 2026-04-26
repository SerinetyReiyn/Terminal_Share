"""Manual walk-through for the 1.1.1 modal-input ACs. Run while the
wrapper is up:

    python tests/smoke_modal.py

This script prompts you to perform each scenario in your wrapper pane
(the @-chat, the Esc abort, etc.), then verifies the resulting state
via chat_history / chat_inbox. Some checks need your eyeballs; those
are explicitly marked.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


URL = "http://127.0.0.1:8765/mcp"


def _payload(result):
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


def _wait(prompt: str) -> None:
    print()
    print(prompt)
    input("Press [enter] in THIS terminal when done> ")


async def _run() -> int:
    failures = 0
    async with streamablehttp_client(URL) as (rs, ws, _):
        async with ClientSession(rs, ws) as client:
            await client.initialize()

            now = time.time()

            # AC#2 — modal entry + commit (direct)
            _wait(
                "AC#2: In your wrapper, type:\n"
                "  @code hi from serinety smoke 2\n"
                "and press Enter."
            )
            r = _payload(await client.call_tool("chat_history", {"limit": 50}))
            msgs = [m for m in r.get("messages", []) if m["ts"] >= now]
            match = next(
                (m for m in msgs
                 if m["sender"] == "serinety"
                 and m["recipient"] == "code"
                 and "smoke 2" in m["text"]),
                None,
            )
            failures += 0 if _check("AC#2 modal commit -> chat_history", match is not None, match) else 1

            # AC#3 — broadcast
            _wait(
                "AC#3: In your wrapper, type:\n"
                "  @all everybody hello smoke 3\n"
                "and press Enter."
            )
            r = _payload(await client.call_tool("chat_history", {"limit": 50}))
            msgs = [m for m in r.get("messages", []) if m["ts"] >= now]
            match = next(
                (m for m in msgs
                 if m["sender"] == "serinety"
                 and m["recipient"] == "all"
                 and "smoke 3" in m["text"]),
                None,
            )
            failures += 0 if _check("AC#3 broadcast -> chat_history (recipient=all)", match is not None, match) else 1

            # AC#4 — Esc abort
            esc_marker = time.time()
            _wait(
                "AC#4 (Esc abort): In your wrapper, type:\n"
                "  @code aborted message smoke 4\n"
                "but press Esc INSTEAD of Enter. Confirm you can type a normal\n"
                "pwsh command afterwards (e.g. Get-Date)."
            )
            r = _payload(await client.call_tool("chat_history", {"limit": 50}))
            msgs = [m for m in r.get("messages", []) if m["ts"] >= esc_marker]
            leaked = any("smoke 4" in m["text"] for m in msgs)
            failures += 0 if _check("AC#4 Esc aborted message NOT persisted", not leaked) else 1

            # AC#5 — Ctrl-C abort
            ctrlc_marker = time.time()
            _wait(
                "AC#5 (Ctrl-C abort): In your wrapper, type:\n"
                "  @code aborted message smoke 5\n"
                "and press Ctrl-C INSTEAD of Enter."
            )
            r = _payload(await client.call_tool("chat_history", {"limit": 50}))
            msgs = [m for m in r.get("messages", []) if m["ts"] >= ctrlc_marker]
            leaked = any("smoke 5" in m["text"] for m in msgs)
            failures += 0 if _check("AC#5 Ctrl-C aborted message NOT persisted", not leaked) else 1

            # AC#6 — backspace-to-empty abort, then real pwsh command works
            bs_marker = time.time()
            _wait(
                "AC#6 (backspace abort): In your wrapper, type:\n"
                "  @code\n"
                "then press backspace 5 times to clear back through the @.\n"
                "Then type:  Get-Date  and press Enter — it should run normally\n"
                "with NO ParserError from a stray @."
            )
            r = _payload(await client.call_tool("chat_history", {"limit": 50}))
            leaked = any(m["ts"] >= bs_marker for m in r.get("messages", []))
            failures += 0 if _check("AC#6 backspace abort persisted nothing", not leaked) else 1
            # The Get-Date no-parser-error check is visual; ask explicitly.
            answer = input("AC#6 visual: did Get-Date run cleanly with no ParserError? [y/N] ").strip().lower()
            failures += 0 if _check("AC#6 visual: no stray @ ParserError", answer == "y") else 1

            # AC#7 — concurrent LLM chat during modal
            print()
            print("AC#7: This one needs the chat_send call from the smoke to fire")
            print("WHILE you're mid-typing in modal. Sequence:")
            print("  1. In wrapper: type  @code partial messa  (do NOT press Enter)")
            print("  2. Switch to this terminal, press [enter] here")
            print("  3. We'll fire a chat_send from 'claudia' that should appear")
            print("     above your modal prompt without disturbing your buffer")
            print("  4. Switch back to wrapper, finish typing  ge  and press Enter")
            input("When you've typed 'partial messa' and are waiting, press [enter]> ")

            ac7_marker = time.time()
            r = _payload(await client.call_tool("chat_send", {
                "sender": "claudia",
                "to": "all",
                "text": "incoming smoke 7 mid-modal",
            }))
            print(f"  fired claudia broadcast id={r.get('id')}")
            _wait("Now finish typing 'ge' in the wrapper and press Enter.")

            r = _payload(await client.call_tool("chat_history", {"limit": 100}))
            msgs = [m for m in r.get("messages", []) if m["ts"] >= ac7_marker]
            mid_modal = any(
                m["sender"] == "claudia" and "smoke 7 mid-modal" in m["text"]
                for m in msgs
            )
            user_committed = next(
                (m for m in msgs
                 if m["sender"] == "serinety"
                 and m["recipient"] == "code"
                 and m["text"] == "partial message"),
                None,
            )
            failures += 0 if _check(
                "AC#7 concurrent claudia chat persisted",
                mid_modal,
            ) else 1
            failures += 0 if _check(
                "AC#7 user's modal message committed correctly post-interrupt",
                user_committed is not None,
                user_committed,
            ) else 1

    return failures


def main() -> int:
    try:
        failures = asyncio.run(_run())
    except Exception as e:
        print("FAIL connection:", repr(e))
        return 1
    print()
    print("OK" if failures == 0 else f"{failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
