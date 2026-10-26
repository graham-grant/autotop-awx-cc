# Installed Windows Server roles, for the server facet (scripts/drivers/windows.py).
# Checked by readonly.PowerShellPolicy before it is sent.
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'SilentlyContinue'
$roles = @(Get-WindowsFeature | Where-Object { $_.Installed -and $_.FeatureType -eq 'Role' } | Select-Object Name, DisplayName)
'AUTOTOP-BEGIN'
ConvertTo-Json -InputObject $roles -Compress
'AUTOTOP-END'
