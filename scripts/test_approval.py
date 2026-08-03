"""Test the approval flow against the dev agent.

1. Pair with no token -> expect {type:"approval.pending"} (dialog pops on PC)
2. Wait for the user/script to click Allow -> expect paired + token
3. Pair again with the token -> instant paired, no prompt
"""

import asyncio
import json
import sys
from pathlib import Path

import websockets

CFG = json.loads(
    (Path.home() / "AppData/Roaming/Sudo/config.json").read_text())


async def pair(ws, **fields):
    await ws.send(json.dumps({"type": "pair", **fields}))
    return json.loads(await ws.recv())


async def main():
    # fresh token read each round
    async with websockets.connect(CFG["relay"]) as ws:
        r = await pair(ws)
        print("first pair ->", r)
        if r.get("type") != "approval.pending":
            print("UNEXPECTED: no approval.pending (token already approved?)")
            return
        # second message arrives after the human/script decision
        r2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        print("after decision ->", r2)
        token = r2.get("token")
        if not token:
            print("no token issued (denied?)")
            return
    async with websockets.connect(CFG["relay"]) as ws:
        r3 = await pair(ws, token=token)
        print("token pair ->", r3)
        assert r3.get("type") == "paired"
        print("APPROVAL FLOW OK")


if __name__ == "__main__":
    asyncio.run(main())
