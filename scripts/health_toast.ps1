<#
.SYNOPSIS
    Windows toast for a nightly run that needs a look (pipeline/tools/health.py).

.DESCRIPTION
    No module needed (BurntToast is not installed): the WinRT toast API is reachable
    from Windows PowerShell 5.1 directly, posted under PowerShell's own app id so no
    shortcut/AppUserModelID registration is required.

    The run ends ~06:00 to an empty room, so the toast has to survive until morning:
      * it goes to the Notification Center, where it stays (default expiry 3 days)
        even if the PC sleeps right after (SLEEP_AFTER=1);
      * scenario="reminder" keeps the banner on screen until it is dismissed, so it is
        the first thing you see after unlocking instead of a 5-second pop-up nobody saw.
    "Open report" opens data\logs\health_<date>.md.

    The scheduled task runs only when the user is logged on (setup_schedule.bat /ru
    without a password), i.e. in the user's own session, which is what toasts need.
    If WinRT is unavailable for any reason, falls back to msg.exe (a plain message box
    that waits up to 12 h).

    Exit codes: 0 shown, 1 nothing could be shown.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Title,
    [Parameter(Mandatory)] [string]$Body,
    [string]$Report = ""
)

$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Esc([string]$s) { [Security.SecurityElement]::Escape($s) }

try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

    $open = ""
    if ($Report -and (Test-Path $Report)) {
        $uri = ([Uri](Resolve-Path $Report).Path).AbsoluteUri
        $open = "<action content=`"Open report`" activationType=`"protocol`" arguments=`"$(Esc $uri)`"/>"
    }
    $xml = @"
<toast scenario="reminder">
  <visual><binding template="ToastGeneric">
    <text>$(Esc $Title)</text>
    <text>$(Esc $Body)</text>
  </binding></visual>
  <actions>
    $open
    <action content="Dismiss" activationType="system" arguments="dismiss"/>
  </actions>
</toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = [Windows.UI.Notifications.ToastNotification]::new($doc)
    $toast.Tag = "kemono-health"
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
    Write-Output "health toast shown: $Title"
    exit 0
} catch {
    Write-Output "health toast failed ($($_.Exception.Message)) - trying msg.exe"
}

try {
    $text = "$Title`n`n$Body"
    if ($Report) { $text += "`n`n$Report" }
    & msg.exe $env:USERNAME /TIME:43200 $text
    if ($LASTEXITCODE -eq 0) { exit 0 }
} catch { }
exit 1
