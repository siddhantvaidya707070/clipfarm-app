$ErrorActionPreference = 'Stop'
$clipfarmRoot = Split-Path -Parent $PSScriptRoot
$ownerSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$principals = @($ownerSid, [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18'), [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
function Test-PrivateAcl($pathToCheck) {
    $aclToCheck = Get-Acl -LiteralPath $pathToCheck
    $allowedSids = @($principals | ForEach-Object { $_.Value })
    foreach ($ruleToCheck in $aclToCheck.Access) {
        if ($ruleToCheck.AccessControlType -eq 'Allow' -and $ruleToCheck.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -notin $allowedSids) { return $false }
    }
    return $true
}
$secretDirectory = Join-Path $clipfarmRoot 'secrets'
if ((Test-Path -LiteralPath $secretDirectory) -and -not (Test-PrivateAcl $secretDirectory)) {
    $directoryAcl = [System.Security.AccessControl.DirectorySecurity]::new()
    $directoryAcl.SetOwner($ownerSid)
    $directoryAcl.SetAccessRuleProtection($true, $false)
    foreach ($principal in $principals) {
        $directoryAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($principal, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $secretDirectory -AclObject $directoryAcl
}
$privateFiles = @((Join-Path $clipfarmRoot '.env'), (Join-Path (Split-Path -Parent $clipfarmRoot) '.env.local'))
if (Test-Path -LiteralPath $secretDirectory) { $privateFiles += @(Get-ChildItem -LiteralPath $secretDirectory -File -Recurse | ForEach-Object { $_.FullName }) }
foreach ($privateFile in $privateFiles) {
    if (-not (Test-Path -LiteralPath $privateFile)) { continue }
    if (Test-PrivateAcl $privateFile) { continue }
    # Files provisioned by the sandbox may have another owner. Replace their
    # identical bytes under the operator account rather than requiring elevation
    # to take ownership of that other account's file.
    $currentOwner = (Get-Acl -LiteralPath $privateFile).GetOwner([System.Security.Principal.SecurityIdentifier])
    if ($currentOwner.Value -ne $ownerSid.Value) {
        $privateTemporary = Join-Path (Split-Path -Parent $privateFile) ('.secure-' + [guid]::NewGuid().ToString('N') + '.tmp')
        [System.IO.File]::WriteAllBytes($privateTemporary, [System.IO.File]::ReadAllBytes($privateFile))
        [System.IO.File]::Move($privateTemporary, [System.IO.Path]::GetFullPath($privateFile), $true)
    }
    if (Test-PrivateAcl $privateFile) { continue }
    $fileAcl = [System.Security.AccessControl.FileSecurity]::new()
    $fileAcl.SetOwner($ownerSid)
    $fileAcl.SetAccessRuleProtection($true, $false)
    foreach ($principal in $principals) { $fileAcl.AddAccessRule([System.Security.AccessControl.FileSystemAccessRule]::new($principal, 'FullControl', 'Allow')) }
    Set-Acl -LiteralPath $privateFile -AclObject $fileAcl
}
Write-Output 'Secret file access restricted to the Windows owner, SYSTEM and Administrators.'
