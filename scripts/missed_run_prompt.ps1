<#
.SYNOPSIS
    Ask before a LATE scheduled run: "the 03:00 run was missed - run now, or tonight?"

.DESCRIPTION
    Called at the top of run_daily_auto.bat with the time the task is scheduled for.
    Task Scheduler's StartWhenAvailable starts a missed run as soon as the PC is back
    on, which is usually the right call - but it lands on you mid-afternoon, taking the
    GPU for a few hours. So:

      * On time (within -LateMinutes of the scheduled time): no window, run.
      * Late, nobody at the PC (idle, or no desktop): no window, run.
      * Late, somebody there: a window with a countdown. "Run now" or no answer runs
        it (the default - a missed night should still get its video); "Tonight"
        postpones: tonight's scheduled run catches the missed date(s) up, spacing
        the uploads (schedule.catch_up_days, upload.min_gap_hours).

    Exit codes: 0 run, 2 postponed.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ScheduledAt,    # HH:mm, 24h
    [int]$LateMinutes = 60,
    [int]$Seconds = 90,
    [int]$IdleMinutes = 5
)

function Write-Step([string]$msg) {
    Write-Output ("missed-run: {0}  {1}" -f (Get-Date -Format "HH:mm:ss"), $msg)
}

try {
    $at = [DateTime]::ParseExact($ScheduledAt, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
} catch {
    Write-Step "can't read scheduled time '$ScheduledAt' - running"
    exit 0
}
$now = Get-Date
$last = $now.Date.Add($at.TimeOfDay)
if ($last -gt $now) { $last = $last.AddDays(-1) }      # most recent scheduled moment
$late = ($now - $last).TotalMinutes
if ($late -le $LateMinutes) { exit 0 }
Write-Step ("started {0:n0} min after the {1} slot" -f $late, $ScheduledAt)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class MissedRunInput {
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

if (-not [Environment]::UserInteractive) { Write-Step "no desktop to ask on - running"; exit 0 }
$idle = [MissedRunInput]::Seconds()
if ($IdleMinutes -gt 0 -and $idle -ge ($IdleMinutes * 60)) {
    Write-Step ("idle {0:n0} min, nobody to ask - running" -f ($idle / 60)); exit 0
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "Daily highlights run"
$form.Size = New-Object System.Drawing.Size(460, 205)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true

$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(20, 18)
$label.Size = New-Object System.Drawing.Size(410, 48)
$label.Text = ("The {0} highlights run was missed (the PC was off). It takes a few hours " +
               "and uses the GPU. Run it now, or leave it for tonight?") -f $ScheduledAt
$form.Controls.Add($label)

$countdown = New-Object System.Windows.Forms.Label
$countdown.Location = New-Object System.Drawing.Point(20, 70)
$countdown.Size = New-Object System.Drawing.Size(410, 30)
$countdown.Font = New-Object System.Drawing.Font($label.Font.FontFamily, 12,
                                                 [System.Drawing.FontStyle]::Bold)
$form.Controls.Add($countdown)

$later = New-Object System.Windows.Forms.Button
$later.Location = New-Object System.Drawing.Point(200, 115)
$later.Size = New-Object System.Drawing.Size(110, 32)
$later.Text = "Tonight"
$form.Controls.Add($later)
$form.CancelButton = $later

$now = New-Object System.Windows.Forms.Button
$now.Location = New-Object System.Drawing.Point(320, 115)
$now.Size = New-Object System.Drawing.Size(110, 32)
$now.Text = "Run now"
$form.Controls.Add($now)
$form.AcceptButton = $now

$script:remaining = $Seconds
$script:decision = "pending"
$later.Add_Click({ $script:decision = "later"; $form.Close() })
$now.Add_Click({ $script:decision = "now"; $form.Close() })
# Closing with X = "Tonight": the user saw it and dismissed it.
$form.Add_FormClosing({ if ($script:decision -eq "pending") { $script:decision = "later" } })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    $script:remaining--
    $countdown.Text = "Starting in {0}..." -f [TimeSpan]::FromSeconds([Math]::Max(0, $script:remaining)).ToString("mm\:ss")
    if ($script:remaining -le 0) { $timer.Stop(); $script:decision = "timeout"; $form.Close() }
})
$countdown.Text = "Starting in {0}..." -f [TimeSpan]::FromSeconds($Seconds).ToString("mm\:ss")
$timer.Start()
[void]$form.ShowDialog()
$timer.Stop(); $timer.Dispose(); $form.Dispose()

switch ($script:decision) {
    "later"   { Write-Step "postponed to tonight by user"; exit 2 }
    "now"     { Write-Step "user chose to run now"; exit 0 }
    default   { Write-Step "no answer in ${Seconds}s - running"; exit 0 }
}
