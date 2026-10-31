# Read-only inventory of a Windows server for autotop (scripts/drivers/windows.py).
# Checked by readonly.PowerShellPolicy before it is sent: only allowlisted
# Get-* cmdlets and formatting, no file writes, no dynamic code.
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'SilentlyContinue'
$cs = Get-CimInstance -ClassName Win32_ComputerSystem
$os = Get-CimInstance -ClassName Win32_OperatingSystem
$bios = Get-CimInstance -ClassName Win32_BIOS
$out = [ordered]@{
  computer = $cs | Select-Object Name, DNSHostName, Domain, PartOfDomain, Manufacturer, Model
  os = $os | Select-Object Caption, Version, BuildNumber
  serial = $bios.SerialNumber
  adapters = @(Get-NetAdapter | Select-Object Name, InterfaceDescription, InterfaceIndex, MacAddress, Status, AdminStatus, ReceiveLinkSpeed, MtuSize, VlanID)
  addresses = @(Get-NetIPAddress | Select-Object InterfaceAlias, IPAddress, PrefixLength, AddressFamily)
  neighbors = @(Get-NetNeighbor -AddressFamily IPv4 | Select-Object InterfaceAlias, IPAddress, LinkLayerAddress, State)
  teams = @(Get-NetLbfoTeam | Select-Object Name, Members, TeamingMode)
}
'AUTOTOP-BEGIN'
ConvertTo-Json -InputObject $out -Depth 4 -Compress
'AUTOTOP-END'
