"""
DOGE Scalper Test — Hyperliquid
Compra y vende DOGE cada 5 minutos durante 1 hora
12 ciclos máximo | DRY RUN por defecto
Precios: Kraken (sin KYC, sin restricciones)
"""

import os, time, json, logging, re, threading, requests
import pandas as pd
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
from eth_account import Account

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("DOGEBot")

# ─── Config ──────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]
PRIVATE_KEY       = os.environ.get("PRIVATE_KEY", "")
DRY_RUN           = os.environ.get("DRY_RUN", "true").lower() == "true"
CAPITAL_USDT      = float(os.environ.get("CAPITAL_USDT", "10"))   # capital por ciclo
CYCLE_MINUTES     = int(os.environ.get("CYCLE_MINUTES", "5"))      # minutos entre compra/venta
HOLD_SECONDS      = int(os.environ.get("HOLD_SECONDS", "120"))     # segundos aguantando posición
TOTAL_HOURS       = float(os.environ.get("TOTAL_HOURS", "1"))      # duración total

MAX_CYCLES        = int((TOTAL_HOURS * 60) / CYCLE_MINUTES)        # 12 ciclos en 1 hora

HL_API  = "https://api.hyperliquid.xyz/exchange"
HL_INFO = "https://api.hyperliquid.xyz/info"

if PRIVATE_KEY:
    account = Account.from_key(PRIVATE_KEY)
    WALLET_ADDRESS = account.address
else:
    WALLET_ADDRESS = ""

# ─── Estado ──────────────────────────────────────────────────────────────────────
state = {
    "status":       "esperando",
    "cycle":        0,
    "max_cycles":   MAX_CYCLES,
    "trades":       [],
    "pnl_total":    0.0,
    "balance":      CAPITAL_USDT,
    "current_price":None,
    "buy_price":    None,
    "phase":        "idle",   # idle | holding | sold
    "last_update":  None,
    "finished":     False,
    "wins":         0,
    "losses":       0,
}

# ─── Telegram ────────────────────────────────────────────────────────────────────
def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram: {e}")

# ─── Precio DOGE desde Kraken ────────────────────────────────────────────────────
def get_doge_price() -> float:
    try:
        url = "https://api.kraken.com/0/public/Ticker?pair=XDGUSD"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        pair_key = list(data["result"].keys())[0]
        price = float(data["result"][pair_key]["c"][0])  # último precio
        return round(price, 6)
    except Exception as e:
        log.error(f"Precio DOGE error: {e}")
        return None

# ─── Hyperliquid orden (real) ─────────────────────────────────────────────────────
def hl_place_order(is_buy: bool, qty: float, price: float, reduce_only: bool = False) -> bool:
    try:
        import time as _time
        from eth_account.messages import encode_defunct
        nonce = int(_time.time() * 1000)
        coin_index = 6  # DOGE en Hyperliquid
        order = {
            "a": coin_index, "b": is_buy,
            "p": str(round(price * (1.005 if is_buy else 0.995), 6)),
            "s": str(qty), "r": reduce_only,
            "t": {"limit": {"tif": "Ioc"}}
        }
        action = {"type": "order", "orders": [order], "grouping": "na"}
        msg_str = json.dumps({"action": action, "nonce": nonce}, separators=(",", ":"))
        msg = encode_defunct(text=msg_str)
        signed = account.sign_message(msg)
        payload = {
            "action": action, "nonce": nonce,
            "signature": {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v},
            "vaultAddress": None,
        }
        resp = requests.post(HL_API, json=payload, timeout=15)
        result = resp.json()
        return result.get("status") == "ok"
    except Exception as e:
        log.error(f"HL order error: {e}")
        return False

