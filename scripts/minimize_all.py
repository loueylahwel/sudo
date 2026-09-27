"""Minimize all PC windows via the agent (for a clean screenshot)."""

import asyncio
import json
from pathlib import Path

import websockets

CFG = json.loads(
    (Path.home() / "AppData/Roaming/Sudo/config.json").read_text())

CMD = ('powershell -NoProfile -Command '
       '"(New-Object -ComObject Shell.Application).MinimizeAll()"')


async def main():
    async with websockets.connect(CFG["relay"]) as ws:
        await ws.send(json.dumps({"type": "pair"}))
        hello = json.loads(await ws.recv())
        assert hello.get("type") == "paired", hello
        await ws.send(json.dumps({"id": "1", "cmd": "sys.exec", "command": CMD}))
        while True:
            raw = await ws.recv()
            if isinstance(raw, bytes):
                continue  # skip streamed screen frames
            msg = json.loads(raw)
            if msg.get("id") == "1":
                print(msg)
                break


if __name__ == "__main__":
    asyncio.run(main())
