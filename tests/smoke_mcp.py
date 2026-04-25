"""Smoke test for a running terminal_share wrapper.

Run the wrapper in one terminal:
    python -m terminal_share

Run this in another:
    python tests/smoke_mcp.py

Prints one PASS or FAIL line per tool call. Exits 0 if all pass.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


URL = "http://127.0.0.1:8765/mcp"


def _format(result) -> str:
    if getattr(result, "structuredContent", None):
        return str(result.structuredContent)
    return str([getattr(c, "text", c) for c in result.content])


async def _run() -> int:
    failures = 0
    async with streamablehttp_client(URL) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as client:
            await client.initialize()

            r = await client.call_tool("ps_status", {})
            ok = not r.isError
            print(("PASS" if ok else "FAIL"), "ps_status:", _format(r))
            failures += 0 if ok else 1

            r = await client.call_tool("ps_send", {"text": "Get-Date"})
            ok = not r.isError
            print(("PASS" if ok else "FAIL"), "ps_send:", _format(r))
            failures += 0 if ok else 1

            await asyncio.sleep(0.6)

            r = await client.call_tool("ps_read", {"since_seq": 0})
            ok = not r.isError
            print(("PASS" if ok else "FAIL"), "ps_read:", _format(r))
            failures += 0 if ok else 1

            r = await client.call_tool("ps_signal", {"name": "ctrl_c"})
            ok = not r.isError
            print(("PASS" if ok else "FAIL"), "ps_signal:", _format(r))
            failures += 0 if ok else 1

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
