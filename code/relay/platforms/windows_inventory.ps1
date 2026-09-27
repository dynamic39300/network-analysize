$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Read-Section([scriptblock]$Query) {
    try {
        $items = @(& $Query)
        if ($items.Count -gt 8192) { throw 'InventoryLimitExceeded' }
        return @{state='ok'; items=$items}
    } catch {
        return @{state='unknown'; items=@(); error=[string]$_.FullyQualifiedErrorId}
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$scope = @{
    user_sid = $identity.User.Value
    session_id = [Diagnostics.Process]::GetCurrentProcess().SessionId
    interactive = [Environment]::UserInteractive
    elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
$sections = [ordered]@{}
$sections.interfaces = Read-Section {
    [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() | ForEach-Object {
        $p = $_.GetIPProperties()
        $v4 = $null; $v6 = $null
        if ($_.Supports([Net.NetworkInformation.NetworkInterfaceComponent]::IPv4)) { $v4 = $p.GetIPv4Properties().Index }
        if ($_.Supports([Net.NetworkInformation.NetworkInterfaceComponent]::IPv6)) { $v6 = $p.GetIPv6Properties().Index }
        @{
            id=$_.Id; name=$_.Name; type=[string]$_.NetworkInterfaceType
            state=[string]$_.OperationalStatus; ipv4_index=$v4; ipv6_index=$v6
        }
    }
}
$sections.addresses = Read-Section {
    Get-NetIPAddress -PolicyStore ActiveStore | ForEach-Object {
        @{index=$_.InterfaceIndex; family=[string]$_.AddressFamily; address=$_.IPAddress;
          prefix_length=$_.PrefixLength; state=[string]$_.AddressState; origin=[string]$_.PrefixOrigin}
    }
}
$sections.ip_interfaces = Read-Section {
    Get-NetIPInterface -PolicyStore ActiveStore | ForEach-Object {
        @{index=$_.InterfaceIndex; family=[string]$_.AddressFamily; metric=$_.InterfaceMetric;
          dhcp=[string]$_.Dhcp; state=[string]$_.ConnectionState}
    }
}
$sections.routes = Read-Section {
    Get-NetRoute -PolicyStore ActiveStore | ForEach-Object {
        @{index=$_.InterfaceIndex; family=[string]$_.AddressFamily; destination=$_.DestinationPrefix;
          next_hop=$_.NextHop; metric=$_.RouteMetric; protocol=[string]$_.Protocol; store='ActiveStore'}
    }
}
$sections.dns = Read-Section {
    Get-DnsClientServerAddress | ForEach-Object {
        @{index=$_.InterfaceIndex; family=[int]$_.AddressFamily;
          servers=@($_.ServerAddresses | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })}
    }
}
$sections.dns_clients = Read-Section {
    Get-DnsClient | ForEach-Object {
        @{index=$_.InterfaceIndex; suffix=$_.ConnectionSpecificSuffix; register=$_.RegisterThisConnectionsAddress}
    }
}
$sections.nrpt = Read-Section {
    Get-DnsClientNrptPolicy -Effective | ForEach-Object {
        @{namespaces=@($_.Namespace | Where-Object { -not [string]::IsNullOrWhiteSpace($_) });
          servers=@($_.NameServers | Where-Object { -not [string]::IsNullOrWhiteSpace($_) });
          dnssec_required=$_.DnsSecValidationRequired}
    }
}
$sections.vpn_user = Read-Section {
    Get-VpnConnection | ForEach-Object {
        @{id=[string]$_.Guid; name=$_.Name; state=[string]$_.ConnectionStatus;
          split_tunnel=$_.SplitTunneling; tunnel_type=[string]$_.TunnelType; scope='current_user'}
    }
}
$sections.vpn_machine = Read-Section {
    Get-VpnConnection -AllUserConnection | ForEach-Object {
        @{id=[string]$_.Guid; name=$_.Name; state=[string]$_.ConnectionStatus;
          split_tunnel=$_.SplitTunneling; tunnel_type=[string]$_.TunnelType; scope='all_users'}
    }
}
@{schema='relay-windows-inventory-v1'; captured_at=[DateTime]::UtcNow.ToString('o');
  scope=$scope; sections=$sections} | ConvertTo-Json -Depth 10 -Compress
