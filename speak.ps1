param(
  [Parameter(Mandatory = $true, Position = 0)]
  [string]$Text,
  [string]$Voice = "af_heart",
  [double]$Speed = 1.0
)

$ErrorActionPreference = "Stop"

$body = @{ text = $Text; voice = $Voice; speed = $Speed } | ConvertTo-Json
$outFile = Join-Path $env:TEMP "kokoro_tts.wav"

Invoke-WebRequest -Method Post -ContentType "application/json" -Body $body -OutFile $outFile -Uri http://127.0.0.1:8000/tts

$player = New-Object System.Media.SoundPlayer $outFile
$player.PlaySync()
