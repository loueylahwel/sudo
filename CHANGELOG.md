# Changelog

Downloads for every version: [releases](https://github.com/loueylahwel/rem/releases).

## v2.4.1 (2026-11-07)

Security fixes, reported privately by
[Corey Klass](https://coreyklass.com). Thank you!

- The optional outside relay (`relay/`, and `"relay"` or `RELAY_URL`
  pointing away from the PC) is removed: phones that went through it
  reached the PC with no Allow prompt and no token. A PC still set up that
  way now ignores it, says so in its log, and uses its own built-in relay
  with the Allow prompt. Default setups were never affected.
- The app sends the PC's password (Unlock) only over the encrypted
  connection that checks the PC's own certificate, never over plain Wi-Fi.
- Right after the first pairing, the phone switches to that encrypted
  connection at once, instead of at the next reconnect.
- Two phones asking to pair at the same moment no longer freeze the PC's
  Allow/Deny window.

## v2.4.0 (2026-11-03)

- Sudo is now called **Rem**, with a new logo. The files are `rem.apk`,
  `Rem-Service.exe` and `Rem-Agent.exe`. Updating keeps your pairings:
  the app installs over the old one, and the PC keeps its settings.
- A PC set to pair without the Allow/Deny prompt (`"require_approval":
  false`) no longer leaves the phone stuck on "Disconnected" right after
  pairing.
- New README, screenshots, contributing and security guides.

## v2.3.2 (2026-11-02)

- Windows: when the phone playing the PC's sound drops off, the PC pauses
  what was playing, and plays it again if the phone comes back within 20
  seconds. Linux already did this.

## v2.3.1 (2026-11-02)

- PC sound on your phone works with Windows PCs (Windows 10 2004 or newer).

## v2.3.0 (2026-11-02)

- Smoother screen: up to 60 fps, following the Wi-Fi's speed; full screen;
  zooming in shows the PC's own pixels.
- Away from home: reach the PC from any network, no account needed.
- Live typing in the PC's keyboard layout; window lock; switching between
  the PC's screens; the Now Playing card follows the PC live.
- PC sound on your phone (Linux PCs).
- Linux PCs: installer for the big distributions, X11 and Wayland.
- The PC says why the screen is black instead of sending a black picture.
- The app shows its real version. The Phone screen (phone as a second
  monitor) is gone.

## v2.2.1 (2026-10-31)

- Fixes the v2.2.0 app closing right at launch.

## v2.2.0 (2026-08-05) and earlier

See the [release notes](https://github.com/loueylahwel/rem/releases).
