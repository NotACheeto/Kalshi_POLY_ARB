"""
High-Performance Real-Time Arbitrage Dashboard Web Server.
Serves responsive SPA frontend and WebSockets feed on localhost.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from src.dashboard.state import DashboardState

logger = logging.getLogger(__name__)


def create_dashboard_app(state: DashboardState) -> FastAPI:
    app = FastAPI(title="Polymarket <-> Kalshi Arbitrage Dashboard")
    active_websockets: Set[WebSocket] = set()

    DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kalshi ⟷ Polymarket Arbitrage Engine</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #0a0e17;
            --card-bg: #121824;
            --border-color: #1e293b;
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --accent-green: #10b981;
            --accent-red: #ef4444;
            --accent-cyan: #06b6d4;
            --accent-purple: #8b5cf6;
            --accent-amber: #f59e0b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg-dark);
            color: var(--text-main);
            font-family: 'Inter', -apple-system, sans-serif;
            padding: 16px;
            overflow-x: hidden;
        }
        .mono { font-family: 'JetBrains Mono', monospace; }
        
        /* Top Navigation Header */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 14px 24px;
            margin-bottom: 16px;
        }
        .logo-title {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .logo-dot {
            width: 14px;
            height: 14px;
            border-radius: 50%;
            background: var(--accent-green);
            box-shadow: 0 0 12px var(--accent-green);
            animation: pulse 2s infinite;
        }
        @keyframes pulse { 0% { opacity: 0.6; } 50% { opacity: 1; } 100% { opacity: 0.6; } }
        .title h1 { font-size: 1.15rem; font-weight: 700; letter-spacing: -0.5px; }
        .title p { font-size: 0.75rem; color: var(--text-muted); }
        .header-badges { display: flex; gap: 10px; align-items: center; }
        .badge {
            font-size: 0.72rem;
            font-weight: 600;
            padding: 4px 10px;
            border-radius: 6px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .badge-dry { background: rgba(6, 182, 212, 0.15); color: var(--accent-cyan); border: 1px solid var(--accent-cyan); }
        .badge-live { background: rgba(239, 68, 68, 0.15); color: var(--accent-red); border: 1px solid var(--accent-red); }
        .badge-active { background: rgba(16, 185, 129, 0.15); color: var(--accent-green); border: 1px solid var(--accent-green); }

        /* Metric Grid */
        .metric-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 14px;
            margin-bottom: 16px;
        }
        .metric-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 14px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }
        .metric-label { font-size: 0.75rem; color: var(--text-muted); margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
        .metric-value { font-size: 1.4rem; font-weight: 700; }
        .metric-sub { font-size: 0.75rem; color: var(--text-muted); margin-top: 4px; }

        /* Radar & Orderbook Section */
        .main-section {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 16px;
            margin-bottom: 16px;
        }
        @media (max-width: 1024px) { .main-section { grid-template-columns: 1fr; } }
        
        .radar-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
        }
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 12px;
            margin-bottom: 16px;
        }
        .section-title { font-size: 1rem; font-weight: 700; display: flex; align-items: center; gap: 8px; }
        .timer-badge {
            font-size: 0.85rem;
            font-weight: 700;
            background: rgba(245, 158, 11, 0.15);
            color: var(--accent-amber);
            border: 1px solid var(--accent-amber);
            padding: 4px 12px;
            border-radius: 6px;
        }

        /* Orderbook Comparison Matrix */
        .book-matrix {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-bottom: 18px;
        }
        .exchange-book {
            background: #0d121c;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 14px;
        }
        .ex-title { font-size: 0.85rem; font-weight: 700; margin-bottom: 10px; display: flex; justify-content: space-between; }
        .book-row {
            display: flex;
            justify-content: space-between;
            padding: 6px 0;
            border-bottom: 1px dashed rgba(255,255,255,0.05);
            font-size: 0.82rem;
        }
        .bid-price { color: var(--accent-green); font-weight: 600; }
        .ask-price { color: var(--accent-red); font-weight: 600; }

        /* Arbitrage Synthetic Bundle Card */
        .spread-display {
            background: #090e18;
            border: 1px solid #1e293b;
            border-radius: 8px;
            padding: 16px;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }
        .spread-box {
            padding: 12px;
            border-radius: 6px;
            background: #111726;
            border: 1px solid #23304a;
        }
        .spread-title { font-size: 0.72rem; color: var(--text-muted); text-transform: uppercase; margin-bottom: 4px; }
        .spread-cost { font-size: 1.3rem; font-weight: 700; }
        .spread-edge { font-size: 0.8rem; font-weight: 600; margin-top: 4px; }
        .positive { color: var(--accent-green); }
        .neutral { color: var(--text-muted); }

        /* Tables & Feeds */
        .feed-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 18px;
        }
        table { width: 100%; border-collapse: collapse; font-size: 0.8rem; margin-top: 10px; }
        th { text-align: left; padding: 8px 10px; color: var(--text-muted); border-bottom: 1px solid var(--border-color); }
        td { padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,0.03); }
        tr:hover { background: rgba(255,255,255,0.02); }

        /* Terminal Console */
        .console-box {
            background: #06090f;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 12px;
            height: 180px;
            overflow-y: auto;
            font-size: 0.75rem;
            color: #94a3b8;
            line-height: 1.4;
        }
        .console-line { margin-bottom: 3px; }
    </style>
</head>
<body>
    <header>
        <div class="logo-title">
            <div class="logo-dot"></div>
            <div class="title">
                <h1>KALSHI ⟷ POLYMARKET ARBITRAGE RADAR</h1>
                <p>Ultra-Low Latency Cross-Exchange Execution Engine (15M BTC Up/Down Focus)</p>
            </div>
        </div>
        <div class="header-badges">
            <span id="badge-mode" class="badge badge-dry">DRY RUN</span>
            <span id="badge-status" class="badge badge-active">ENGINE RUNNING</span>
            <span id="uptime-display" class="badge mono" style="background:#1e293b; color:#cbd5e1;">UPTIME: 00:00:00</span>
        </div>
    </header>

    <!-- Top Key Metrics & Latencies -->
    <div class="metric-grid">
        <div class="metric-card">
            <div class="metric-label">Kalshi Latency</div>
            <div id="kalshi-rtt" class="metric-value mono positive">-- ms</div>
            <div class="metric-sub">FCA Benchmark API (RTT)</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Polymarket CLOB Latency</div>
            <div id="poly-rtt" class="metric-value mono" style="color:var(--accent-cyan);">-- ms</div>
            <div class="metric-sub">EIP-712 Orderbook (RTT)</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Scan Frequency</div>
            <div id="scan-freq" class="metric-value mono">-- Hz</div>
            <div id="total-scans-sub" class="metric-sub">0 total scans completed</div>
        </div>
        <div class="metric-card">
            <div class="metric-label">Realized Net PnL</div>
            <div id="pnl-val" class="metric-value mono positive">$0.00</div>
            <div id="trades-sub" class="metric-sub">0 trades executed</div>
        </div>
    </div>

    <!-- Active 15M Bitcoin Arbitrage Radar -->
    <div class="main-section">
        <div class="radar-card">
            <div class="section-header">
                <div class="section-title">
                    <span>⚡ RECURRING 15-MINUTE BITCOIN ARBITRAGE PAIR</span>
                </div>
                <div id="countdown-timer" class="timer-badge mono">EXPIRATION: --:--</div>
            </div>
            
            <p id="active-pair-name" style="font-size:0.85rem; color:var(--text-muted); margin-bottom:14px;">Loading active market pair...</p>

            <div class="book-matrix">
                <!-- Kalshi Book -->
                <div class="exchange-book">
                    <div class="ex-title">
                        <span>KALSHI (KXBTC15M)</span>
                        <span id="kalshi-spread" class="mono" style="color:var(--accent-amber); font-size:0.75rem;">Spread: --</span>
                    </div>
                    <div class="book-row">
                        <span>YES Bid / Ask:</span>
                        <span class="mono"><span id="k-yes-bid" class="bid-price">--</span> / <span id="k-yes-ask" class="ask-price">--</span></span>
                    </div>
                    <div class="book-row">
                        <span>NO Bid / Ask:</span>
                        <span class="mono"><span id="k-no-bid" class="bid-price">--</span> / <span id="k-no-ask" class="ask-price">--</span></span>
                    </div>
                </div>

                <!-- Polymarket Book -->
                <div class="exchange-book">
                    <div class="ex-title">
                        <span>POLYMARKET (btc-updown-15m)</span>
                        <span id="poly-spread" class="mono" style="color:var(--accent-amber); font-size:0.75rem;">Spread: --</span>
                    </div>
                    <div class="book-row">
                        <span>UP Bid / Ask:</span>
                        <span class="mono"><span id="p-yes-bid" class="bid-price">--</span> / <span id="p-yes-ask" class="ask-price">--</span></span>
                    </div>
                    <div class="book-row">
                        <span>DOWN Bid / Ask:</span>
                        <span class="mono"><span id="p-no-bid" class="bid-price">--</span> / <span id="p-no-ask" class="ask-price">--</span></span>
                    </div>
                </div>
            </div>

            <!-- Synthetic Spreads & Real-Time EV Gate -->
            <div class="spread-display">
                <div class="spread-box">
                    <div class="spread-title">DIRECTION 1: Buy Poly UP + Kalshi NO</div>
                    <div id="dir1-cost" class="spread-cost mono">--</div>
                    <div id="dir1-status" class="spread-edge mono neutral">Monitoring Spread</div>
                </div>
                <div class="spread-box">
                    <div class="spread-title">DIRECTION 2: Buy Poly DOWN + Kalshi YES</div>
                    <div id="dir2-cost" class="spread-cost mono">--</div>
                    <div id="dir2-status" class="spread-edge mono neutral">Monitoring Spread</div>
                </div>
            </div>
        </div>

        <!-- Risk & Safety Panel -->
        <div class="feed-card">
            <div class="section-header">
                <div class="section-title">🛡️ RISK & SAFETY GATES</div>
            </div>
            <div style="font-size:0.8rem; margin-bottom:12px;">
                <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                    <span style="color:var(--text-muted);">Daily Loss Limit:</span>
                    <span id="daily-loss-display" class="mono">$0.00 / $15.00</span>
                </div>
                <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                    <span style="color:var(--text-muted);">Max Market Exposure:</span>
                    <span class="mono">$50.00</span>
                </div>
                <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                    <span style="color:var(--text-muted);">Min Net Edge Required:</span>
                    <span class="mono">1.50%</span>
                </div>
                <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
                    <span style="color:var(--text-muted);">Execution Gate:</span>
                    <span class="mono positive">Strict Positive-EV</span>
                </div>
                <div style="display:flex; justify-content:space-between;">
                    <span style="color:var(--text-muted);">Quote Freshness Gate:</span>
                    <span class="mono">&lt; 2.0s</span>
                </div>
            </div>
            <hr style="border:none; border-top:1px solid var(--border-color); margin:12px 0;">
            <div class="section-title" style="font-size:0.85rem; margin-bottom:8px;">TERMINAL FEED</div>
            <div id="console-stream" class="console-box mono">
                <div class="console-line">Connecting to live engine telemetry...</div>
            </div>
        </div>
    </div>

    <!-- Arbitrage Opportunities & Executed Trades Table -->
    <div class="feed-card">
        <div class="section-header">
            <div class="section-title">📊 ARBITRAGE OPPORTUNITY FEED & EXECUTION HISTORY</div>
        </div>
        <table>
            <thead>
                <tr>
                    <th>TIMESTAMP</th>
                    <th>STRATEGY / DIRECTION</th>
                    <th>GROSS COST</th>
                    <th>NET PROFIT</th>
                    <th>NET EDGE</th>
                    <th>SIZE</th>
                    <th>STATUS</th>
                </tr>
            </thead>
            <tbody id="opp-table-body" class="mono">
                <tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:20px;">Awaiting detected arbitrage events...</td></tr>
            </tbody>
        </table>
    </div>

    <script>
        const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${wsProto}//${window.location.host}/ws`;
        let ws;

        function connect() {
            ws = new WebSocket(wsUrl);
            ws.onopen = () => console.log('Connected to Arbitrage Dashboard');
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                updateUI(data);
            };
            ws.onclose = () => {
                console.log('WebSocket disconnected, reconnecting in 2s...');
                setTimeout(connect, 2000);
            };
        }

        function formatUptime(seconds) {
            const hrs = Math.floor(seconds / 3600).toString().padStart(2, '0');
            const mins = Math.floor((seconds % 3600) / 60).toString().padStart(2, '0');
            const secs = Math.floor(seconds % 60).toString().padStart(2, '0');
            return `${hrs}:${mins}:${secs}`;
        }

        function updateUI(d) {
            // Header & Badges
            document.getElementById('badge-mode').innerText = d.mode.includes('DRY') ? 'DRY RUN' : 'LIVE';
            document.getElementById('badge-mode').className = d.mode.includes('DRY') ? 'badge badge-dry' : 'badge badge-live';
            document.getElementById('uptime-display').innerText = `UPTIME: ${formatUptime(d.uptime_seconds)}`;

            // Metrics
            const kRtt = document.getElementById('kalshi-rtt');
            kRtt.innerText = `${d.kalshi_latency_ms.toFixed(1)} ms`;
            kRtt.style.color = d.kalshi_latency_ms < 60 ? 'var(--accent-green)' : (d.kalshi_latency_ms < 150 ? 'var(--accent-amber)' : 'var(--accent-red)');

            const pRtt = document.getElementById('poly-rtt');
            pRtt.innerText = `${d.poly_latency_ms.toFixed(1)} ms`;
            pRtt.style.color = d.poly_latency_ms < 120 ? 'var(--accent-green)' : (d.poly_latency_ms < 200 ? 'var(--accent-amber)' : 'var(--accent-red)');

            document.getElementById('scan-freq').innerText = `${d.scan_frequency_hz.toFixed(1)} Hz`;
            document.getElementById('total-scans-sub').innerText = `${d.total_scans} total scans completed`;
            document.getElementById('pnl-val').innerText = `$${d.daily_pnl_dollars.toFixed(2)}`;
            document.getElementById('trades-sub').innerText = `${d.total_trades_executed} trades executed`;

            // Active Pair & Countdown
            document.getElementById('active-pair-name').innerText = `${d.active_pair_id} ⟷ ${d.active_pair_title}`;
            const minsLeft = Math.floor(d.seconds_to_expiration / 60);
            const secsLeft = Math.floor(d.seconds_to_expiration % 60);
            document.getElementById('countdown-timer').innerText = `EXPIRATION: ${minsLeft}m ${secsLeft.toString().padStart(2, '0')}s`;

            // Kalshi Book
            document.getElementById('k-yes-bid').innerText = d.kalshi_yes_bid !== null ? `$${d.kalshi_yes_bid.toFixed(2)}` : '--';
            document.getElementById('k-yes-ask').innerText = d.kalshi_yes_ask !== null ? `$${d.kalshi_yes_ask.toFixed(2)}` : '--';
            document.getElementById('k-no-bid').innerText = d.kalshi_no_bid !== null ? `$${d.kalshi_no_bid.toFixed(2)}` : '--';
            document.getElementById('k-no-ask').innerText = d.kalshi_no_ask !== null ? `$${d.kalshi_no_ask.toFixed(2)}` : '--';
            document.getElementById('kalshi-spread').innerText = d.kalshi_spread !== null ? `Spread: $${d.kalshi_spread.toFixed(2)}` : 'Spread: --';

            // Polymarket Book
            document.getElementById('p-yes-bid').innerText = d.poly_yes_bid !== null ? `$${d.poly_yes_bid.toFixed(2)}` : '--';
            document.getElementById('p-yes-ask').innerText = d.poly_yes_ask !== null ? `$${d.poly_yes_ask.toFixed(2)}` : '--';
            document.getElementById('p-no-bid').innerText = d.poly_no_bid !== null ? `$${d.poly_no_bid.toFixed(2)}` : '--';
            document.getElementById('p-no-ask').innerText = d.poly_no_ask !== null ? `$${d.poly_no_ask.toFixed(2)}` : '--';
            document.getElementById('poly-spread').innerText = d.poly_spread !== null ? `Spread: $${d.poly_spread.toFixed(2)}` : 'Spread: --';

            // Spreads
            if (d.dir1_cost !== null) {
                const el = document.getElementById('dir1-cost');
                el.innerText = `$${d.dir1_cost.toFixed(4)}`;
                el.style.color = d.dir1_cost < 1.0 ? 'var(--accent-green)' : 'var(--text-main)';
                document.getElementById('dir1-status').innerText = d.dir1_cost < 1.0 ? `EDGE: ${((1.0 - d.dir1_cost)*100).toFixed(2)}%` : 'No Arbitrage';
                document.getElementById('dir1-status').className = d.dir1_cost < 1.0 ? 'spread-edge mono positive' : 'spread-edge mono neutral';
            }
            if (d.dir2_cost !== null) {
                const el = document.getElementById('dir2-cost');
                el.innerText = `$${d.dir2_cost.toFixed(4)}`;
                el.style.color = d.dir2_cost < 1.0 ? 'var(--accent-green)' : 'var(--text-main)';
                document.getElementById('dir2-status').innerText = d.dir2_cost < 1.0 ? `EDGE: ${((1.0 - d.dir2_cost)*100).toFixed(2)}%` : 'No Arbitrage';
                document.getElementById('dir2-status').className = d.dir2_cost < 1.0 ? 'spread-edge mono positive' : 'spread-edge mono neutral';
            }

            // Console Stream
            if (d.recent_logs && d.recent_logs.length > 0) {
                const consoleBox = document.getElementById('console-stream');
                consoleBox.innerHTML = d.recent_logs.map(line => `<div class="console-line">${line}</div>`).join('');
            }

            // Opportunity Table
            if (d.recent_opportunities && d.recent_opportunities.length > 0) {
                const tbody = document.getElementById('opp-table-body');
                tbody.innerHTML = d.recent_opportunities.map(o => `
                    <tr>
                        <td>${o.time || '--'}</td>
                        <td style="color:var(--accent-cyan); font-weight:600;">${o.direction}</td>
                        <td>$${parseFloat(o.gross_cost).toFixed(4)}</td>
                        <td class="positive">+$${parseFloat(o.net_profit).toFixed(2)}</td>
                        <td class="positive">${(parseFloat(o.net_edge) * 100).toFixed(2)}%</td>
                        <td>${o.size}</td>
                        <td><span class="badge badge-active">${o.status}</span></td>
                    </tr>
                `).join('');
            }
        }

        connect();
    </script>
</body>
</html>
"""

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(content=DASHBOARD_HTML, status_code=200)

    @app.get("/api/state")
    async def get_state():
        return state.to_dict()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        active_websockets.add(websocket)
        try:
            while True:
                # Send latest telemetry snapshot
                data = state.to_dict()
                await websocket.send_text(json.dumps(data))
                await asyncio.sleep(0.35)  # ~3 Hz broadcast frequency
        except WebSocketDisconnect:
            active_websockets.remove(websocket)
        except Exception as e:
            logger.debug(f"Dashboard WebSocket disconnected: {e}")
            active_websockets.discard(websocket)

    return app
