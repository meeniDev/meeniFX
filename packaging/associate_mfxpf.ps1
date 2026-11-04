<#
    Associates .mfxpf project files with the packaged meeniFX.exe.

    Everything is written under HKCU (per-user), so no administrator rights are needed and
    nothing outside the current Windows account is changed. Run it after extracting or
    building dist\meeniFX\:

        powershell -ExecutionPolicy Bypass -File packaging\associate_mfxpf.ps1

    Pass -ExePath to point at another copy of meeniFX.exe, and -Remove to undo.
#>
param(
    [string]$ExePath = "",
    [switch]$Remove
)

$root = "HKCU:\Software\Classes"
$progId = "meeniFX.Project"
$extension = ".mfxpf"

if ($Remove) {
    foreach ($key in @("$root\$extension", "$root\$progId")) {
        if (Test-Path $key) { Remove-Item $key -Recurse -Force }
    }
    Write-Host "Removed the .mfxpf association."
    return
}

if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $ExePath = Join-Path (Split-Path -Parent $scriptDir) "dist\meeniFX\meeniFX.exe"
}
$ExePath = (Resolve-Path $ExePath -ErrorAction Stop).Path
if (-not (Test-Path $ExePath)) { throw "meeniFX.exe was not found at $ExePath" }

New-Item -Path "$root\$extension" -Force | Out-Null
Set-ItemProperty -Path "$root\$extension" -Name "(Default)" -Value $progId
Set-ItemProperty -Path "$root\$extension" -Name "Content Type" -Value "application/x-meenifx-project"
Set-ItemProperty -Path "$root\$extension" -Name "PerceivedType" -Value "document"

New-Item -Path "$root\$progId" -Force | Out-Null
Set-ItemProperty -Path "$root\$progId" -Name "(Default)" -Value "meeniFX Project"

New-Item -Path "$root\$progId\DefaultIcon" -Force | Out-Null
Set-ItemProperty -Path "$root\$progId\DefaultIcon" -Name "(Default)" -Value "`"$ExePath`",0"

New-Item -Path "$root\$progId\shell\open\command" -Force | Out-Null
# %1 is the double-clicked project; meeniFX opens it on startup.
Set-ItemProperty -Path "$root\$progId\shell\open\command" -Name "(Default)" -Value "`"$ExePath`" `"%1`""

Write-Host ".mfxpf projects now open in $ExePath"
Write-Host "If Explorer still shows an old icon, sign out and back in (or restart explorer.exe)."
