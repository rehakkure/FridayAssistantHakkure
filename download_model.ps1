$ErrorActionPreference = 'Stop'
$model = 'vosk-model-small-ru-0.22'
$dir = 'C:\Users\hakku\friday_assistant\models'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$zip = Join-Path $dir ($model + '.zip')
$urls = @(
  ('https://alphacephei.com/vosk/models/' + $model + '.zip'),
  ('https://github.com/alphacep/vosk-api/releases/download/v0.3.45/' + $model + '.zip')
)
$ok = $false
foreach ($u in $urls) {
  try {
    Write-Host "Downloading $u"
    Invoke-WebRequest -Uri $u -OutFile $zip
    $ok = $true
    break
  }
  catch {
    Write-Host "Failed: $($_.Exception.Message)"
  }
}
if (-not $ok) { throw 'download failed' }
Write-Host 'Extracting...'
Expand-Archive -Path $zip -DestinationPath $dir -Force
Remove-Item $zip -Force
Write-Host 'Done'
