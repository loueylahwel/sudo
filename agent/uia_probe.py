"""Continuous UIA probe: poll the UIA tree for 30s for the lock screen
password field (IsPassword=1 edit). When found: SetFocus and try
ValuePattern.SetValue / LegacyIAccessible.SetValue with '1234'."""

import time
import traceback

import comtypes
import comtypes.client

LOG = "C:/ProgramData/Sudo/uia_probe.log"
DESCENDANTS = 4
EDIT_TYPE = 50004
VALUE_PATTERN = 10002
LEGACY_PATTERN = 10018


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def main():
    log("--- continuous probe start ---")
    comtypes.CoInitialize()
    mod = comtypes.client.GetModule("UIAutomationCore.dll")
    uia = comtypes.client.CreateObject(
        "{FF48DBA4-60EF-4201-AA87-54103EEF594E}",
        interface=mod.IUIAutomation,
    )
    cond = uia.CreatePropertyCondition(30003, EDIT_TYPE)
    deadline = time.time() + 30
    seen = set()
    while time.time() < deadline:
        try:
            root = uia.GetRootElement()
            edits = root.FindAll(DESCENDANTS, cond)
            for i in range(edits.Length):
                el = edits.GetElement(i)
                try:
                    name = el.CurrentName
                    is_pwd = el.CurrentIsPassword
                except Exception:
                    continue
                key = f"{name}|{is_pwd}"
                if is_pwd and key not in seen:
                    seen.add(key)
                    log(f"PASSWORD EDIT FOUND: {name!r}")
                    try:
                        el.SetFocus()
                        log("  SetFocus ok")
                    except Exception:
                        log("  SetFocus failed")
                    try:
                        pat = el.GetCurrentPattern(VALUE_PATTERN)
                        pat.SetValue("1234")
                        log("  ValuePattern.SetValue OK")
                    except Exception:
                        log("  ValuePattern.SetValue failed")
                    try:
                        pat = el.GetCurrentPattern(LEGACY_PATTERN)
                        pat.SetValue("1234")
                        log("  LegacyIAccessible.SetValue OK")
                    except Exception:
                        log("  LegacyIAccessible.SetValue failed")
        except Exception:
            log("poll error:\n" + traceback.format_exc())
        time.sleep(1)
    log(f"--- probe end; password edits seen: {len(seen)} ---")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