# ─── Ciclo de scalping ────────────────────────────────────────────────────────────
def run_cycle(cycle_num: int):
    dry = "🧪 [SIM] " if DRY_RUN else ""

    # 1. Obtener precio actual
    price = get_doge_price()
    if not price:
        log.error("No se pudo obtener precio DOGE, skip ciclo")
        return

    state["current_price"] = price
    qty = round(CAPITAL_USDT / price, 0)  # cantidad DOGE a comprar

    log.info(f"Ciclo {cycle_num}/{MAX_CYCLES} | DOGE: ${price} | Qty: {qty}")

    # 2. COMPRA
    state["phase"] = "holding"
    state["buy_price"] = price
    buy_time = datetime.now(timezone.utc).strftime("%H:%M:%S")

    if DRY_RUN:
        log.info(f"[DRY] 🟢 COMPRA {qty} DOGE @ {price}")
        ok_buy = True
    else:
        ok_buy = hl_place_order(is_buy=True, qty=qty, price=price)

    if not ok_buy:
        log.error("Compra fallida, skip ciclo")
        state["phase"] = "idle"
        return

    send_telegram(
        f"{dry}🟢 <b>DOGE COMPRA #{cycle_num}</b>\n"
        f"💰 Precio: <b>${price}</b>\n"
        f"📦 Cantidad: {qty} DOGE\n"
        f"⏱ {buy_time} UTC\n"
        f"🔄 Ciclo {cycle_num}/{MAX_CYCLES}"
    )

    # 3. ESPERAR (hold)
    log.info(f"Aguantando {HOLD_SECONDS}s...")
    time.sleep(HOLD_SECONDS)

    # 4. Precio al vender
    sell_price = get_doge_price() or price
    state["current_price"] = sell_price
    qty_sell = qty

    # 5. VENTA
    state["phase"] = "sold"
    sell_time = datetime.now(timezone.utc).strftime("%H:%M:%S")

    if DRY_RUN:
        log.info(f"[DRY] 🔴 VENTA {qty_sell} DOGE @ {sell_price}")
        ok_sell = True
    else:
        ok_sell = hl_place_order(is_buy=False, qty=qty_sell, price=sell_price, reduce_only=True)

    pnl     = (sell_price - price) * qty_sell
    pnl_pct = (sell_price - price) / price * 100
    state["pnl_total"] += pnl
    state["balance"]   += pnl

    if pnl > 0:
        state["wins"] += 1
    else:
        state["losses"] += 1

    trade = {
        "cycle":     cycle_num,
        "buy":       price,
        "sell":      sell_price,
        "qty":       qty_sell,
        "pnl":       round(pnl, 4),
        "pnl_pct":   round(pnl_pct, 4),
        "buy_time":  buy_time,
        "sell_time": sell_time,
    }
    state["trades"].append(trade)
    state["phase"] = "idle"

    emoji = "✅" if pnl > 0 else "❌"
    send_telegram(
        f"{dry}{emoji} <b>DOGE VENTA #{cycle_num}</b>\n"
        f"💰 Compra: ${price} → Venta: ${sell_price}\n"
        f"📊 P&L: <b>{pnl:+.4f} USDT ({pnl_pct:+.4f}%)</b>\n"
        f"💼 Balance: {state['balance']:.4f} USDT\n"
        f"⏱ {sell_time} UTC"
    )

