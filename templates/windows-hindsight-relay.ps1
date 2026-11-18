param(
    [switch]$Enroll,
    [Parameter(Mandatory=$true)][string]$SetupRoot,
    [string]$Python,
    [string]$Endpoint,
    [string]$ExpectedCaSha256,
    [string]$RelayScript,
    [string]$ExpectedRelaySha256
)
# Keep this setup directory, CA and DPAPI-protected token outside the checkout.
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) { throw 'Windows is required.' }
if (-not [IO.Path]::IsPathRooted($SetupRoot)) { throw 'SetupRoot must be absolute.' }
$secretPath = Join-Path $SetupRoot 'gateway-token.dpapi'
$caPath = Join-Path $SetupRoot 'ca.crt'
if ($Enroll) {
    if (-not (Test-Path -LiteralPath $SetupRoot -PathType Container)) { throw 'Create the private setup directory first.' }
    if (Test-Path -LiteralPath $secretPath) { throw 'An enrollment already exists.' }
    $secure = Read-Host -Prompt 'Gateway token' -AsSecureString
    try {
        if ($secure.Length -lt 32 -or $secure.Length -gt 512) { throw 'Invalid token length.' }
        $encoded = ConvertFrom-SecureString -SecureString $secure
        $stream = [IO.File]::Open($secretPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $bytes = [Text.Encoding]::ASCII.GetBytes($encoded)
            $stream.Write($bytes, 0, $bytes.Length)
        } finally { $stream.Dispose() }
    } finally { $secure.Dispose() }
    Write-Output 'Token enrolled for this Windows user.'
    return
}
if (-not [IO.Path]::IsPathRooted($Python) -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Supply an absolute Python path.' }
if ($RelayScript) {
    if (-not [IO.Path]::IsPathRooted($RelayScript) -or -not (Test-Path -LiteralPath $RelayScript -PathType Leaf)) { throw 'Supply an absolute relay script path.' }
    if ($ExpectedRelaySha256 -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $RelayScript -Algorithm SHA256).Hash -ne $ExpectedRelaySha256) { throw 'Relay script hash mismatch.' }
} elseif ($ExpectedRelaySha256) {
    throw 'RelayScript is required when a relay hash is supplied.'
}
if (-not (Test-Path -LiteralPath $secretPath -PathType Leaf) -or -not (Test-Path -LiteralPath $caPath -PathType Leaf)) { throw 'Enrollment or CA missing.' }
if ($ExpectedCaSha256 -notmatch '^[a-fA-F0-9]{64}$' -or (Get-FileHash -LiteralPath $caPath -Algorithm SHA256).Hash -ne $ExpectedCaSha256) { throw 'CA hash mismatch.' }
$uri = $null
if (-not [Uri]::TryCreate($Endpoint, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'https' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne '/') { throw 'Invalid HTTPS endpoint.' }
$secure = ConvertTo-SecureString -String ([IO.File]::ReadAllText($secretPath, [Text.Encoding]::ASCII))
$ptr = [IntPtr]::Zero
try {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ($token.Length -lt 32 -or $token.Length -gt 512) { throw 'Invalid enrolled token.' }
    [Environment]::SetEnvironmentVariable('SIOS_MEMORY_GATEWAY_TOKEN', $token, 'Process')
    if ($RelayScript) {
        & $Python -I -B $RelayScript --endpoint $Endpoint --ca-file $caPath --ca-sha256 $ExpectedCaSha256
    } else {
        & $Python -I -B -m aios.hindsight_relay --endpoint $Endpoint --ca-file $caPath --ca-sha256 $ExpectedCaSha256
    }
    $result = $LASTEXITCODE
} finally {
    [Environment]::SetEnvironmentVariable('SIOS_MEMORY_GATEWAY_TOKEN', $null, 'Process')
    if ($ptr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    $token = $null
    $secure.Dispose()
}
exit $result
