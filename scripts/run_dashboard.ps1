# run_dashboard.ps1 — กดทีเดียว: rebuild metrics -> export ข้อมูลแดชบอร์ด -> รวมข้ามโปรเจกต์ -> เสิร์ฟ + เปิดเบราว์เซอร์
# วิธีใช้: ดับเบิลคลิก run_dashboard.bat (repo root) หรือรันสคริปต์นี้จากที่ไหนก็ได้
$ErrorActionPreference = "Stop"

# repo root = โฟลเดอร์แม่ของ scripts/ (สคริปต์นี้อยู่ใน scripts/) — กันเคส CWD ผิดแล้วเกิดไฟล์ stray
$Root = (Get-Item -LiteralPath $PSScriptRoot).Parent.FullName
Push-Location -LiteralPath $Root
try {
    Write-Host "[1/4] Rebuilding metrics..."
    & python scripts/metrics.py --rebuild
    if ($LASTEXITCODE -ne 0) { throw "metrics rebuild failed (exit $LASTEXITCODE)" }

    Write-Host "[2/4] Exporting dashboard data..."
    & python scripts/export_json.py
    if ($LASTEXITCODE -ne 0) { throw "dashboard export failed (exit $LASTEXITCODE)" }

    Write-Host "[3/4] Aggregating all projects..."
    & python scripts/aggregate.py
    if ($LASTEXITCODE -ne 0) { Write-Warning "aggregate failed - single-project dashboard still works" }

    # หาพอร์ตว่างตัวแรก 8080..8090 (ลอง bind จริง ถ้าติดแสดงว่ามีคนใช้แล้ว)
    $Port = $null
    foreach ($p in 8080..8090) {
        $l = $null
        try {
            $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $p)
            $l.Start(); $l.Stop(); $Port = $p; break
        } catch { if ($l) { try { $l.Stop() } catch {} } }
    }
    if (-not $Port) { throw "no free port in 8080..8090" }

    Write-Host "[4/4] Serving dashboard at http://localhost:$Port/dashboard/ ..."
    $job = Start-Job -ScriptBlock {
        Set-Location -LiteralPath $using:Root
        & python -m http.server $using:Port
    }
    try {
        # รอจนพอร์ตตอบ (สูงสุด ~10 วินาที)
        $ready = $false
        for ($i = 0; $i -lt 50 -and -not $ready; $i++) {
            $t = New-Object Net.Sockets.TcpClient
            try { $t.Connect("127.0.0.1", $Port); $ready = $true } catch {}
            $t.Close()
            if (-not $ready) { Start-Sleep -Milliseconds 200 }
        }
        if (-not $ready) { throw "server did not start on port $Port" }
        Start-Process "http://localhost:$Port/dashboard/all.html"
        Write-Host "Browser opened. Press Ctrl+C in this window to stop the server."
        Wait-Job -Job $job | Out-Null
    } finally {
        Stop-Job -Job $job -ErrorAction SilentlyContinue
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
} finally {
    Pop-Location
}
