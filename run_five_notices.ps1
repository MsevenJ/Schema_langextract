$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectDir ".venv\Scripts\python.exe"
$inputDir = Join-Path $projectDir "notices"
$outputDir = Join-Path $projectDir "output"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Project virtual-environment Python was not found: $pythonPath"
}

$noticeFiles = @(
    Get-ChildItem -LiteralPath $inputDir -File -Filter "notice*"
)
if ($noticeFiles.Count -ne 5) {
    throw "The notices directory must contain exactly 5 notice* files; found $($noticeFiles.Count)."
}

Set-Location -LiteralPath $projectDir
& $pythonPath `
    "traffic_notice_batch.py" `
    "--input-dir" $inputDir `
    "--pattern" "notice*" `
    "--output-dir" $outputDir `
    "--retries" "1"

exit $LASTEXITCODE
