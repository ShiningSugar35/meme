# ============================================================
# Solana Meme Trading Bot - Simulation Test Script
# Demonstrates complete filtering logic inline with 思路.md
# ============================================================
# Usage: .\scripts\test_sim_chain.ps1
# Prerequisites: backend running on localhost:8000 (for the full system)
#   This script ALSO demonstrates the logic standalone via GMGN API.
# ============================================================

$ErrorActionPreference = "Continue"
$apiKey = "gmgn_524c209a2bd95e1d703c415688c725fd"
$baseUrl = "https://openapi.gmgn.ai"
$Headers = @{ "X-APIKEY" = $apiKey; "Content-Type" = "application/json" }

function Get-UnixTimestamp {
    return [int]((Get-Date).ToUniversalTime() - (Get-Date "1970-01-01")).TotalSeconds
}

# ---------- strategy groups from 思路.md ----------
$strategies = @(
    @{ name = "组1: x=0.15 y=2.25 t=0s";   x = 0.15; y = 2.25; t = 0 },
    @{ name = "组2: x=0.20 y=2.75 t=60s";  x = 0.20; y = 2.75; t = 60 }
)

# ---------- platform whitelist & burn/creator values ----------
$PLATFORMS = @("Pump.fun","Moonshot","moonshot_app","letsbonk","memoo","token_mill","jup_studio","bags","believe","heaven",
               "pump_mayhem","pump_mayhem_agent","pump_agent","bonkers","bankr","liquid","heaven","sugar","trendsfun","trends_fun")
$BURN_VALUES = @("burn","burned","burnt","true","1","yes")
$CREATOR_CLOSE = @("creator_close","close","closed","creator_closed")

function ToFloat($v) {
    if ($v -eq $null -or $v -eq "") { return $null }
    try { return [double]$v } catch { return $null }
}
function ToBool($v) {
    if ($v -eq $null) { return $null }
    if ($v -is [string] -and $v.Trim() -eq "") { return $null }
    if ($v -is [bool]) { if ($v) { return 1 } else { return 0 } }
    if ($v -is [int] -or $v -is [double] -or $v -is [long]) { if ($v -eq 0) { return 0 } else { return 1 } }
    $s = "$v".Trim().ToLower()
    if ($s -eq "" -or $s -eq "none" -or $s -eq "null") { return $null }
    if ($s -in @("1","true","yes","y","renounced","burn","burned")) { return 1 }
    if ($s -in @("0","false","no","n","open","not_renounced")) { return 0 }
    return $null
}

function ToHashtable($obj) {
    $h = @{}
    if ($obj -is [System.Collections.IDictionary]) { $obj.Keys | ForEach-Object { $h[$_] = $obj[$_] } }
    elseif ($obj.PSObject) { $obj.PSObject.Properties | ForEach-Object { $h[$_.Name] = $_.Value } }
    return $h
}

# ============================================================
function Get-Trenches {
    param($tSeconds, $limit = 80)
    $uuid = [guid]::NewGuid().ToString()
    $ts = Get-UnixTimestamp
    $body = @{
        version = "v2"
        new_creation = @{
            filters = @("offchain", "onchain")
            launchpad_platform = $PLATFORMS
            quote_address_type = @(4,5,3,1,13,0)
            launchpad_platform_v2 = $true
            limit = $limit
            min_created = "${tSeconds}s"
            max_created = "$($tSeconds + 60)s"
        }
    }
    $uri = "$baseUrl/v1/trenches?timestamp=$ts&client_id=$uuid&chain=sol"
    try {
        $resp = Invoke-RestMethod -Uri $uri -Method Post -Headers $Headers -Body (ConvertTo-Json $body -Depth 10 -Compress) -TimeoutSec 15
        return $resp
    } catch {
        Write-Host "Trenches API error: $_" -ForegroundColor Red
        return $null
    }
}

