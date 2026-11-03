# Contributing to Rem

Thanks for helping. Bug reports, ideas, translations and pull requests are
all welcome.

## Reporting a bug

Open an [issue](https://github.com/loueylahwel/rem/issues/new/choose) with:

- your phone (model and Android version) and your PC (Windows version, or
  Linux distribution and desktop, X11 or Wayland);
- the Rem version (the release you downloaded, or the commit);
- what you did, what you expected and what happened instead;
- on Linux, the agent's log if you can:
  `journalctl --user -u sudo-agent -n 200`.

## Working on the code

| Part | Where | Run it |
|---|---|---|
| PC agent | `agent/` (Python 3.11+) | `pip install -r agent/requirements.txt`, then `python agent/agent.py` |
| Phone app | `app/` (Flutter) | `cd app && flutter run` |

The phone finds an agent on the same Wi-Fi with **Find my PC**, or use
**Enter IP manually** (an Android emulator reaches the PC at `10.0.2.2`).
The wire protocol is in [docs/protocol.md](docs/protocol.md).

Before you open a pull request:

- keep it to one change, and say in the description how you tested it;
- the agent must keep working on Windows and Linux: CI checks both
  (`.github/workflows/`), and the scripts in `scripts/` run those checks
  locally;
- match the style of the code around your change.

## Code of conduct

Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).
