# Elevated: create a local code-signing cert, trust it, sign the helper,
# and place it in Program Files (all required for uiAccess).
$ErrorActionPreference = "Stop"

$certSubject = "CN=Sudo Local UIAccess"
$cert = Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -eq $certSubject } | Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Subject $certSubject `
        -Type CodeSigningCert -CertStoreLocation Cert:\LocalMachine\My `
        -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddYears(10)
}

# Trust anchors: root + publisher (uiAccess checks chain trust)
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "LocalMachine")
$store.Open("ReadWrite"); $store.Add($cert); $store.Close()
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("TrustedPublisher", "LocalMachine")
$store.Open("ReadWrite"); $store.Add($cert); $store.Close()

$src = "C:\Users\BL9\Documents\pc-remote\agent\dist\Sudo-UnlockHelper.exe"
$dstDir = "C:\Program Files\Sudo"
New-Item -ItemType Directory -Force -Path $dstDir | Out-Null
$dst = Join-Path $dstDir "Sudo-UnlockHelper.exe"
Copy-Item $src $dst -Force

$result = Set-AuthenticodeSignature -FilePath $dst -Certificate $cert
Write-Output "signing: $($result.Status) ($($result.StatusMessage))"

# Kill the old session-0 helper if present
Get-Process Sudo-UnlockHelper -ErrorAction SilentlyContinue | Stop-Process -Force
schtasks /delete /tn "SudoUnlockHelper" /f 2>$null | Out-Null

# Start the uiAccess helper now (as the interactive user)
Start-Process $dst
Write-Output "done"
