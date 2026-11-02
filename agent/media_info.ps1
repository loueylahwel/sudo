# Queries Windows SystemMediaTransportControls (Now Playing info) and prints
# JSON: title, artist, album, position/duration (seconds), playing flag.
# With -Seek <seconds> it seeks instead and prints {"seek": true|false}.
# With -Pause it pauses every app that is playing and prints {"paused":
# [their app ids]}; -Resume "id|id" plays those again (only the ones still
# paused) and prints {"resumed": [...]}. PC sound on the phone uses them
# when the phone drops (pc_audio.pause_players / resume_players).
param([double]$Seek = [double]::NaN, [switch]$Pause, [string]$Resume = "")

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

if ($Pause -or $Resume) {
    $wanted = $Resume -split '\|'
    $done = @()
    foreach ($s in $mgr.GetSessions()) {
        $status = $s.GetPlaybackInfo().PlaybackStatus   # 4 playing, 5 paused
        $id = $s.SourceAppUserModelId
        if ($Pause -and $status -eq 4) {
            if (Await ($s.TryPauseAsync()) ([bool])) { $done += $id }
        } elseif ($Resume -and $status -eq 5 -and $wanted -contains $id) {
            if (Await ($s.TryPlayAsync()) ([bool])) { $done += $id }
        }
    }
    $key = if ($Pause) { 'paused' } else { 'resumed' }
    @{ $key = @($done) } | ConvertTo-Json -Compress | Write-Output
    exit
}

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
