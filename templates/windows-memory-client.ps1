param(
    [switch]$Enroll,
    [switch]$Revoke,
    [string]$SiosExe,
    [string]$SetupRoot,
    [string]$ExpectedInstanceId,
    [string]$ExpectedEndpoint,
    [string]$ExpectedCaSha256
)

$ErrorActionPreference = 'Stop'
# Use this host's Microsoft module; inherited PS7 paths can shadow WinPS 5.1.
$securityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
Import-Module -Name $securityModule -ErrorAction Stop
# Pin Utility too: inherited PS7 module paths can hide WinPS Get-FileHash/JSON cmdlets.
$utilityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1'
Import-Module -Name $utilityModule -ErrorAction Stop
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'This helper is for Windows only.'
}
if ($Enroll -and $Revoke) {
    throw 'Choose only one action.'
}

if ([string]::IsNullOrWhiteSpace($SetupRoot)) {
    $root = Join-Path $env:LOCALAPPDATA 'SIOS\Satellite'
} else {
    if (-not [System.IO.Path]::IsPathRooted($SetupRoot)) { throw 'SetupRoot must be absolute.' }
    $root = $SetupRoot
}
$secretPath = Join-Path $root 'satellite-token.dpapi'
$configPath = Join-Path $root 'config.json'
$caPath = Join-Path $root 'ca.crt'

if ($Revoke) {
    Remove-Item -LiteralPath $secretPath -ErrorAction SilentlyContinue
    Write-Output 'satellite local token enrollment removed.'
    return
}

if ($Enroll) {
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    if (Test-Path -LiteralPath $secretPath) {
        throw 'Enrollment already exists. Revoke it before replacing the token.'
    }
    $secure = Read-Host -Prompt 'Enter the satellite SIOS token' -AsSecureString
    try {
        if ($secure.Length -lt 32 -or $secure.Length -gt 4096) {
            throw 'The token length is invalid.'
        }
        # On Windows, no -Key means DPAPI protects the file for this user.
        $protected = ConvertFrom-SecureString -SecureString $secure
        [System.IO.File]::WriteAllText($secretPath, $protected,
            [System.Text.Encoding]::ASCII)
    }
    finally {
        $secure.Dispose()
    }
    Write-Output 'satellite token enrolled for this Windows user.'
    return
}

foreach ($path in @($secretPath, $configPath, $caPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'satellite client configuration, public CA or token enrollment is missing.'
    }
}
if ([string]::IsNullOrWhiteSpace($SiosExe) -or
    -not [System.IO.Path]::IsPathRooted($SiosExe) -or
    -not (Test-Path -LiteralPath $SiosExe -PathType Leaf)) {
    throw 'Pass the verified absolute path to the installed sios.exe.'
}

if ($ExpectedInstanceId -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -or
    $ExpectedEndpoint -notmatch '^https://[^\s/]+(:[0-9]+)?$' -or
    $ExpectedCaSha256 -notmatch '^[0-9a-fA-F]{64}$') {
    throw 'Pass independently verified instance, HTTPS endpoint and CA file hash.'
}
# File SHA-256 checks the copied PEM bytes; this is not the X.509 fingerprint.
$actualCaFileHash = (Get-FileHash -LiteralPath $caPath -Algorithm SHA256).Hash
if ($actualCaFileHash -ne $ExpectedCaSha256) {
    throw 'The public CA file does not match the configured CA.'
}

$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($config.mode -ne 'client' -or
    $config.instance_id -cne $ExpectedInstanceId -or
    $config.endpoint -cne $ExpectedEndpoint -or
    $config.token_env -cnotmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    throw 'The satellite client configuration targets a different instance.'
}

$tokenEnv = $config.token_env
$secure = ConvertTo-SecureString -String (
    [System.IO.File]::ReadAllText($secretPath, [System.Text.Encoding]::ASCII))
$pointer = [IntPtr]::Zero
try {
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    if ($token.Length -lt 32 -or $token.Length -gt 4096) {
        throw 'The enrolled token length is invalid.'
    }
    [Environment]::SetEnvironmentVariable($tokenEnv, $token, 'Process')
    & $SiosExe --config $configPath --ca-file $caPath mcp
    $exitCode = $LASTEXITCODE
}
finally {
    [Environment]::SetEnvironmentVariable($tokenEnv, $null, 'Process')
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $token = $null
    $secure.Dispose()
}
exit $exitCode
