<#
.SYNOPSIS
    Run a command with the system idle timer suppressed, then release it.

.DESCRIPTION
    Without this the unattended run does not survive its own wake-up. When Task
    Scheduler wakes a sleeping PC with a wake timer, Windows treats it as an
    UNATTENDED wake and applies the "unattended sleep timeout" - two minutes by
    default. The machine goes straight back to sleep while the pipeline is still
    downloading clips, and you find a half-finished run in the morning.

    SetThreadExecutionState(ES_SYSTEM_REQUIRED | ES_CONTINUOUS) tells Windows the
    system is in use until told otherwise. The assertion belongs to the thread that
    made it, so this script has to be the one that waits for the child - it holds the
    flag for exactly as long as the run takes and clears it afterwards, including on
    Ctrl-C. The display is deliberately NOT asserted: at 03:00 the screen should stay
    off.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\keep_awake.ps1 `
        -Exe "C:\repo\venv\Scripts\python.exe" -Arguments "-m pipeline.run_daily"
#>
[CmdletBinding()]
param(
    # Executable to run. Quote it - the path may contain spaces.
    [Parameter(Mandatory = $true)][string]$Exe,

    # Arguments as ONE string, split on whitespace. Individual arguments therefore
    # cannot contain spaces; config overlay filenames must not either.
    [string]$Arguments = ""
)

Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class KeepAwake {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
}
'@

$ES_CONTINUOUS      = [uint32]"0x80000000"
$ES_SYSTEM_REQUIRED = [uint32]"0x00000001"

$held = [KeepAwake]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED)
if ($held -eq 0) {
    # Non-fatal: the run still works on a machine that never sleeps.
    Write-Warning "could not suppress the idle timer - the PC may sleep mid-run"
} else {
    Write-Output "keep-awake: idle timer suppressed for the duration of the run"
}

try {
    $argv = @($Arguments -split '\s+' | Where-Object { $_ })
    if ($argv.Count -gt 0) { & $Exe @argv } else { & $Exe }
    $exit = $LASTEXITCODE
} finally {
    # Drop back to normal power behaviour even if the run threw or was interrupted,
    # otherwise the PC would stay awake indefinitely.
    [void][KeepAwake]::SetThreadExecutionState($ES_CONTINUOUS)
}

if ($null -eq $exit) { $exit = 0 }
exit $exit
