# cleanup-old.ps1 -- delete redundant / superseded files that have not been touched for N days.
# ASCII only (PowerShell 5.1 reads ANSI when there is no BOM).
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File cleanup-old.ps1            # real run
#   powershell -NoProfile -ExecutionPolicy Bypass -File cleanup-old.ps1 -WhatIf    # dry run
#   ... -Days 30                                                                   # custom age
param(
    [int]$Days = 15,
    [switch]$WhatIf,
    [switch]$Quiet
)

$ErrorActionPreference = 'SilentlyContinue'
$Root    = 'D:\dsh 大肥鱼'
$LogFile = Join-Path $PSScriptRoot 'cleanup-old.log'
$cutoff  = (Get-Date).AddDays(-$Days)

# Paths that must never be touched, wherever they appear under $Root.
$protected = @(
    '\memory\', '\secrets\', '\plugins\', '\repos\', '\scores\',
    '\voice\ref\', '\voice\models\', '\voice\tts\venv\', '\voice\tts\Lib\',
    '\slides\yunbianduan\out\', '\wallpaper\live\out\', '\.git\', '\node_modules\'
)

# Directories whose contents are disposable (cache / generated media / scratch output).
$cacheDirs = @(
    'cache', 'captures', 'voice\cache', 'gen\cache', 'voice\watch',
    'qq-bridge\media', 'tools\__pycache__'
)

# File patterns that are always considered redundant copies.
$junkPatterns = @('*.bak-*', '*.orig.*', '*.tmp', '*.bak', '*~', '*.old', '*.rej', '*.orig')

# Render / preview scratch directories produced by QA steps.
$scratchDirRegex = '^(prev|prev2|prev3|pdfprev\d*|pdfcheck[\w-]*|prev-[\w-]+)$'

function Is-Protected([string]$path) {
    foreach ($p in $protected) { if ($path.IndexOf($p, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true } }
    return $false
}

# Age = the newest of LastWriteTime / LastAccessTime, so a file that was read recently survives.
function Age-Days($item) {
    $t = $item.LastWriteTime
    if ($item.LastAccessTime -gt $t) { $t = $item.LastAccessTime }
    return ((Get-Date) - $t).TotalDays
}

$deleted = New-Object System.Collections.ArrayList
$freed   = 0L
$skipped = 0

function Try-Delete($item, [string]$reason) {
    $age = Age-Days $item
    if ($age -lt $Days) { $script:skipped++; return }
    if (Is-Protected $item.FullName) { $script:skipped++; return }
    $len = 0
    if (-not $item.PSIsContainer) { $len = $item.Length }
    if ($WhatIf) {
        [void]$script:deleted.Add([pscustomobject]@{ Path = $item.FullName; MB = [math]::Round($len / 1MB, 2); Age = [math]::Round($age, 1); Reason = $reason })
        $script:freed += $len
        return
    }
    Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $item.FullName)) {
        [void]$script:deleted.Add([pscustomobject]@{ Path = $item.FullName; MB = [math]::Round($len / 1MB, 2); Age = [math]::Round($age, 1); Reason = $reason })
        $script:freed += $len
    } else {
        $script:skipped++   # locked by a running process
    }
}

# 1) redundant copies anywhere in the tree
foreach ($pat in $junkPatterns) {
    Get-ChildItem -LiteralPath $Root -Recurse -Depth 4 -Force -File -Filter $pat -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName.IndexOf('\node_modules\', [StringComparison]::OrdinalIgnoreCase) -lt 0 } |
        ForEach-Object { Try-Delete $_ 'backup-copy' }
}

# 2) cache / generated-media / scratch output directories
foreach ($rel in $cacheDirs) {
    $dir = Join-Path $Root $rel
    if (-not (Test-Path -LiteralPath $dir)) { continue }
    Get-ChildItem -LiteralPath $dir -Recurse -Force -File -ErrorAction SilentlyContinue |
        ForEach-Object { Try-Delete $_ 'cache' }
}

# 3) render / preview scratch directories
Get-ChildItem -LiteralPath $Root -Recurse -Force -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match $scratchDirRegex } |
    ForEach-Object { Try-Delete $_ 'scratch-dir' }

# 4) empty directories left behind in the cache roots
if (-not $WhatIf) {
    foreach ($rel in $cacheDirs) {
        $dir = Join-Path $Root $rel
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        Get-ChildItem -LiteralPath $dir -Recurse -Force -Directory -ErrorAction SilentlyContinue |
            Sort-Object { $_.FullName.Length } -Descending |
            Where-Object { @(Get-ChildItem -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue).Count -eq 0 } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$line  = "{0}  days={1} {2}  deleted={3}  freed={4:N1} MB  skipped={5}" -f $stamp, $Days, $(if ($WhatIf) { 'DRY' } else { 'RUN' }), $deleted.Count, ($freed / 1MB), $skipped
if (-not $Quiet) { Write-Output $line }
Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8

if ($deleted.Count -gt 0) {
    $reportDir = Join-Path $PSScriptRoot 'cleanup-reports'
    if (-not (Test-Path -LiteralPath $reportDir)) { New-Item -ItemType Directory -Path $reportDir -Force | Out-Null }
    $report = Join-Path $reportDir ("cleanup-{0}.csv" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $deleted | Sort-Object MB -Descending | Export-Csv -LiteralPath $report -NoTypeInformation -Encoding utf8
    if (-not $Quiet) { Write-Output ("report: " + $report) }
}