function Get-TokenKline {
    param($address, $resolution = "1m", $limit = 5)
    $uuid = [guid]::NewGuid().ToString()
    $ts = Get-UnixTimestamp
    $uri = "$baseUrl/v1/market/token_kline?timestamp=$ts&client_id=$uuid&chain=sol&address=$address&resolution=$resolution&limit=$limit"
    try {
        $resp = Invoke-RestMethod -Uri $uri -Method Get -Headers $Headers -TimeoutSec 10
        return $resp.data.list
    } catch {
        return @()
    }
}

function Get-TokenHolders {
    param($address, $limit = 20)
    $uuid = [guid]::NewGuid().ToString()
    $ts = Get-UnixTimestamp
    $uri = "$baseUrl/v1/market/token_top_holders?timestamp=$ts&client_id=$uuid&chain=sol&address=$address&limit=$limit"
    try {
        $resp = Invoke-RestMethod -Uri $uri -Method Get -Headers $Headers -TimeoutSec 10
        return $resp.data.list
    } catch {
        return @()
    }
}

function Get-TokenInfo {
    param($address)
    $uuid = [guid]::NewGuid().ToString()
    $ts = Get-UnixTimestamp
    $uri = "$baseUrl/v1/token/info?timestamp=$ts&client_id=$uuid&chain=sol&address=$address"
    try {
        $resp = Invoke-RestMethod -Uri $uri -Method Get -Headers $Headers -TimeoutSec 10
        $info = $resp.data
        $price = if ($info.price -and $info.price.price) { ToFloat $info.price.price } else { $null }
        return @{ price_usd = $price; liquidity = (ToFloat $info.liquidity) }
    } catch {
        return @{ price_usd = $null; liquidity = 0 }
    }
}

# ============================================================
# 1. INITIAL FILTER (matches filters.py logic exactly)
# ============================================================
function Run-InitialFilter {
    param($token, $strat)
    $x = $strat.x
    $fails = @()

    # type
    $typ = [string]$token.type
    if ($typ -ne "new_creation") { $fails += "type!=new_creation" }

    # liquidity
    $liq = ToFloat $token.liquidity_usd
    $minLiq = 10000 - 20000 * $x
    if ($liq -eq $null -or $liq -lt $minLiq) { $fails += "liquidity($liq) < $minLiq" }

    # top10 holder rate
    $t10 = ToFloat $token.top_10_holder_rate
    $lo = 0.175 - 0.15 * $x; $hi = 0.25 + 0.25 * $x
    if ($t10 -eq $null -or $t10 -le $lo -or $t10 -ge $hi) { $fails += "top10_rate($t10) not in ($lo,$hi)" }

    # renounced mint & freeze
    if ((ToBool $token.renounced_mint) -ne 1) { $fails += "renounced_mint!=1" }
    if ((ToBool $token.renounced_freeze_account) -ne 1) { $fails += "renounced_freeze!=1" }

    # rug / entrapment / rat / bundler
    foreach ($kv in @(
        @{k="rug_ratio"; n="rug"; threshold=-0.05+$x},
        @{k="entrapment_ratio"; n="entrapment"; threshold=-0.05+$x},
        @{k="rat_trader_amount_rate"; n="rat_trader"; threshold=-0.05+$x},
        @{k="bundler_rate"; n="bundler"; threshold=-0.05+$x},
        @{k="bundler_trader_amount_rate"; n="bundler2"; threshold=-0.05+$x}
    )) {
        $v = ToFloat $token[$kv.k]
        if ($v -ne $null) { if ($v -ge $kv.threshold) { $fails += "$($kv.n)=$v >= $($kv.threshold)" } }
    }

    # wash trading
    if ((ToBool $token.is_wash_trading) -ne 0) { $fails += "is_wash_trading" }

    # insider hold
    $ins = ToFloat $token.suspected_insider_hold_rate; if ($ins -ne $null -and $ins -ge $x) { $fails += "suspected_insider=$ins >= $x" }

    # fresh wallet
    $fw = ToFloat $token.fresh_wallet_rate; if ($fw -ne $null -and $fw -ge (0.13+0.1*$x)) { $fails += "fresh_wallet=$fw >= $((0.13+0.1*$x).ToString('F4'))" }

    # sell_tax (with >1 normalization)
    $st = ToFloat $token.sell_tax
    if ($st -ne $null) { if ($st -gt 1) { $st = $st / 100.0 }; if ($st -ge (0.1*$x)) { $fails += "sell_tax=$st >= $((0.1*$x).ToString('F4'))" } }

    # social (only enforced when x<0.15)
    if ($x -lt 0.15) {
        $social = ToBool $token.has_at_least_one_social
        if ($social -ne 1) { $fails += "no_social (required when x<0.15)" }
    }

    # creator/dev
    $cs = "$($token.creator_token_status)".ToLower()
    $dev = ToFloat $token.dev_team_hold_rate
    $devTh = 0.03 + 0.1 * $x
    if ($cs -notin $CREATOR_CLOSE -and ($dev -eq $null -or $dev -ge $devTh)) { $fails += "creator($cs) dev_hold($dev)>=$devTh" }

    # burn
    $bs = "$($token.burn_status)".ToLower()
    if ($bs -notin $BURN_VALUES) { $fails += "burn($bs)" }

    # sniper count
    $sn = ToFloat $token.sniper_count; if ($sn -ne $null -and $sn -ge (50*$x)) { $fails += "sniper=$sn >= $((50*$x).ToString())" }

    # platform
    $plat = "$($token.launchpad_platform)".ToLower()
    $match = $false; foreach ($p in $PLATFORMS) { if ($plat -eq $p.ToLower()) { $match = $true; break } }
    if (-not $match) { $fails += "platform($plat) not in whitelist" }

    return @{ passed = ($fails.Count -eq 0); fails = $fails }
}

