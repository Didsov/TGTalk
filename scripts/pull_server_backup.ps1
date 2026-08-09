[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,

    [string]$RemoteUser = "inntophone",
    [string]$RemoteProject = "/opt/inntophone",
    [string]$RepositoryRoot = "",
    [string]$LocalBackupDirectory = "D:\INNtoPhone-Backups",
    [int]$RemoteKeep = 3,
    [int]$LocalKeep = 7
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $scriptPath = $MyInvocation.MyCommand.Path
    if ([string]::IsNullOrWhiteSpace($scriptPath)) {
        throw "The script path could not be determined. Pass -RepositoryRoot explicitly."
    }
    $RepositoryRoot = Split-Path -Parent (Split-Path -Parent $scriptPath)
}

if ($RemoteKeep -lt 1 -or $LocalKeep -lt 1) {
    throw "RemoteKeep and LocalKeep must be greater than zero."
}
if ($RemoteUser -notmatch '^[a-z_][a-z0-9_-]*$') {
    throw "Invalid remote user name."
}
if ($Server -notmatch '^[a-zA-Z0-9._:-]+$') {
    throw "Invalid server address."
}
if ($RemoteProject -notmatch '^/[a-zA-Z0-9._/-]+$' -or $RemoteProject.Contains('..')) {
    throw "RemoteProject must be a safe absolute Linux path."
}

foreach ($command in @("ssh", "scp")) {
    if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "$command was not found. Install OpenSSH Client."
    }
}

$repository = [System.IO.Path]::GetFullPath($RepositoryRoot)
$localDirectory = [System.IO.Path]::GetFullPath($LocalBackupDirectory)
New-Item -ItemType Directory -Path $localDirectory -Force | Out-Null

$venvPython = Join-Path $repository ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }

$remoteDatabase = "$RemoteProject/data/clients.db"
$remoteResponses = "$RemoteProject/data/telegram_reports"
$remoteOutput = "$RemoteProject/data/backups"
$remoteCommand = (
    "cd {0} && .venv/bin/python -m src.cli.backup create " +
    "--database {1} --responses {2} --output {3} --keep {4}"
) -f $RemoteProject, $remoteDatabase, $remoteResponses, $remoteOutput, $RemoteKeep

Write-Host "Creating a consistent snapshot on the server..."
$remoteOutputLines = & ssh "$RemoteUser@$Server" $remoteCommand
if ($LASTEXITCODE -ne 0) {
    throw "The server could not create a backup."
}
$jsonLine = $remoteOutputLines | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
if ([string]::IsNullOrWhiteSpace($jsonLine)) {
    throw "The server did not return backup metadata."
}
$result = $jsonLine | ConvertFrom-Json
if (-not $result.ok) {
    throw "The server reported a backup error."
}

$remoteBackupPath = [string]$result.backup_path
$allowedRemotePrefix = "$remoteOutput/inntophone-backup-"
if (-not $remoteBackupPath.StartsWith($allowedRemotePrefix) -or
    -not $remoteBackupPath.EndsWith(".zip")) {
    throw "The server returned an unexpected backup path."
}

$fileName = [System.IO.Path]::GetFileName($remoteBackupPath)
$partialPath = Join-Path $localDirectory "$fileName.partial"
$finalPath = Join-Path $localDirectory $fileName

try {
    Write-Host "Downloading $fileName..."
    & scp "$RemoteUser@$Server`:$remoteBackupPath" $partialPath
    if ($LASTEXITCODE -ne 0) {
        throw "SCP could not download the backup."
    }

    Push-Location $repository
    try {
        $verifyOutput = & $python -m src.cli.backup verify --archive $partialPath
        if ($LASTEXITCODE -ne 0) {
            throw "The downloaded archive failed verification."
        }
        $verifyResult = $verifyOutput | ConvertFrom-Json
        if (-not $verifyResult.ok) {
            throw "The downloaded archive failed verification."
        }
    }
    finally {
        Pop-Location
    }

    Move-Item -LiteralPath $partialPath -Destination $finalPath -Force
}
catch {
    Remove-Item -LiteralPath $partialPath -Force -ErrorAction SilentlyContinue
    throw
}

$backups = Get-ChildItem -LiteralPath $localDirectory -File |
    Where-Object { $_.Name -like "inntophone-backup-*.zip" } |
    Sort-Object Name -Descending

$expired = $backups | Select-Object -Skip $LocalKeep
foreach ($backup in $expired) {
    $resolvedBackup = [System.IO.Path]::GetFullPath($backup.FullName)
    $expectedPrefix = $localDirectory.TrimEnd('\') + '\'
    if (-not $resolvedBackup.StartsWith($expectedPrefix)) {
        throw "Refusing to delete a file outside the backup directory."
    }
    Remove-Item -LiteralPath $resolvedBackup -Force
}

Write-Host "Backup saved and verified: $finalPath"
