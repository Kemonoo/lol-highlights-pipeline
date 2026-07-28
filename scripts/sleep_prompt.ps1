<#
.SYNOPSIS
    Put the PC to sleep after an unattended run, with a chance to say no.

.DESCRIPTION
    Called at the end of run_daily_auto.bat when SLEEP_AFTER=1. Two situations to
    tell apart:

      * Nobody is there (the 03:00 case). Sleeping silently is the whole point, so
        if the session has been idle for -IdleMinutes, or there is no interactive
        desktop at all, it suspends immediately.

      * Somebody is using the PC (a run kicked off by hand, or you sat down while it
        finished). A countdown window appears on top of whatever you are doing;
        anything you click except "Sleep now" cancels, and so does closing it.

    Cancelling is the safe default in every ambiguous case: a machine that stayed
    awake costs a few watts, a machine that slept mid-sentence costs you work.

.NOTES
    Sleep is requested via SetSuspendState. On a machine with hibernation enabled
    Windows may hibernate instead of suspending - `powercfg /hibernate off` if you
    want true sleep.

    Exit codes: 0 slept, 2 cancelled, 3 skipped (dry run).
#>
[CmdletBinding()]
param(
    # How long the cancel window stays up before sleeping.
    [int]$Seconds = 120,

    # Idle time after which we assume nobody is at the keyboard and skip the prompt.
    # 0 always prompts.
    [int]$IdleMinutes = 5,

    # Report the decision and exit without touching power state. For testing.
    [switch]$DryRun
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class LastInput {
    [StructLayout(LayoutKind.Sequential)]
    private struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [DllImport("user32.dll")]
    private static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    public static uint Seconds() {
        LASTINPUTINFO lii = new LASTINPUTINFO();
        lii.cbSize = (uint)Marshal.SizeOf(lii);
        if (!GetLastInputInfo(ref lii)) { return 0; }
        return ((uint)Environment.TickCount - lii.dwTime) / 1000;
    }
}
'@

function Write-Step([string]$msg) {
    Write-Output ("sleep: {0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $msg)
}

function Invoke-Sleep([string]$why) {
    Write-Step "sleeping ($why)"
    if ($DryRun) {
        Write-Step "DRY RUN - not actually suspending"
        exit 3
    }
    # forceCritical=$false so drivers may veto; disableWake=$false so the next
    # scheduled wake timer still fires.
    [void][System.Windows.Forms.Application]::SetSuspendState(
        [System.Windows.Forms.PowerState]::Suspend, $false, $false)
    exit 0
}

# ── is anyone there? ─────────────────────────────────────────────────────────
if (-not [Environment]::UserInteractive) {
    Invoke-Sleep "no interactive desktop to prompt on"
}

$idle = [LastInput]::Seconds()
if ($IdleMinutes -gt 0 -and $idle -ge ($IdleMinutes * 60)) {
    Invoke-Sleep ("idle {0:n0} min, nobody to ask" -f ($idle / 60))
}
Write-Step ("someone may be at the PC (idle {0}s) - asking first" -f $idle)

# ── countdown window ─────────────────────────────────────────────────────────
$form = New-Object System.Windows.Forms.Form
$form.Text = "Daily highlights run finished"
$form.Size = New-Object System.Drawing.Size(440, 195)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true

$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(20, 20)
$label.Size = New-Object System.Drawing.Size(390, 46)
$label.Text = "The daily highlights run has finished."
$form.Controls.Add($label)

$countdown = New-Object System.Windows.Forms.Label
$countdown.Location = New-Object System.Drawing.Point(20, 62)
$countdown.Size = New-Object System.Drawing.Size(390, 30)
$countdown.Font = New-Object System.Drawing.Font($label.Font.FontFamily, 12,
                                                 [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($countdown)

$stay = New-Object System.Windows.Forms.Button
$stay.Location = New-Object System.Drawing.Point(200, 110)
$stay.Size = New-Object System.Drawing.Size(100, 32)
$stay.Text = "Stay awake"
$form.Controls.Add($stay)
$form.AcceptButton = $stay      # Enter/Esc both cancel - the safe direction
$form.CancelButton = $stay

$now = New-Object System.Windows.Forms.Button
$now.Location = New-Object System.Drawing.Point(310, 110)
$now.Size = New-Object System.Drawing.Size(100, 32)
$now.Text = "Sleep now"
$form.Controls.Add($now)

# $script: scope so the timer/button handlers mutate the same variables the code
# below reads - a plain assignment inside a scriptblock would create a local.
$script:remaining = $Seconds
$script:decision = "pending"

$stay.Add_Click({ $script:decision = "cancelled"; $form.Close() })
$now.Add_Click({ $script:decision = "confirmed"; $form.Close() })
# Closing the window with X reads as "no" too. Only "pending" is converted, so the
# timer's own Close() below is not mistaken for the user dismissing the window.
$form.Add_FormClosing({ if ($script:decision -eq "pending") { $script:decision = "cancelled" } })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    $script:remaining--
    $countdown.Text = "Sleeping in {0}..." -f [TimeSpan]::FromSeconds([Math]::Max(0, $script:remaining)).ToString("mm\:ss")
    if ($script:remaining -le 0) {
        $timer.Stop()
        $script:decision = "timeout"
        $form.Close()
    }
})

$countdown.Text = "Sleeping in {0}..." -f [TimeSpan]::FromSeconds($Seconds).ToString("mm\:ss")
$timer.Start()
[void]$form.ShowDialog()
$timer.Stop()
$timer.Dispose()
$form.Dispose()

switch ($script:decision) {
    "cancelled" { Write-Step "cancelled - staying awake"; exit 2 }
    "confirmed" { Invoke-Sleep "confirmed by user" }
    default     { Invoke-Sleep "no answer in ${Seconds}s" }
}