# ============================================================
# 2. SECOND FILTER (matches second_filter.py logic exactly)
# ============================================================
function Run-SecondFilter {
    param($token, $strat, $klines, $liqUsd)

    $y = $strat.y
    $fails = @()
    $curPrice = ToFloat $token.latest_price_usd

    if ($curPrice -eq $null -or $curPrice -le 0) { return @{ passed = $false; fails = @("no_current_price") } }

    # 5m high/low from klines + current price
    $highs = @(); $lows = @()
    foreach ($k in $klines) {
        $h = ToFloat $k.high; $l = ToFloat $k.low
        if ($h) { $highs += $h }; if ($l) { $lows += $l }
    }
    $highs += $curPrice; $lows += $curPrice
    if ($highs.Count -eq 0 -or $lows.Count -eq 0) { return @{ passed = $false; fails = @("no_5m_range") } }
    $hi5 = ($highs | Measure-Object -Maximum).Maximum
    $lo5 = ($lows | Measure-Object -Minimum).Minimum
    if ($hi5 -le $lo5) { return @{ passed = $false; fails = @("high<=low") } }

    # latest 1m candle data
    $lk = if ($klines.Count -gt 0) { $klines[-1] } else { $null }
    $open1 = ToFloat ($lk.open); $close1 = ToFloat ($lk.close)
    $hi1 = if ($lk.high) { ToFloat $lk.high } else { $close1 }
    $lo1 = if ($lk.low) { ToFloat $lk.low } else { $close1 }
    $vol1 = ToFloat ($lk.volume)

    # rule 1: volume_1m > max(liquidity*(0.07-0.02*y), median_prev*(1.3-0.1*y))
    $prevVols = @()
    for ($i = 0; $i -lt [Math]::Max(0, $klines.Count-1); $i++) {
        $pv = ToFloat $klines[$i].volume
        if ($pv -and $pv -gt 0) { $prevVols += $pv }
    }
    $med = if ($prevVols.Count -gt 0) { ($prevVols | Sort-Object)[[int]($prevVols.Count/2)] } else { 0 }
    $liqVal = 0; if ($liqUsd) { $liqVal = $liqUsd }
    $tA = $liqVal * (0.07 - 0.02 * $y)
    $tB = $med * (1.3 - 0.1 * $y)
    $th1 = if ($tA -gt $tB) { $tA } else { $tB }
    if ($vol1 -le $th1) { $fails += "vol_1m($vol1) <= max($tA,$tB)" }

    # rule 2: close_1m > open_1m * (1 - 0.002*y)
    if ($open1 -and $close1) {
        if ($close1 -le $open1 * (1 - 0.002*$y)) { $fails += "close($close1) <= open($open1)*{0}" -f ((1-0.002*$y).ToString("F4")) }
    } else { $fails += "no open/close 1m" }

    # rule 3: candle_ratio > 0.80 - 0.01*y
    if ($hi1 -and $lo1 -and $hi1 -gt $lo1 -and $close1) {
        $cr = ($close1 - $lo1) / ($hi1 - $lo1)
        if ($cr -le (0.80 - 0.01*$y)) { $fails += "candle_ratio($cr) <= $((0.80-0.01*$y).ToString('F4'))" }
    } else { $fails += "no candle range" }

    # rule 4: current > high_5m / y
    if ($curPrice -le ($hi5 / $y)) { $fails += "cur($curPrice) <= high5m/y($(($hi5/$y).ToString('F6')))" }

    # rule 5: current < low_5m * y
    if ($curPrice -ge ($lo5 * $y)) { $fails += "cur($curPrice) >= low5m*y($(($lo5*$y).ToString('F6')))" }

    # rule 6: 0.8-0.2*y < frac < 0.35+0.2*y
    $frac = ($curPrice - $lo5) / ($hi5 - $lo5)
    $lf = 0.8 - 0.2 * $y; $hf = 0.35 + 0.2 * $y
    if ($frac -le $lf -or $frac -ge $hf) { $fails += "frac($frac) not in ($lf,$hf)" }

    return @{ passed = ($fails.Count -eq 0); fails = $fails }
}

