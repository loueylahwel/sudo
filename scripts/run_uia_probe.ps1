$ErrorActionPreference = "Continue"
$cert = Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -eq "CN=Sudo Local UIAccess" } | Select-Object -First 1
$src = "C:\Users\BL9\Documents\pc-remote\agent\dist\UIA-Probe.exe"
$dst = "C:\Program Files\Sudo\UIA-Probe.exe"

# build happens outside this script; here: sign, lock, run probe
Copy-Item $src $dst -Force
$result = Set-AuthenticodeSignature -FilePath $dst -Certificate $cert
Write-Output "signing: $($result.Status)"
rundll32.exe user32.dll,LockWorkStation
Write-Host ">>> PC is locking. Press ONE key to show the PIN field, then HANDS OFF for 30 seconds <<<"
Start-Process $dst -Wait
Write-Output "probe finished"