# ─── Loop principal ───────────────────────────────────────────────────────────────
def bot_loop():
    log.info("🚀 DOGE Scalper iniciado")
    dry = "🧪 MODO SIMULACIÓN" if DRY_RUN else "🔴 MODO REAL"

    send_telegram(
        f"🐕 <b>DOGE Scalper iniciado</b>\n"
        f"💼 Capital por ciclo: {CAPITAL_USDT} USDT\n"
        f"🔄 Ciclos: {MAX_CYCLES} (cada {CYCLE_MINUTES} min)\n"
        f"⏱ Duración: {TOTAL_HOURS} hora(s)\n"
        f"⏳ Hold por ciclo: {HOLD_SECONDS}s\n"
        f"{dry}"
    )

    state["status"] = "running"
    start_time = time.time()

    for cycle in range(1, MAX_CYCLES + 1):
        state["cycle"] = cycle
        state["last_update"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        run_cycle(cycle)

        # Esperar hasta el próximo ciclo (descontando el tiempo de hold)
        if cycle < MAX_CYCLES:
            wait = (CYCLE_MINUTES * 60) - HOLD_SECONDS
            if wait > 0:
                log.info(f"Esperando {wait}s para próximo ciclo...")
                time.sleep(wait)

    # ─── Resumen final ────────────────────────────────────────────────────────────
    state["status"]   = "finished"
    state["finished"] = True

    total_pnl = state["pnl_total"]
    wins      = state["wins"]
    losses    = state["losses"]
    win_rate  = round(wins / MAX_CYCLES * 100, 1) if MAX_CYCLES > 0 else 0
    elapsed   = round((time.time() - start_time) / 60, 1)

    best  = max(state["trades"], key=lambda t: t["pnl"]) if state["trades"] else None
    worst = min(state["trades"], key=lambda t: t["pnl"]) if state["trades"] else None

    dry = "🧪 [SIMULADO] " if DRY_RUN else ""
    summary = (
        f"{dry}📊 <b>RESUMEN DOGE Scalper</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⏱ Duración: {elapsed} min\n"
        f"🔄 Ciclos: {MAX_CYCLES}\n"
        f"✅ Wins: {wins} | ❌ Losses: {losses}\n"
        f"🎯 Win rate: {win_rate}%\n"
        f"💰 P&L total: <b>{total_pnl:+.4f} USDT</b>\n"
        f"💼 Balance final: {state['balance']:.4f} USDT\n"
    )
    if best:
        summary += f"🏆 Mejor trade: +{best['pnl']} USDT (ciclo {best['cycle']})\n"
    if worst:
        summary += f"💔 Peor trade: {worst['pnl']} USDT (ciclo {worst['cycle']})\n"

    send_telegram(summary)
    log.info("✅ DOGE Scalper finalizado")

# ─── Flask Dashboard ──────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/api/state")
def api_state():
    return jsonify(state)

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DOGE Scalper</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0806;--surface:#110e0a;--border:#2a1f10;--gold:#ffd700;--green:#00ff88;--red:#ff3355;--muted:#5a4a35;--text:#f0e6d3;--mono:'Share Tech Mono',monospace;--display:'Syne',sans-serif}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--mono);min-height:100vh}
header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:linear-gradient(180deg,#1a0f06,var(--bg))}
.logo{font-family:var(--display);font-weight:800;font-size:1.2rem;color:var(--gold)}
.pill{display:flex;align-items:center;gap:6px;font-size:.68rem;color:var(--muted);background:var(--surface);padding:5px 12px;border-radius:20px;border:1px solid var(--border)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;box-shadow:0 0 6px var(--green)}50%{opacity:.4}}
.top{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:1px;background:var(--border);border-bottom:1px solid var(--border)}
.metric{background:var(--surface);padding:16px;position:relative}
.metric::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--gold);opacity:.3}
.ml{font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:5px}
.mv{font-size:1.4rem;font-weight:bold;font-family:var(--display);line-height:1}
.ms{font-size:.6rem;color:var(--muted);margin-top:3px}
.gold{color:var(--gold)}.green{color:var(--green)}.red{color:var(--red)}
.main{display:grid;grid-template-columns:1fr 280px;gap:1px;background:var(--border);min-height:calc(100vh - 140px)}
.left{background:var(--surface);padding:20px}
.right{background:var(--surface);padding:16px;display:flex;flex-direction:column;gap:12px}
.sec{font-size:.58rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}

/* Progress bar */
.progress-wrap{background:var(--border);border-radius:4px;height:8px;margin:8px 0 16px;overflow:hidden}
.progress-fill{height:100%;background:var(--gold);border-radius:4px;transition:width .5s}

/* Phase indicator */
.phase-box{background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:16px;text-align:center}
.phase-val{font-family:var(--display);font-size:1.6rem;font-weight:800}
.phase-sub{font-size:.65rem;color:var(--muted);margin-top:6px}

/* Trades */
.trade-list{background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:14px;flex:1;overflow-y:auto}
.trade-item{padding:8px 0;border-bottom:1px solid var(--border);font-size:.68rem}
.trade-item:last-child{border-bottom:none}
.trade-row{display:flex;justify-content:space-between}
.trade-sub{color:var(--muted);margin-top:2px}

/* Stats */
.stats-box{background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:14px}
.stat-row{display:flex;justify-content:space-between;font-size:.7rem;padding:4px 0;border-bottom:1px solid var(--border)}
.stat-row:last-child{border-bottom:none}
.sk{color:var(--muted)}

/* Finished banner */
.fin-banner{background:rgba(255,215,0,.08);border:1px solid rgba(255,215,0,.3);border-radius:4px;padding:16px;text-align:center;display:none}
.fin-title{font-family:var(--display);font-size:1.3rem;font-weight:800;color:var(--gold)}
.fin-pnl{font-size:1.8rem;font-family:var(--display);font-weight:800;margin:8px 0}

