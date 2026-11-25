# 启动风控系统依赖的基础设施：Redis(6379) + Neo4j(7687)。已在监听的端口直接跳过。
# F5 时由 VS Code preLaunchTask 自动调用；也可以手动运行：powershell -File scripts\start_infra.ps1
$ErrorActionPreference = "Stop"

$RedisDir = "D:\software\redis\Redis-8.10.3-Windows-x64-msys2"
$Neo4jDir = "D:\software\neo4j\neo4j-community-5.26.12"

function Test-Port([int]$Port) {
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $Port); return $true } catch { return $false } finally { $c.Close() }
}

function Wait-Port([int]$Port, [int]$Seconds, [string]$Name) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $Port) { Write-Host "[infra] $Name 已就绪 (端口 $Port)"; return }
        Start-Sleep -Seconds 1
    }
    throw "[infra] $Name 在 $Seconds 秒内没有启动，请检查 $Name 日志"
}

# 用 WMI Win32_Process.Create 启动：进程挂在 WmiPrvSE 下，不属于调用方的进程树/作业对象，
# 调用脚本的终端、VS Code 任务或自动化会话结束时不会把 Redis/Neo4j 一起带走。
function Start-Detached([string]$CommandLine, [string]$WorkDir) {
    $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $CommandLine; CurrentDirectory = $WorkDir; ProcessStartupInformation = $startup }
    if ($r.ReturnValue -ne 0) { throw "[infra] 启动失败 (Win32_Process.Create 返回 $($r.ReturnValue))：$CommandLine" }
    return $r.ProcessId
}

if (Test-Port 6379) {
    Write-Host "[infra] Redis 已在运行 (6379)"
} else {
    Write-Host "[infra] 启动 Redis ..."
    $procId = Start-Detached "`"$RedisDir\redis-server.exe`" redis.conf" $RedisDir
    Write-Host "[infra] Redis PID $procId"
    Wait-Port 6379 15 "Redis"
}

if (Test-Port 7687) {
    Write-Host "[infra] Neo4j 已在运行 (7687)"
} else {
    Write-Host "[infra] 启动 Neo4j（首次约 20-40 秒）..."
    $procId = Start-Detached "cmd.exe /c bin\neo4j.bat console" $Neo4jDir
    Write-Host "[infra] Neo4j 启动器 PID $procId"
    Wait-Port 7687 90 "Neo4j"
}