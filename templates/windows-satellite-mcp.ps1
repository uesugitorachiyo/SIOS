<#
Local Windows stdio MCP launcher. Keep config.json, ca.crt, and the DPAPI token
outside the source checkout. Pass the expected server identity and CA file hash
from an independently verified enrollment record.
#>
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
if (-not $IsWindows -and $PSVersionTable.PSEdition -eq 'Core') {
    throw 'This helper is for Windows only.'
}
if ($Enroll -and $Revoke) { throw 'Choose only one action.' }

# Use this Windows host's official module, even when PS7 paths were inherited.
$securityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
Import-Module -Name $securityModule -ErrorAction Stop

if ([string]::IsNullOrWhiteSpace($SetupRoot)) {
    $root = Join-Path $env:LOCALAPPDATA 'SIOS\client'
} else {
    if (-not [System.IO.Path]::IsPathRooted($SetupRoot)) {
        throw 'SetupRoot must be absolute.'
    }
    $root = $SetupRoot
}
$secretPath = Join-Path $root 'token.dpapi'
$configPath = Join-Path $root 'config.json'
$caPath = Join-Path $root 'ca.crt'

if ($Revoke) {
    Remove-Item -LiteralPath $secretPath -ErrorAction SilentlyContinue
    Write-Output 'Local token enrollment removed.'
    return
}

if ($Enroll) {
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    if (Test-Path -LiteralPath $secretPath) {
        throw 'Enrollment exists. Revoke it before replacing the token.'
    }
    $secure = Read-Host -Prompt 'Enter the SIOS satellite token' -AsSecureString
    try {
        if ($secure.Length -lt 32 -or $secure.Length -gt 4096) {
            throw 'The token length is invalid.'
        }
        # On Windows, no -Key uses current-user DPAPI protection.
        $protected = ConvertFrom-SecureString -SecureString $secure
        [System.IO.File]::WriteAllText($secretPath, $protected,
            [System.Text.Encoding]::ASCII)
    } finally {
        $secure.Dispose()
    }
    Write-Output 'Token enrolled for this Windows user.'
    return
}

foreach ($path in @($secretPath, $configPath, $caPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'Client configuration, public CA or token enrollment is missing.'
    }
}
if ([string]::IsNullOrWhiteSpace($SiosExe) -or
    -not [System.IO.Path]::IsPathRooted($SiosExe) -or
    -not (Test-Path -LiteralPath $SiosExe -PathType Leaf)) {
    throw 'Pass the verified absolute path to the SIOS executable.'
}
if ($ExpectedInstanceId -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -or
    $ExpectedEndpoint -notmatch '^https://[^\s/]+(:[0-9]+)?$' -or
    $ExpectedCaSha256 -notmatch '^[0-9a-fA-F]{64}$') {
    throw 'Pass independently verified instance, HTTPS endpoint and CA file hash.'
}
$actualCaFileHash = (Get-FileHash -LiteralPath $caPath -Algorithm SHA256).Hash
if ($actualCaFileHash -ne $ExpectedCaSha256) {
    throw 'The public CA file hash does not match the expected value.'
}
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($config.mode -ne 'client' -or
    $config.instance_id -ne $ExpectedInstanceId -or
    $config.endpoint -ne $ExpectedEndpoint -or
    $config.token_env -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
    throw 'The client configuration does not match the expected SIOS instance.'
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
} finally {
    [Environment]::SetEnvironmentVariable($tokenEnv, $null, 'Process')
    if ($pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $token = $null
    $secure.Dispose()
}
exit $exitCode
