$ErrorActionPreference = "Continue"
$log = "C:\ProgramData\sudo-cleanup.log"
"start $(Get-Date -Format o)" | Out-File $log
Get-Process Sudo-UnlockHelper, UIA-Probe -ErrorAction SilentlyContinue | Stop-Process -Force
schtasks /delete /tn "SudoUnlockHelper" /f 2>&1 | Out-File $log -Append
try {
    Remove-Item -Recurse -Force "C:\Program Files\Sudo" -ErrorAction Stop
    "deleted dir" | Out-File $log -Append
} catch {
    "delete error: $_" | Out-File $log -Append
}
if (Test-Path "C:\Program Files\Sudo") { "STILL THERE" | Out-File $log -Append } else { "GONE" | Out-File $log -Append }
