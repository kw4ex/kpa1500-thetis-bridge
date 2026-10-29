# KPA1500 bridge - network check (read-only: changes nothing)
$ErrorActionPreference = 'Continue'
"=== time ===";  Get-Date
"=== PowerShell ==="; $PSVersionTable.PSVersion.ToString()
"=== installer network list (.NET, as in the installer) ==="
try {
    foreach ($ni in [Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
        foreach ($ua in $ni.GetIPProperties().UnicastAddresses) {
            if ($ua.Address.AddressFamily -ne 'InterNetwork') { continue }
            $pl = '?'; try { $pl = $ua.PrefixLength } catch { $pl = 'ERR: ' + $_.Exception.Message }
            "{0} | {1} | {2} | {3}/{4}" -f $ni.Name, $ni.OperationalStatus, $ni.NetworkInterfaceType, $ua.Address, $pl
        }
    }
} catch { "ERROR: " + $_.Exception.Message }
"=== Get-NetIPAddress ==="
try { Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Format-Table InterfaceAlias,IPAddress,PrefixLength,PrefixOrigin -AutoSize | Out-String -Width 200 } catch { "ERROR: " + $_.Exception.Message }
"=== default routes ==="
try { Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction Stop | Format-Table InterfaceAlias,NextHop,RouteMetric -AutoSize | Out-String } catch { "ERROR: " + $_.Exception.Message }
"=== ping 192.168.0.175 ==="
ping -n 2 -w 800 192.168.0.175
"=== python processes running the bridge ==="
try { Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*kpa1500*' } | ForEach-Object { "{0} {1}: {2}" -f $_.ProcessId, $_.Name, $_.CommandLine } } catch { "ERROR: " + $_.Exception.Message }
"=== bridge: running? ==="
& py -3 kpa1500_thetis_bridge.py --check-running 2>&1
"exit code $LASTEXITCODE"
"=== bridge: probe 192.168.0.175 ==="
& py -3 kpa1500_thetis_bridge.py --probe 192.168.0.175 2>&1
"exit code $LASTEXITCODE"
"=== bridge: search the network ==="
& py -3 kpa1500_thetis_bridge.py --discover 2>&1
"exit code $LASTEXITCODE"
"=== firewall rules mentioning python or KPA ==="
(netsh advfirewall firewall show rule name=all verbose | Out-String) -split "(\r?\n){2,}" | Where-Object { $_ -match 'python|KPA1500' } | Out-String -Width 300
"=== settings file ==="
if (Test-Path kpa1500_settings.json) { Get-Content kpa1500_settings.json } else { "(none)" }
"=== end ==="
