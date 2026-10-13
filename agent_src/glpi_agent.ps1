# GLPI Agent — PowerShell (встроен в Windows 10/11, установка не нужна)
# Синхронизирует заявки GLPI с панелью 24fox.ru/glpi/ каждые 30 минут

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigFile = Join-Path $ScriptDir "config.json"
$LogFile    = Join-Path $ScriptDir "agent.log"

# ── Конфигурация ──────────────────────────────────────────────────────────────

$DefaultConfig = [ordered]@{
    glpi_url        = "https://glpi.example.com/apirest.php"
    glpi_app_token  = "CHANGE_ME_GLPI_APP_TOKEN"
    glpi_user_token = "CHANGE_ME_GLPI_USER_TOKEN"
    panel_url       = "https://24fox.ru/glpi/api/sync"
    panel_api_key   = "CHANGE_ME_PANEL_API_KEY"
    locations       = @("RU0974", "RU0943", "RU0975")
    interval_sec    = 1800
    last_sync       = 0
}

function Load-Config {
    if (Test-Path $ConfigFile) {
        try {
            $raw = Get-Content $ConfigFile -Raw -Encoding UTF8
            $obj = $raw | ConvertFrom-Json
            $cfg = [ordered]@{}
            foreach ($k in $DefaultConfig.Keys) {
                if ($obj.PSObject.Properties[$k]) { $cfg[$k] = $obj.$k }
                else { $cfg[$k] = $DefaultConfig[$k] }
            }
            return $cfg
        } catch {
            Log "Config read error: $_"
        }
    }
    $DefaultConfig | ConvertTo-Json -Depth 5 | Set-Content $ConfigFile -Encoding UTF8
    Log "Created default config.json"
    return $DefaultConfig
}

# ── Лог ──────────────────────────────────────────────────────────────────────

