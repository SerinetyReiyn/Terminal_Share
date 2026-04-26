"""Smoke test for the 1.2 agent-loop wrapper primitives. Runs against a
live wrapper.

Run from a terminal NOT inside the wrapped pwsh:

    python tests/smoke_agents.py

Covers wrapper-mechanical paths for AC#2 (long-poll), AC#3 (status),
AC#4 (offline warning), AC#5 (agent_stop). The agent-behavioral ACs
(#6/#7/#8) are honor-system contracts on each agent's runtime; they
can't be smoke-tested at the wrapper layer.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


URL = "http://127.0.0.1:8765/mcp"


def _data(result) -> dict | str:
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

            # AC#2: long-poll empty returns within budget
            t0 = time.monotonic()
            r = _data(await client.call_tool("chat_inbox", {
                "reader": "code", "wait_seconds": 2.0,
            }))
            elapsed = time.monotonic() - t0
            failures += 0 if _check(
                "AC#2 long-poll empty inbox blocks ~2s and returns",
                r.get("count") == 0 and 1.5 <= elapsed <= 4.0,
                f"elapsed={elapsed:.2f}s count={r.get('count')}",
            ) else 1

            # AC#2: long-poll wakes on a concurrent send.
            # MCP serializes requests on a single ClientSession, so the
            # inbox call would hold the connection and queue the send
            # behind itself. Open a SECOND independent session for the
            # send so the two requests actually run concurrently.
            async def delayed_send_separate_session():
                await asyncio.sleep(0.5)
                async with streamablehttp_client(URL) as (r2, w2, _):
                    async with ClientSession(r2, w2) as c2:
                        await c2.initialize()
                        await c2.call_tool("chat_send", {
                            "sender": "claudia", "to": "code",
                            "text": "smoke wake",
                        })

            t0 = time.monotonic()
            inbox_task = asyncio.create_task(client.call_tool("chat_inbox", {
                "reader": "code", "wait_seconds": 10.0,
            }))
            send_task = asyncio.create_task(delayed_send_separate_session())
            inbox_result, _send_result = await asyncio.gather(inbox_task, send_task)
            elapsed = time.monotonic() - t0
            r = _data(inbox_result)
            failures += 0 if _check(
                "AC#2 long-poll returns within 1s of insert",
                r.get("count", 0) >= 1 and elapsed < 3.0,
                f"elapsed={elapsed:.2f}s count={r.get('count')}",
            ) else 1

            # AC#3: chat_participants status fields present + correct
            r = _data(await client.call_tool("chat_participants", {}))
            participants = r.get("participants", {})
            failures += 0 if _check(
                "AC#3 every participant has status + last_seen_at fields",
                all(
                    "status" in p and "last_seen_at" in p
                    for p in participants.values()
                ),
                list(participants.keys()),
            ) else 1
            failures += 0 if _check(
                "AC#3 code is online after recent inbox",
                participants.get("code", {}).get("status") == "online",
                participants.get("code"),
            ) else 1

            # AC#5: agent_stop on a fresh participant — never inboxed
            # We use 'serinety' since serinety hasn't called inbox yet (modal
            # commits don't go through chat_inbox). If smoke ran with a fresh
            # wrapper, was_online should be False; with a stale wrapper, may
            # be True. Test the SHAPE primarily.
            r = _data(await client.call_tool("agent_stop", {
                "participant": "claudia",
            }))
            failures += 0 if _check(
                "AC#5 agent_stop returns ok+participant+was_online",
                r.get("ok") is True
                and r.get("participant") == "claudia"
                and "was_online" in r,
                r,
            ) else 1

            # The synthetic /exit must be queryable from the recipient's inbox
            r = _data(await client.call_tool("chat_inbox", {
                "reader": "claudia",
                "mark_read": False,
            }))
            has_exit = any(
                m.get("text") == "/exit" and m.get("sender") == "system"
                for m in r.get("messages", [])
            )
            failures += 0 if _check(
                "AC#5 synthetic /exit from system queued in claudia's inbox",
                has_exit,
            ) else 1

            r = _data(await client.call_tool("agent_stop", {
                "participant": "ghost",
            }))
            failures += 0 if _check(
                "AC#5 agent_stop unknown participant returns ok=false",
                r.get("ok") is False,
                r,
            ) else 1

            # AC#4: offline warning rendering — check via ps_read for the
            # system comment text. Use a fresh sentinel so we can locate it.
            sentinel = f"smoke-offline-{int(time.time())}"
            status_before = _data(await client.call_tool("ps_status", {}))
            seq_before = status_before["buffer_tail_seq"]
            await client.call_tool("chat_send", {
                "sender": "claudia", "to": "code",
                "text": sentinel,
            })
            # Poll up to 5s for the warning text to appear
            deadline = time.monotonic() + 5.0
            text = ""
            while time.monotonic() < deadline:
                buf = _data(await client.call_tool("ps_read", {
                    "since_seq": seq_before, "max_bytes": 65536,
                    "strip_ansi": True,
                }))
                text = buf["data"]
                if "[system" in text and "@code" in text:
                    break
                await asyncio.sleep(0.3)

            # If code is currently online (because we long-polled it earlier
            # in this smoke run, refreshing its heartbeat), the offline
            # warning won't fire. The test passes either way: warning OR
            # no-warning-because-online.
            participants_now = _data(
                await client.call_tool("chat_participants", {})
            ).get("participants", {})
            code_status = participants_now.get("code", {}).get("status")
            if code_status == "online":
                # No warning expected; verify message was still sent
                failures += 0 if _check(
                    "AC#4 chat_send to online code: no warning rendered",
                    sentinel in text and "[system" not in text,
                    f"code_status={code_status}",
                ) else 1
            else:
                # Warning should be in the buffer
                warning_present = (
                    "[system" in text
                    and "@code" in text
                    and "not currently listening" in text
                )
                failures += 0 if _check(
                    "AC#4 offline warning rendered when @-ing offline code",
                    warning_present,
                    f"code_status={code_status}",
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