# ============================================================
# 3. Top1 holder check
# ============================================================
function Check-Top1Holder {
    param($address, $x)
    $holders = Get-TokenHolders -address $address
    foreach ($h in $holders) {
        if ($h.addr_type -eq 0) {
            $rate = ToFloat ($h.amount_percentage -or $h.amount_cur)
            $th = 0.048 + 0.01 * $x
            if ($rate -eq $null -or $rate -lt $th) { return $true }
            return $false
        }
    }
    return $true  # no normal wallet found = pass
}

# ============================================================
# 4. Position sizing
# ============================================================
function Calc-Size {
    param($liqUsd, $maxEntryUsd = 200)
    $pct = 0.015
    $size = [Math]::Floor([Math]::Min($liqUsd * $pct, $maxEntryUsd) * 100) / 100
    return [Math]::Max($size, 0)
}

# ============================================================
# MAIN
# ============================================================
Write-Host "============= Meme Trading Bot - Sim Test =============" -ForegroundColor Cyan
Write-Host "API Key: $($apiKey.Substring(0,8))..." -ForegroundColor Gray
Write-Host ""

foreach ($strat in $strategies) {
    Write-Host ("-" * 60)
    Write-Host "Strategy: $($strat.name)" -ForegroundColor Yellow
    Write-Host "  x=$($strat.x)  y=$($strat.y)  t=$($strat.t)s" -ForegroundColor Gray
    Write-Host ""

    Write-Host "[1] Fetching trenches (age $($strat.t)-$($strat.t+60)s)..." -ForegroundColor Green
    $trenchesResp = Get-Trenches -tSeconds $strat.t
    $items = @()
    if ($trenchesResp.data) {
        $raw = $trenchesResp.data
        # v2 response: data.new_creation, data.pump, data.completed
        $items = @()
        foreach ($k in @('new_creation','pump','completed')) {
            $arr = $raw.$k
            if ($arr -is [array]) { $items += $arr }
        }
    }
    Write-Host "  Got $($items.Count) trench items" -ForegroundColor Gray

    # inject synthetic passing token for demo (real tokens rarely pass all filters)
    $synth = @{
        token_mint = "DEMO_PASS_TOKEN"; address = "DEMO_PASS_TOKEN"; symbol = "DEMOPASS"
        name = "Demo Passing Token"; type = "new_creation"; liquidity = 25000.0
        liquidity_usd = 25000.0; top_10_holder_rate = 0.20
        renounced_mint = 1; renounced_freeze_account = 1
        rug_ratio = 0.01; entrapment_ratio = 0.01; is_wash_trading = 0
        rat_trader_amount_rate = 0.0; bundler_trader_amount_rate = 0.0; bundler_rate = 0.0
        suspected_insider_hold_rate = 0.0; fresh_wallet_rate = 0.05; sell_tax = 0.0
        has_at_least_one_social = 1; creator_token_status = "creator_close"
        dev_team_hold_rate = 0.0; burn_status = "burn"; sniper_count = 3
        launchpad_platform = "Pump.fun"; latest_price_usd = 0.000085
        market_cap = 85000.0; total_supply = 1000000000
    }
    $items += $synth
    Write-Host "  +1 synthetic token for full-chain demo" -ForegroundColor Magenta

    if ($items.Count -eq 0) { Write-Host "  No tokens found." -ForegroundColor Red; continue }

    Write-Host "[2] Running initial filter..." -ForegroundColor Green
    $passedInit = @()
    foreach ($item in $items) {
        $ht = ToHashtable $item
        $ht.token_mint = $ht.address
        $ht.type = "new_creation"
        if (-not $ht.liquidity_usd) { $ht.liquidity_usd = if ($ht.liquidity) { ToFloat $ht.liquidity } else { 0 } }
        if (-not $ht.latest_price_usd) { $ht.latest_price_usd = if ($ht.price) { ToFloat $ht.price } else { 0 } }
        $res = Run-InitialFilter -token $ht -strat $strat
        if ($res.passed) {
            $passedInit += $ht
            Write-Host "  PASS: $($ht.symbol) ($($ht.address.Substring(0,8))...)" -ForegroundColor Cyan
        } else {
            Write-Host "  FAIL: $($ht.symbol): $($res.fails -join '; ')" -ForegroundColor DarkGray
        }
    }
    if ($passedInit.Count -eq 0) { Write-Host "  No initial filter passes." -ForegroundColor Red; continue }

    Write-Host "  --> $($passedInit.Count) tokens passed initial filter" -ForegroundColor Cyan
    Write-Host ""

    Write-Host "[3] Second filter + top1..." -ForegroundColor Green
    $final = @()
    foreach ($item in $passedInit) {
        $addr = $item.address
        $liq = if ($item.liquidity) { ToFloat $item.liquidity } else { 0 }
        Write-Host "  Token: $($item.symbol) (liq=`$$liq)" -ForegroundColor Gray

        # fetch price from token/info (skip for synthetic token)
        $price = $item.latest_price_usd
        if ($item.token_mint -eq "DEMO_PASS_TOKEN") {
            $price = 0.000085
            # generate synthetic klines for full-chain demo (engineered to pass all 7 rules)
            $base = 0.000080
            $price = 0.000085
            $klines = @(
                @{ open = $base * 0.98; close = $base; high = $base * 1.01; low = $base * 0.97; volume = 15000.0; time = (Get-UnixTimestamp - 540) * 1000 }
                @{ open = $base; close = $base * 1.02; high = $base * 1.03; low = $base * 0.99; volume = 22000.0; time = (Get-UnixTimestamp - 480) * 1000 }
                @{ open = $base * 1.02; close = $base * 1.04; high = $base * 1.05; low = $base * 1.01; volume = 18000.0; time = (Get-UnixTimestamp - 420) * 1000 }
                @{ open = $base * 1.04; close = $base * 1.06; high = $base * 1.07; low = $base * 1.03; volume = 21000.0; time = (Get-UnixTimestamp - 360) * 1000 }
                @{ open = $base * 1.06; close = $price; high = $price * 1.03; low = $price * 0.85; volume = 28000.0; time = (Get-UnixTimestamp - 300) * 1000 }
            )
        } else {
            $info = Get-TokenInfo -address $addr
            $price = $info.price_usd
            $klines = Get-TokenKline -address $addr
        }
        if ($klines.Count -lt 2) {
            Write-Host "    SKIP: not enough klines ($($klines.Count))" -ForegroundColor DarkGray
            continue
        }
        if (-not $price -or $price -le 0) {
            Write-Host "    SKIP: no price data" -ForegroundColor DarkGray
            continue
        }
        $res2 = Run-SecondFilter -token (@{ latest_price_usd = $price }) -strat $strat -klines $klines -liqUsd $liq
        if (-not $res2.passed) {
            Write-Host "    SECOND FAIL: $($res2.fails -join '; ')" -ForegroundColor DarkGray
            continue
        }

        # top1 holder check (skip for synthetic demo token)
        if ($addr -ne "DEMO_PASS_TOKEN" -and -not (Check-Top1Holder -address $addr -x $strat.x)) {
            Write-Host "    TOP1 FAIL" -ForegroundColor DarkGray
            continue
        }
        $final += $item
        Write-Host "    ALL PASS!" -ForegroundColor Green
    }

    if ($final.Count -eq 0) { Write-Host "  No tokens passed full screening." -ForegroundColor Red; continue }

    Write-Host ""
    Write-Host "[4] Simulated entries..." -ForegroundColor Green
    foreach ($item in $final) {
        $liq = if ($item.liquidity) { ToFloat $item.liquidity } else { 0 }
        $price = if ($item.latest_price_usd) { $item.latest_price_usd } else { 0 }
        if ($price -le 0) {
            $info = Get-TokenInfo -address $item.address
            $price = if ($info.price_usd) { $info.price_usd } else { 0 }
        }
        $entrySize = Calc-Size -liqUsd $liq
        $tokensBought = if ($price -and $price -gt 0) { $entrySize / $price } else { 0 }
        Write-Host "  BUY: $($item.symbol)" -ForegroundColor Magenta
        Write-Host "    Entry: `$$entrySize  |  Liquidity: `$$liq  |  Price: `$$price  |  Tokens: $([Math]::Round($tokensBought, 0))" -ForegroundColor Gray

        # Simulate exit scenarios
        Write-Host "    ---- Exit scenarios ----" -ForegroundColor DarkYellow
        $scenarios = @(
            @{ name="+80% TP 50%"; mult=1.80; sellPct=0.5; feePct=0.005 },
            @{ name="+150% TP 50% more"; mult=2.50; sellPct=0.5; feePct=0.005 },
            @{ name="+210% TP all"; mult=3.10; sellPct=1.0; feePct=0.005 },
            @{ name="-20% SL 50%"; mult=0.80; sellPct=0.5; feePct=0.005 },
            @{ name="-40% SL remaining all"; mult=0.60; sellPct=1.0; feePct=0.005 }
        )
        $remainingTokens = $tokensBought
        $totalProfit = 0.0
        foreach ($sc in $scenarios) {
            if ($remainingTokens -le 0) { break }
            $sellTokens = $remainingTokens * $sc.sellPct
            $sellPrice = $price * $sc.mult
            $revenue = $sellTokens * $sellPrice
            $fee = $revenue * $sc.feePct
            $costBasis = $sellTokens * $price
            $pnl = $revenue - $fee - $costBasis
            $totalProfit += $pnl
            $remainingTokens -= $sellTokens
            Write-Host ("      {0,-28} sold {1,10:N0} @ {2,10:F6} | PnL: {3,10:F2}" -f $sc.name, $sellTokens, $sellPrice, $pnl)
        }
        $roi = if ($entrySize -gt 0) { $totalProfit / $entrySize * 100 } else { 0 }
        Write-Host ("    ---> TOTAL PnL: `$$([Math]::Round($totalProfit,2))  (ROI: $([Math]::Round($roi,1))%)" -f $sc.name) -ForegroundColor Yellow
        Write-Host ""
    }
}

Write-Host "============= Done =============" -ForegroundColor Cyan