.dry{background:rgba(255,215,0,.1);border:1px solid rgba(255,215,0,.3);color:var(--gold);font-size:.6rem;padding:3px 8px;border-radius:3px}
.upd{text-align:center;font-size:.56rem;color:var(--muted);padding:5px}
@media(max-width:600px){.main{grid-template-columns:1fr}.mv{font-size:1.1rem}}
</style>
</head>
<body>
<header>
  <div class="logo">🐕 DOGE Scalper // 1h Test</div>
  <div style="display:flex;gap:7px;align-items:center">
    <span class="dry">🧪 DRY RUN</span>
    <div class="pill"><div class="dot" id="dot"></div><span id="st">Conectando...</span></div>
  </div>
</header>

<div class="top">
  <div class="metric"><div class="ml">Precio DOGE</div><div class="mv gold" id="price">—</div><div class="ms">USDT</div></div>
  <div class="metric"><div class="ml">Ciclo Actual</div><div class="mv" id="cycle">—</div><div class="ms" id="cycleMax">de — total</div></div>
  <div class="metric"><div class="ml">P&L Total</div><div class="mv" id="pnl">—</div><div class="ms">USDT simulado</div></div>
  <div class="metric"><div class="ml">Win Rate</div><div class="mv" id="wr">—</div><div class="ms" id="wl">— wins / — losses</div></div>
  <div class="metric"><div class="ml">Fase</div><div class="mv" id="phase">—</div><div class="ms" id="phaseSub">—</div></div>
</div>

<div class="main">
  <div class="left">
    <div class="sec">Progreso — <span id="pctText">0%</span></div>
    <div class="progress-wrap"><div class="progress-fill" id="progressBar" style="width:0%"></div></div>

    <div id="finBanner" class="fin-banner">
      <div class="fin-title">🏁 Prueba completada</div>
      <div class="fin-pnl" id="finPnl">—</div>
      <div style="font-size:.7rem;color:var(--muted)" id="finStats">—</div>
    </div>

    <div class="sec">Historial de trades</div>
    <div id="tradesTable" style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:.68rem">
        <thead>
          <tr style="color:var(--muted);font-size:.58rem">
            <th style="text-align:left;padding:6px 4px;border-bottom:1px solid var(--border)">#</th>
            <th style="text-align:right;padding:6px 4px;border-bottom:1px solid var(--border)">Compra</th>
            <th style="text-align:right;padding:6px 4px;border-bottom:1px solid var(--border)">Venta</th>
            <th style="text-align:right;padding:6px 4px;border-bottom:1px solid var(--border)">P&L</th>
            <th style="text-align:right;padding:6px 4px;border-bottom:1px solid var(--border)">%</th>
          </tr>
        </thead>
        <tbody id="tradeRows"></tbody>
      </table>
    </div>
  </div>

  <div class="right">
    <div class="stats-box">
      <div class="sec">Estadísticas</div>
      <div class="stat-row"><span class="sk">Capital/ciclo</span><span id="capital">—</span></div>
      <div class="stat-row"><span class="sk">Ciclos totales</span><span id="totalCycles">—</span></div>
      <div class="stat-row"><span class="sk">Hold por ciclo</span><span id="holdSec">—</span></div>
      <div class="stat-row"><span class="sk">Mejor trade</span><span class="green" id="bestTrade">—</span></div>
      <div class="stat-row"><span class="sk">Peor trade</span><span class="red" id="worstTrade">—</span></div>
      <div class="stat-row"><span class="sk">Balance</span><span id="balance">—</span></div>
    </div>

    <div class="trade-list">
      <div class="sec">Último trade</div>
      <div id="lastTrade" style="color:var(--muted);font-size:.7rem;text-align:center;padding:10px">Esperando primer ciclo...</div>
    </div>

    <div class="upd" id="upd">↻ —</div>
  </div>
</div>

