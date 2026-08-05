# Queries Windows SystemMediaTransportControls (Now Playing info) and prints
# JSON: title, artist, album, position/duration (seconds), playing flag.
# With -Seek <seconds> it seeks instead and prints {"seek": true|false}.
param([double]$Seek = [double]::NaN)

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($op, $resultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
    $netTask = $asTask.Invoke($null, @($op))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

$mgrType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$propsType = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties]

$mgr = Await ($mgrType::RequestAsync()) $mgrType
$session = $mgr.GetCurrentSession()
if ($null -eq $session) {
    '{"playing":false}' | Write-Output
    exit
}

if (-not [double]::IsNaN($Seek)) {
    $ticks = [long]($Seek * 10000000)
    $ok = Await ($session.TryChangePlaybackPositionAsync($ticks)) ([bool])
    @{ seek = [bool]$ok } | ConvertTo-Json -Compress | Write-Output
    exit
}

$props = Await ($session.TryGetMediaPropertiesAsync()) $propsType
$timeline = $session.GetTimelineProperties()
$playback = $session.GetPlaybackInfo()

@{
    title      = "$($props.Title)"
    artist     = "$($props.Artist)"
    album      = "$($props.AlbumTitle)"
    position_s = [math]::Round($timeline.Position.TotalSeconds, 1)
    duration_s = [math]::Round($timeline.EndTime.TotalSeconds, 1)
    playing    = ($playback.PlaybackStatus -eq 4)
} | ConvertTo-Json -Compress | Write-Output
