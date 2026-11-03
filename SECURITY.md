# Security

Rem gives a paired phone full control of a PC: its screen, files and a
terminal. Security problems matter a lot here, so thank you for reporting
them carefully.

## Reporting a vulnerability

Please **don't open a public issue**. Use GitHub's private reporting
instead: the **Security** tab of this repository → **Report a
vulnerability**. Include what an attacker can do, how to reproduce it, and
the Rem version.

You'll get an answer within a week. Fixes go out as a new release, and the
report is credited unless you'd rather not.

## How Rem protects the PC

- A phone the PC hasn't seen before can connect only after someone clicks
  **Allow** on the PC (unless `"require_approval": false` is set in the
  agent's `config.json`).
- After pairing, the phone connects over TLS and pins the PC's own
  certificate.
- Away from home, traffic goes through the router or a Cloudflare tunnel,
  still encrypted with the PC's certificate. `"remote_access": false`
  turns access from outside off.

## Supported versions

Only the [latest release](https://github.com/loueylahwel/rem/releases/latest)
gets security fixes.
