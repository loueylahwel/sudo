# Elevated: kill ALL helper instances, install the fresh build, sign, start.
$ErrorActionPreference = "Continue"

Get-Process Sudo-UnlockHelper -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 1

$cert = Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -eq "CN=Sudo Local UIAccess" } | Select-Object -First 1
$src = "C:\Users\BL9\Documents\pc-remote\agent\dist\Sudo-UnlockHelper.exe"
$dst = "C:\Program Files\Sudo\Sudo-UnlockHelper.exe"
Copy-Item $src $dst -Force
$result = Set-AuthenticodeSignature -FilePath $dst -Certificate $cert
Write-Output "signing: $($result.Status)"

Start-Process $dst
Start-Sleep 2
Get-Process Sudo-UnlockHelper | Select-Object Id, StartTime