<script>
function update(){
  fetch('/api/state').then(r=>r.json()).then(d=>{
    document.getElementById('dot').style.background = d.status==='running'?'var(--green)':d.status==='finished'?'var(--gold)':'var(--red)';
    document.getElementById('st').textContent = d.status==='running'?'Corriendo':d.status==='finished'?'Completado':'Iniciando';

    // Price
    if(d.current_price) document.getElementById('price').textContent='$'+d.current_price;

    // Cycle
    document.getElementById('cycle').textContent = d.cycle||0;
    document.getElementById('cycleMax').textContent = 'de '+d.max_cycles+' total';

    // Progress
    const pct = d.max_cycles>0?Math.round((d.cycle/d.max_cycles)*100):0;
    document.getElementById('progressBar').style.width = pct+'%';
    document.getElementById('pctText').textContent = pct+'%';

    // PnL
    const pnl=d.pnl_total||0;
    const pe=document.getElementById('pnl');
    pe.textContent=(pnl>=0?'+':'')+pnl.toFixed(4);
    pe.className='mv '+(pnl>0?'green':pnl<0?'red':'gold');

    // Win rate
    const total=d.wins+d.losses;
    const wr=total>0?Math.round(d.wins/total*100):0;
    const wre=document.getElementById('wr');
    wre.textContent=wr+'%';
    wre.className='mv '+(wr>=50?'green':'red');
    document.getElementById('wl').textContent=(d.wins||0)+' wins / '+(d.losses||0)+' losses';

    // Phase
    const phaseMap={'idle':'En espera','holding':'💎 Holding','sold':'Vendido'};
    const pe2=document.getElementById('phase');
    pe2.textContent=phaseMap[d.phase]||d.phase||'—';
    pe2.className='mv '+(d.phase==='holding'?'gold':d.phase==='sold'?'green':'');
    document.getElementById('phaseSub').textContent=d.phase==='holding'?'Posición abierta':d.phase==='sold'?'Trade cerrado':'Sin posición';

    // Stats
    document.getElementById('balance').textContent=(d.balance||0).toFixed(4)+' USDT';
    if(d.trades&&d.trades.length){
      const best=d.trades.reduce((a,b)=>a.pnl>b.pnl?a:b);
      const worst=d.trades.reduce((a,b)=>a.pnl<b.pnl?a:b);
      document.getElementById('bestTrade').textContent='+'+(best.pnl>0?best.pnl.toFixed(4):'—')+' USDT';
      document.getElementById('worstTrade').textContent=(worst.pnl<0?worst.pnl.toFixed(4):'—')+' USDT';

      // Table
      document.getElementById('tradeRows').innerHTML=[...d.trades].reverse().map(t=>`
        <tr style="border-bottom:1px solid var(--border)">
          <td style="padding:5px 4px;color:var(--muted)">#${t.cycle}</td>
          <td style="text-align:right;padding:5px 4px">$${t.buy}</td>
          <td style="text-align:right;padding:5px 4px">$${t.sell}</td>
          <td style="text-align:right;padding:5px 4px;color:${t.pnl>=0?'var(--green)':'var(--red)'};font-weight:bold">${t.pnl>=0?'+':''}${t.pnl}</td>
          <td style="text-align:right;padding:5px 4px;color:${t.pnl_pct>=0?'var(--green)':'var(--red)'}">${t.pnl_pct>=0?'+':''}${t.pnl_pct}%</td>
        </tr>`).join('');

      // Last trade
      const last=d.trades[d.trades.length-1];
      document.getElementById('lastTrade').innerHTML=`
        <div style="font-family:var(--display);font-size:1rem;font-weight:700;color:${last.pnl>=0?'var(--green)':'var(--red)'}">${last.pnl>=0?'+':''}${last.pnl} USDT</div>
        <div style="color:var(--muted);margin-top:4px;font-size:.65rem">$${last.buy} → $${last.sell}</div>
        <div style="color:var(--muted);font-size:.62rem">${last.buy_time} → ${last.sell_time}</div>`;
    }

    // Finished banner
    if(d.finished){
      const fb=document.getElementById('finBanner');
      fb.style.display='block';
      const fp=document.getElementById('finPnl');
      fp.textContent=(pnl>=0?'+':'')+pnl.toFixed(4)+' USDT';
      fp.style.color=pnl>=0?'var(--green)':'var(--red)';
      document.getElementById('finStats').textContent=wr+'% win rate | '+d.wins+' wins, '+d.losses+' losses';
    }

    document.getElementById('upd').textContent='↻ '+(d.last_update||'—');
  }).catch(()=>{
    document.getElementById('dot').style.background='var(--red)';
  });
}
update();setInterval(update,5000);  // actualiza cada 5s
</script>
</body>
</html>"""

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