function Log($msg) {
    $ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

# ── GLPI API ──────────────────────────────────────────────────────────────────

function GLPI-Get($url, $headers) {
    return Invoke-RestMethod -Uri $url -Headers $headers -Method Get -UseBasicParsing
}

function Init-Session($cfg) {
    $h = @{
        "Authorization" = "user_token $($cfg.glpi_user_token)"
        "App-Token"     = $cfg.glpi_app_token
    }
    $r = GLPI-Get "$($cfg.glpi_url)/initSession" $h
    return $r.session_token
}

function Kill-Session($cfg, $token) {
    try {
        $h = @{ "Session-Token" = $token; "App-Token" = $cfg.glpi_app_token }
        GLPI-Get "$($cfg.glpi_url)/killSession" $h | Out-Null
    } catch {}
}

function Get-MyUserId($cfg, $token) {
    $h = @{ "Session-Token" = $token; "App-Token" = $cfg.glpi_app_token }
    try {
        $r = GLPI-Get "$($cfg.glpi_url)/getFullSession" $h
        # GLPI возвращает данные в разных местах в зависимости от версии
        if ($r.glpiID)                          { Log "  user_id from glpiID: $($r.glpiID)"; return [int]$r.glpiID }
        if ($r.session -and $r.session.glpiID) { Log "  user_id from session.glpiID: $($r.session.glpiID)"; return [int]$r.session.glpiID }
        # Сохраняем ответ для диагностики
        $dump = $r | ConvertTo-Json -Depth 3 -Compress
        Log "  getFullSession response: $dump"
    } catch {
        Log "  getFullSession error: $_"
    }
    return $null
}

function Search-Tickets($cfg, $token, $params, $label) {
    $h = @{ "Session-Token" = $token; "App-Token" = $cfg.glpi_app_token }

    $fixed = @(
        "forcedisplay[0]=2",
        "forcedisplay[1]=1",
        "forcedisplay[2]=12",
        "forcedisplay[3]=10",
        "forcedisplay[4]=15",
        "forcedisplay[5]=19",
        "forcedisplay[6]=64",
        "forcedisplay[7]=83",
        "forcedisplay[8]=21",
        "range=0-200",
        "sort=19",
        "order=DESC"
    )
    # Delta: only tickets modified since last sync (minus 5 min buffer)
    $sinceFilter = @()
    $ls = [long]($cfg.last_sync)
    if ($ls -gt 0) {
        $epoch = [DateTime]::new(1970,1,1,0,0,0,[DateTimeKind]::Utc)
        $dt = $epoch.AddSeconds($ls - 300).ToString("yyyy-MM-dd HH:mm:ss")
        $sinceFilter = @(
            "criteria[1][field]=19",
            "criteria[1][link]=AND",
            "criteria[1][searchtype]=morethan",
            "criteria[1][value]=$dt"
        )
    }
    $qs = ($fixed + $params + $sinceFilter) -join "&"
    $url = "$($cfg.glpi_url)/search/Ticket?$qs"

    try {
        $r = GLPI-Get $url $h
        $rows = if ($r.data) { $r.data } else { @() }
        Log "  $label`: $($rows.Count) tickets"
        return $rows
    } catch {
        Log "  Search error ($label): $_"
        return @()
    }
}

function Get-Field($obj, $key, $default) {
    $prop = $obj.PSObject.Properties[$key]
    if ($prop -ne $null -and $prop.Value -ne $null -and "$($prop.Value)" -ne '') {
        return $prop.Value
    }
    return $default
}

function Parse-Ticket($row, $source) {
    return @{
        id        = [int](Get-Field $row '2'  0)
        name      = [string](Get-Field $row '1'  "")
        status    = [int](Get-Field $row '12' 0)
        priority  = [int](Get-Field $row '10' 0)
        date      = [string](Get-Field $row '15' "")
        date_mod  = [string](Get-Field $row '19' "")
        requester = [string](Get-Field $row '64' "")
        location  = [string](Get-Field $row '83' "")
        content   = [string](Get-Field $row '21' "")
        source    = $source
    }
}

# ── Синхронизация ─────────────────────────────────────────────────────────────

function Sync($cfg) {
    Log "=== Sync start ==="
    $token = $null
    try {
        $token  = Init-Session $cfg
        Log "GLPI: session opened"

        $myId   = Get-MyUserId $cfg $token
        Log "GLPI: user_id = $myId"

        $all    = @{}   # id -> ticket

        # Вспомогательная функция добавления в коллекцию (ключ — строка)
        function Add-Ticket($t) {
            if ($t.id -gt 0) {
                $key = "$($t.id)"
                if (-not $all.ContainsKey($key)) { $all[$key] = $t }
            }
        }

        # 1. Мои тикеты
        if ($myId) {
            $p = @(
                "criteria[0][field]=4",
                "criteria[0][link]=AND",
                "criteria[0][searchtype]=equals",
                "criteria[0][value]=$myId"
            )
            foreach ($row in (Search-Tickets $cfg $token $p "My tickets")) {
                try { Add-Ticket (Parse-Ticket $row "mine") } catch { Log "  Row parse error: $_" }
            }
        }

        # 2. Тикеты по локациям
        $locList = @($cfg.locations)   # гарантируем массив
        foreach ($loc in $locList) {
            # По полю Location (field 83)
            $p = @(
                "criteria[0][field]=83",
                "criteria[0][link]=AND",
                "criteria[0][searchtype]=contains",
                "criteria[0][value]=$loc"
            )
            foreach ($row in (Search-Tickets $cfg $token $p "Location $loc (field)")) {
                try { Add-Ticket (Parse-Ticket $row "$loc") } catch { Log "  Row parse error: $_" }
            }

            # По содержанию тикета (field 21)
            $p = @(
                "criteria[0][field]=21",
                "criteria[0][link]=AND",
                "criteria[0][searchtype]=contains",
                "criteria[0][value]=$loc"
            )
            foreach ($row in (Search-Tickets $cfg $token $p "Location $loc (content)")) {
                try { Add-Ticket (Parse-Ticket $row "$loc") } catch { Log "  Row parse error: $_" }
            }
        }

        $tickets = @($all.Values)
        Log "Total unique tickets: $($tickets.Count)"

        # Отправляем на панель
        $body = @{ tickets = $tickets } | ConvertTo-Json -Depth 5 -Compress
        $h    = @{
            "X-Api-Key"    = $cfg.panel_api_key
            "Content-Type" = "application/json"
        }
        $res = Invoke-RestMethod -Uri $cfg.panel_url -Method Post `
               -Headers $h -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -UseBasicParsing
        Log "Panel response: ok=$($res.ok) upserted=$($res.count)"

        # Save last_sync so next run fetches only changed tickets
        $epoch = [DateTime]::new(1970,1,1,0,0,0,[DateTimeKind]::Utc)
        $nowTs = [long]([DateTime]::UtcNow - $epoch).TotalSeconds
        $cfg.last_sync = $nowTs
        $cfg | ConvertTo-Json -Depth 5 | Set-Content $ConfigFile -Encoding UTF8
        Log "Delta sync enabled: next run fetches only tickets modified after $(([DateTime]::UtcNow).ToString('HH:mm:ss')) UTC"

    } catch {
        Log "SYNC ERROR: $_"
    } finally {
        if ($token) {
            Kill-Session $cfg $token
            Log "GLPI: session closed"
        }
    }
    Log "=== Sync done ===`n"
}

# ── Точка входа ───────────────────────────────────────────────────────────────

$Host.UI.RawUI.WindowTitle = "GLPI Agent"
Log "GLPI Agent started"

$cfg = Load-Config
Log "Interval: $($cfg.interval_sec) sec ($([int]($cfg.interval_sec/60)) min)"
Log "Locations: $($cfg.locations -join ', ')"
Log "Panel: $($cfg.panel_url)"

Sync $cfg

while ($true) {
    $waited = 0
    $flagUrl = $cfg.panel_url -replace 'api/sync', 'api/resync-flag'
    $flagHeaders = @{ "X-Api-Key" = $cfg.panel_api_key }
    while ($waited -lt $cfg.interval_sec) {
        $sleep = [Math]::Min(60, $cfg.interval_sec - $waited)
        Start-Sleep -Seconds $sleep
        $waited += $sleep
        try {
            $chk = Invoke-RestMethod -Uri $flagUrl -Headers $flagHeaders -UseBasicParsing
            if ($chk.requested -eq $true) { Log "Force sync from panel"; break }
        } catch {}
    }
    $cfg = Load-Config
    Sync $cfg
}
