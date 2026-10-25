# Stops the background bot and removes the autostart task.
$TaskName = "MusicBot"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Autostart removed and the background bot stopped."
