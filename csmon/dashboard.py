"""看板前端（单页，内联 HTML/CSS/JS）。

两个刻意的取舍：
  1. **不引任何 CDN**。K 线用原生 Canvas 画，不依赖 ECharts/Chart.js。
     树莓派常部署在无外网或外网不稳的环境，而且监控看板离线也能看历史数据，
     引 CDN 会让「数据源全挂时至少还能看库里的历史」这个兜底能力失效。
  2. **无构建步骤**。没有 npm/webpack，改完刷新即可。个人自用工具不值得
     为前端引入一套工具链。
"""
from __future__ import annotations

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>youyoumonitor · CS 饰品行情看板</title>
<style>
  :root{--bg:#0d1117;--panel:#161b22;--line:#232a33;--fg:#e6edf3;--dim:#8b949e;
        --up:#f85149;--down:#3fb950;--accent:#58a6ff;--warn:#e3b341;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif}
  header{padding:14px 20px;border-bottom:1px solid var(--line)}
  .top{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
  h1{font-size:16px;margin:0;font-weight:600}
  .sub{color:var(--dim);font-size:12px}
  nav{display:flex;gap:4px;margin-top:12px;flex-wrap:wrap}
  nav button{background:transparent;color:var(--dim);border:1px solid transparent;
             border-radius:7px;padding:6px 12px;cursor:pointer;font-size:13px}
  nav button:hover{color:var(--fg);background:#1b2129}
  nav button.on{color:var(--fg);background:#21262d;border-color:var(--line)}
  main{padding:18px 20px 40px;max-width:1500px}
  .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
        padding:12px 16px;min-width:140px;flex:0 1 auto}
  .card .k{color:var(--dim);font-size:12px}
  .card .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{text-align:left;padding:8px 11px;border-bottom:1px solid var(--line);
        font-size:13px;vertical-align:middle}
  th{color:var(--dim);font-weight:500}
  tr:hover td{background:#1b2129}
  .tag{display:inline-block;padding:1px 7px;border-radius:5px;font-size:11px;
       border:1px solid var(--line);color:var(--dim);white-space:nowrap}
  .tag.BUFF{color:#e3b341;border-color:#5c4a1a}
  .tag.YOUPIN{color:#58a6ff;border-color:#1f3d5c}
  .tag.STEAM{color:#8b949e}
  .pos{color:var(--up)} .neg{color:var(--down)}
  .sev-critical{color:#f85149}.sev-warning{color:#e3b341}.sev-info{color:var(--dim)}
  section{margin-bottom:26px}
  h2{font-size:13px;color:var(--dim);font-weight:500;margin:0 0 10px;
     text-transform:none;letter-spacing:.02em}
  .empty{color:var(--dim);padding:22px;text-align:center;border:1px dashed var(--line);
         border-radius:10px}
  .bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;
       display:inline-block;width:64px;vertical-align:middle}
  .bar > i{display:block;height:100%;background:var(--accent)}
  button.act{background:#21262d;color:var(--fg);border:1px solid var(--line);
             border-radius:6px;padding:5px 11px;cursor:pointer;font-size:12px}
  button.act:hover{border-color:var(--accent)}
  input,select{background:#0d1117;color:var(--fg);border:1px solid var(--line);
               border-radius:6px;padding:6px 10px;font-size:13px;font-family:inherit}
  input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
  code{background:#1b2129;padding:1px 5px;border-radius:4px;font-size:12px}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
  @media(max-width:1000px){.grid2{grid-template-columns:1fr}}
  canvas{background:var(--panel);border:1px solid var(--line);border-radius:10px;
         width:100%;height:340px;display:block}
  .metrics{display:flex;gap:20px;flex-wrap:wrap;margin:14px 0}
  .metric{min-width:120px}
  .metric .k{color:var(--dim);font-size:12px}
  .metric .v{font-size:15px;font-variant-numeric:tabular-nums}
  .readings{color:var(--dim);font-size:12.5px;line-height:1.7}
  .hide{display:none}
  footer{color:var(--dim);font-size:12px;padding:0 20px 30px}
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>youyoumonitor</h1>
    <span class="sub" id="meta">加载中…</span>
    <span style="flex:1"></span>
    <button class="act" onclick="refreshAll()">刷新</button>
    <button class="act" onclick="runOnce()">立即采集</button>
  </div>
  <nav id="tabs"></nav>
</header>
<main>
  <div class="cards" id="cards"></div>

  <section data-tab="focus">
    <h2>关注清单（我真正要买 / 要卖的）</h2>
    <div id="focusSummary" class="sub" style="margin-bottom:10px"></div>
    <div id="focus"></div>
    <h2 style="margin-top:24px">按基础饰品展开变体</h2>
    <div class="row">
      <input id="variantBase" placeholder="输入基础名，如 AK-47 | Redline"
             style="min-width:320px">
      <button class="act" onclick="loadVariants()">展开变体</button>
      <span class="sub" id="variantHint"></span>
    </div>
    <div id="variants"></div>
  </section>

  <section data-tab="overview" class="hide">
    <h2>行情快照（每个平台最新一条）</h2>
    <div id="quotes"></div>
  </section>

  <section data-tab="kline" class="hide">
    <div class="row">
      <input id="klineItem" list="itemList" placeholder="输入饰品名（Steam 官方命名）"
             style="min-width:360px">
      <datalist id="itemList"></datalist>
      <select id="klinePlatform">
        <option>BUFF</option><option>YOUPIN</option><option>STEAM</option>
      </select>
      <select id="klineDays">
        <option value="30">30 天</option>
        <option value="90" selected>90 天</option>
        <option value="365">365 天</option>
      </select>
      <button class="act" onclick="loadKline()">加载</button>
    </div>
    <canvas id="chart" width="1200" height="340"></canvas>
    <div class="metrics" id="klineMetrics"></div>
    <div class="readings" id="klineReadings"></div>
  </section>

  <section data-tab="rent" class="hide">
    <div class="row">
      <span class="sub" id="rentNote"></span>
      <span style="flex:1"></span>
      <label class="sub">最低流动性
        <input id="rentMinLiq" type="number" value="0" step="10" style="width:70px"></label>
      <button class="act" onclick="loadRent()">刷新</button>
    </div>
    <h2>租赁收益排行（已计入租金抽成、卖出抽成、提现费、推算出租率）</h2>
    <div id="rent"></div>
    <div id="rentDetail" style="margin-top:22px"></div>
  </section>

  <section data-tab="spread" class="hide">
    <h2>跨平台价差雷达</h2>
    <div class="row">
      <label class="sub">最低净收益率
        <input id="spMinPct" type="number" value="3" step="0.5" style="width:70px">%</label>
      <label class="sub">最低净收益
        <input id="spMinProfit" type="number" value="1" step="1" style="width:70px">元</label>
      <button class="act" onclick="loadSpread()">重新计算</button>
      <span class="sub" id="spNote"></span>
    </div>
    <div id="spread"></div>
  </section>

  <section data-tab="movers" class="hide">
    <div class="grid2">
      <div>
        <h2>涨跌排行</h2>
        <div id="movers"></div>
      </div>
      <div>
        <h2>流动性（在售量）</h2>
        <div id="liquidity"></div>
      </div>
    </div>
  </section>

  <section data-tab="advice" class="hide">
    <div class="row">
      <span class="sub" id="llmStatus"></span>
      <span style="flex:1"></span>
      <button class="act" onclick="loadAdvice()">刷新建议</button>
    </div>
    <div id="advice"></div>
  </section>

  <section data-tab="alerts" class="hide">
    <h2>最近告警</h2>
    <div id="alerts"></div>
  </section>

  <section data-tab="sources" class="hide">
    <div class="grid2">
      <div>
        <h2>数据源健康度</h2>
        <div id="sources"></div>
      </div>
      <div>
        <h2>极致追踪</h2>
        <div id="extreme"></div>
      </div>
    </div>
  </section>
</main>
<footer id="footer"></footer>

<script>
const TABS = [
  ["focus","关注"],["overview","概览"],["kline","K 线"],["rent","租赁"],
  ["spread","套利"],["movers","涨跌"],["advice","建议"],["alerts","告警"],
  ["sources","数据源"],
];
let current = "overview";

const $ = id => document.getElementById(id);
const fmt = (v, d=2) => (v === null || v === undefined || v === "")
  ? "—" : Number(v).toLocaleString("zh-CN",
      {minimumFractionDigits:d, maximumFractionDigits:d});
const pct = (v, d=1) => (v === null || v === undefined)
  ? "—" : (v >= 0 ? "+" : "") + (v*100).toFixed(d) + "%";
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
const platTag = p => `<span class="tag ${p==="BUFF"?"BUFF":(p==="YOUPIN"?"YOUPIN":"STEAM")}">${esc(p)}</span>`;

async function get(url){
  const r = await fetch(url);
  if(!r.ok) throw new Error(url + " → HTTP " + r.status);
  return r.json();
}

function buildTabs(){
  $("tabs").innerHTML = TABS.map(([id,label]) =>
    `<button data-tab="${id}" class="${id===current?"on":""}"
      onclick="switchTab('${id}')">${label}</button>`).join("");
}

function switchTab(id){
  current = id;
  buildTabs();
  document.querySelectorAll("main section").forEach(s =>
    s.classList.toggle("hide", s.dataset.tab !== id));
  if(id === "kline" && !chartDrawn) loadKline();
  if(id === "spread" && !spreadDrawn) loadSpread();
  if(id === "focus" && !focusDrawn) loadFocus();
  if(id === "rent" && !rentDrawn) loadRent();
  if(id === "advice") loadAdvice();
}

async function loadFocus(){
  try{ renderFocus(await get("/api/focus")); }
  catch(e){
    document.getElementById("focus").innerHTML =
      `<div class="empty neg">加载失败：${esc(e.message)}</div>`;
  }
}

// ── 概览 ───────────────────────────────────────────────────
function renderCards(s){
  const items = [["饰品总数",s.items],["报价采样",s.quotes],
                 ["监控中",s.watching],["告警累计",s.alerts]];
  $("cards").innerHTML = items.map(([k,v]) =>
    `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
  $("meta").textContent = s.database;
}

function renderQuotes(rows){
  if(!rows.length){
    $("quotes").innerHTML = '<div class="empty">还没有采集到报价。点右上角「立即采集」或运行 <code>python bootstrap.py</code></div>';
    return;
  }
  const head = `<tr><th>饰品</th><th>平台</th><th>在售价</th><th>在售量</th><th>求购价</th><th>来源</th><th>观测时间</th></tr>`;
  $("quotes").innerHTML = `<table>${head}` + rows.map(r => `<tr>
    <td>${esc(r.market_hash_name)}</td>
    <td>${platTag(r.platform)}</td>
    <td>${fmt(r.sell_price)}</td>
    <td>${r.sell_count ?? "—"}</td>
    <td>${fmt(r.bid_price)}</td>
    <td class="sub">${esc(r.source)}</td>
    <td class="sub">${esc((r.observed_at||"").replace("T"," ").slice(0,19))}</td>
  </tr>`).join("") + "</table>";
}

// ── K 线（原生 Canvas，无外部依赖）─────────────────────────
let chartDrawn = false;

function drawChart(bars){
  const cv = $("chart");
  const dpr = window.devicePixelRatio || 1;
  const cssW = cv.clientWidth || 1200, cssH = 340;
  cv.width = cssW * dpr; cv.height = cssH * dpr;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,cssW,cssH);

  if(!bars.length){ return; }

  const padL = 58, padR = 12, padT = 14, padB = 26;
  const w = cssW - padL - padR, h = cssH - padT - padB;
  const highs = bars.map(b => b.high), lows = bars.map(b => b.low);
  const max = Math.max(...highs), min = Math.min(...lows);
  const span = (max - min) || (max * 0.1) || 1;
  const top = max + span*0.06, bot = Math.max(0, min - span*0.06);
  const y = v => padT + h * (1 - (v - bot) / ((top - bot) || 1));
  const slot = w / bars.length;
  const bw = Math.max(1.5, Math.min(14, slot * 0.62));

  // 网格与刻度
  ctx.strokeStyle = "#232a33"; ctx.fillStyle = "#8b949e";
  ctx.font = "11px system-ui,sans-serif"; ctx.lineWidth = 1;
  for(let i=0;i<=4;i++){
    const v = bot + (top-bot)*i/4, yy = Math.round(y(v)) + .5;
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(cssW-padR, yy); ctx.stroke();
    ctx.fillText(v.toFixed(2), 6, yy + 4);
  }

  // 蜡烛
  bars.forEach((b, i) => {
    const cx = padL + slot*i + slot/2;
    const up = b.close >= b.open;
    const color = up ? "#f85149" : "#3fb950";
    ctx.strokeStyle = color; ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(Math.round(cx)+.5, y(b.high));
    ctx.lineTo(Math.round(cx)+.5, y(b.low));
    ctx.stroke();
    const yo = y(b.open), yc = y(b.close);
    const yTop = Math.min(yo,yc), bh = Math.max(1.5, Math.abs(yc-yo));
    ctx.fillRect(cx - bw/2, yTop, bw, bh);
  });

  // MA7 / MA30 折线
  const maLine = (period, color) => {
    if(bars.length < period) return;
    ctx.strokeStyle = color; ctx.lineWidth = 1.4; ctx.beginPath();
    let started = false;
    for(let i=period-1;i<bars.length;i++){
      let sum = 0;
      for(let k=i-period+1;k<=i;k++) sum += bars[k].close;
      const v = sum/period, cx = padL + slot*i + slot/2, yy = y(v);
      if(!started){ ctx.moveTo(cx, yy); started = true; } else { ctx.lineTo(cx, yy); }
    }
    ctx.stroke();
  };
  maLine(7, "#58a6ff");
  maLine(30, "#e3b341");

  // 图例与 x 轴日期
  ctx.font = "11px system-ui,sans-serif";
  ctx.fillStyle = "#58a6ff"; ctx.fillText("MA7", padL, 12);
  ctx.fillStyle = "#e3b341"; ctx.fillText("MA30", padL + 34, 12);
  ctx.fillStyle = "#8b949e";
  const step = Math.max(1, Math.ceil(bars.length / 8));
  for(let i=0;i<bars.length;i+=step){
    const cx = padL + slot*i + slot/2;
    ctx.fillText(bars[i].date.slice(5), cx - 14, cssH - 8);
  }
}

async function loadKline(){
  const name = $("klineItem").value.trim();
  if(!name){ $("klineMetrics").innerHTML =
    '<span class="sub">先输入饰品名（可从概览页复制，或用 datalist 建议）</span>'; return; }
  const platform = $("klinePlatform").value, days = $("klineDays").value;
  try{
    const d = await get(`/api/kline/${encodeURIComponent(name)}?platform=${platform}&days=${days}`);
    chartDrawn = true;
    drawChart(d.bars || []);
    const ind = d.indicators || {};
    const ma = ind.ma || {};
    const boll = ind.bollinger || {};
    $("klineMetrics").innerHTML = [
      ["现价", fmt(ind.last)],
      ["MA7", fmt(ma.ma7)], ["MA30", fmt(ma.ma30)], ["MA90", fmt(ma.ma90)],
      ["RSI(14)", fmt(ind.rsi14)],
      ["布林带宽", boll.width != null ? (boll.width*100).toFixed(1)+"%" : "—"],
      ["年化波动率", ind.annualized_volatility != null ? pct(ind.annualized_volatility,0) : "—"],
      ["年化收益率", pct(ind.annualized_return)],
      ["7 日动量", pct(ind.momentum_7)],
      ["最大回撤", pct(ind.max_drawdown)],
      ["7 日均价基准", fmt(d.baseline_median_7d)],
      ["日线根数", (d.bars||[]).length],
    ].map(([k,v]) => `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
    $("klineReadings").innerHTML = (d.readings||[]).map(t => "· " + esc(t)).join("<br>")
      || '<span class="sub">数据点不足以上指标（至少需要 2 个交易日、30 个采样点）。多跑几轮采集后回来查看。</span>';
  }catch(e){
    $("klineMetrics").innerHTML = `<span class="neg">加载失败：${esc(e.message)}</span>`;
  }
}

// ── 套利 ───────────────────────────────────────────────────
let spreadDrawn = false;
async function loadSpread(){
  const mp = (parseFloat($("spMinPct").value)||3)/100;
  const mpr = parseFloat($("spMinProfit").value)||1;
  try{
    const d = await get(`/api/spread?min_percent=${mp}&min_profit=${mpr}`);
    spreadDrawn = true;
    $("spNote").textContent = `命中 ${d.count} 条，其中可即时成交 ${d.executable_count} 条`;
    if(!d.items.length){
      $("spread").innerHTML = '<div class="empty">当前没有满足阈值的价差。多积累几轮数据、或放宽阈值再试。</div>';
      return;
    }
    const head = `<tr><th>饰品</th><th>买入平台</th><th>买价</th><th>卖出平台</th>
      <th>卖价</th><th>净收益</th><th>净收益率</th><th>可即时成交</th><th>手续费口径</th></tr>`;
    $("spread").innerHTML = `<table>${head}` + d.items.map(r => `<tr>
      <td>${esc(r.market_hash_name)}</td>
      <td>${platTag(r.buy_platform)}</td><td>${fmt(r.buy_price)}</td>
      <td>${platTag(r.sell_platform)}</td><td>${fmt(r.sell_price)}</td>
      <td class="neg">${fmt(r.net_profit)}</td>
      <td class="neg">${pct(r.net_percent)}</td>
      <td>${r.executable ? '<span class="tag" style="color:#3fb950;border-color:#1c3a24">是</span>'
                          : '<span class="tag">否（需挂单等）</span>'}</td>
      <td class="sub">${esc(r.fee_note)}</td>
    </tr>`).join("") + "</table>";
  }catch(e){
    $("spread").innerHTML = `<div class="empty neg">加载失败：${esc(e.message)}</div>`;
  }
}

// ── 关注清单 ───────────────────────────────────────────────
let focusDrawn = false;

const STATUS_STYLE = {
  ready:  'color:#3fb950;border-color:#1c3a24',
  near:   'color:#e3b341;border-color:#5c4a1a',
  waiting:'color:#8b949e',
  no_data:'color:#8b949e',
  tracking:'color:#8b949e',
};

function renderFocus(d){
  focusDrawn = true;
  const c = d.counts || {};
  document.getElementById("focusSummary").innerHTML =
    `买入 ${c.buy||0} ｜ 卖出 ${c.sell||0} ｜ 观察 ${c.watch||0}` +
    (d.actionable ? ` ｜ <span class="neg">⚡ ${d.actionable} 个已达标</span>` : "");

  const el = document.getElementById("focus");
  const groups = [["买入", d.buy||[]],["卖出", d.sell||[]],["观察", d.watch||[]]]
    .filter(g => g[1].length);
  if(!groups.length){
    el.innerHTML = '<div class="empty">关注清单为空。用命令行添加，例如：<br>' +
      '<code>python -m csmon focus add "AK-47 | Redline" --intent buy --target 95 --wears FT,MW</code></div>';
    return;
  }
  el.innerHTML = groups.map(([title, rows]) => `
    <h2 style="margin-top:16px">${title}</h2>
    <table><tr><th>状态</th><th>饰品</th><th>现价</th><th>目标</th>
      <th>平台</th><th>磨损</th><th>品质</th><th>档位</th><th>在售量</th><th>进度</th></tr>` +
    rows.map(r => `<tr>
      <td><span class="tag" style="${STATUS_STYLE[r.status]||''}">${esc(r.status_cn)}</span></td>
      <td>${esc(r.display_name)}${r.note ? `<br><span class="sub">${esc(r.note)}</span>` : ""}</td>
      <td>${fmt(r.current_price)}</td>
      <td>${fmt(r.target_price)}</td>
      <td>${r.current_platform ? platTag(r.current_platform) : "—"}</td>
      <td>${esc(r.wear_cn || "—")}</td>
      <td>${esc(r.quality_cn || "普通")}${r.is_star ? " ★" : ""}</td>
      <td>${esc(r.pattern_label || "—")}</td>
      <td>${r.sell_count ?? "—"}</td>
      <td>${r.progress != null ? Math.round(r.progress*100)+"%" : "—"}</td>
    </tr>`).join("") + "</table>").join("");
}

async function loadVariants(){
  const base = document.getElementById("variantBase").value.trim();
  if(!base){ document.getElementById("variantHint").textContent = "先输入基础名"; return; }
  try{
    const d = await get(`/api/variants/${encodeURIComponent(base)}`);
    const hint = document.getElementById("variantHint");
    hint.textContent = `共 ${d.groups.length} 个基础饰品`;
    document.getElementById("variants").innerHTML = d.groups.map(g => `
      <h2 style="margin-top:14px">${esc(g.display_name || g.base)}
        ${g.variant_rich ? '<span class="tag" style="color:#e3b341">变体影响价格</span>' : ""}
      </h2>
      <table><tr><th>变体（中文）</th><th>磨损</th><th>品质</th><th>星标</th><th>市场名</th></tr>` +
      g.variants.map(v => `<tr>
        <td>${esc(v.wear_cn || "无磨损档")}</td>
        <td>${esc(v.wear || "—")}</td>
        <td>${esc(v.quality_cn || "普通")}</td>
        <td>${v.is_star ? "★" : ""}</td>
        <td class="sub">${esc(v.raw_name)}</td>
      </tr>`).join("") + "</table>").join("");
  }catch(e){
    document.getElementById("variants").innerHTML =
      `<div class="empty neg">加载失败：${esc(e.message)}</div>`;
  }
}

// ── 租赁收益 ───────────────────────────────────────────────
let rentDrawn = false;

const VERDICT_STYLE = {
  good:'color:#3fb950;border-color:#1c3a24',
  marginal:'color:#e3b341;border-color:#5c4a1a',
  poor:'color:#f85149;border-color:#5c1c1c',
  unknown:'color:#8b949e',
};

async function loadRent(){
  const minLiq = parseFloat(document.getElementById("rentMinLiq").value) || 0;
  try{
    const d = await get(`/api/rent?limit=60&min_liquidity=${minLiq}`);
    rentDrawn = true;
    const s = d.stats || {};
    document.getElementById("rentNote").innerHTML =
      `租赁库 ${s.snapshots||0} 条快照 / ${s.items_with_rent||0} 个饰品有租价` +
      `　｜　费率口径：租赁抽成 ${(d.assumptions.rent_fee.YOUPIN*100).toFixed(0)}%` +
      ` · 卖出 ${(d.assumptions.sell_fee.YOUPIN*100).toFixed(1)}%` +
      ` · 提现 ${(d.assumptions.withdraw_fee*100).toFixed(1)}%` +
      `　｜　出租率缺省假设 ${(d.assumptions.occupancy_fallback*100).toFixed(0)}%`;

    const el = document.getElementById("rent");
    if(!d.items.length){
      el.innerHTML = '<div class="empty">还没有租赁数据。<br>' +
        '采集：<code>python -m csmon rent scan</code>（需 CSQAQ Token）</div>';
      return;
    }
    el.innerHTML = `<table><tr><th>年化</th><th>风险调整</th><th>模式</th>
      <th>持有天数</th><th>日租金</th><th>出租率</th><th>波动率</th>
      <th>流动性</th><th>结论</th><th>饰品</th></tr>` +
      d.items.map(r => `<tr style="cursor:pointer"
          onclick="loadRentDetail('${esc(r.market_hash_name).replace(/'/g,"\\'")}')">
        <td class="${r.annualized_pct>=0?'neg':'pos'}"><b>${r.annualized_pct.toFixed(1)}%</b></td>
        <td>${r.risk_adjusted_pct != null ? r.risk_adjusted_pct.toFixed(2) : "—"}</td>
        <td>${esc(r.mode_cn)}</td>
        <td>${r.horizon_days}</td>
        <td>${fmt(r.daily_rent, 2)}</td>
        <td>${(r.occupancy*100).toFixed(0)}%</td>
        <td>${r.volatility_pct != null ? r.volatility_pct.toFixed(1)+"%" : "—"}</td>
        <td>${r.liquidity_score != null ? r.liquidity_score.toFixed(0) : "—"}</td>
        <td><span class="tag" style="${VERDICT_STYLE[r.verdict]||''}">${esc(r.verdict_cn)}</span></td>
        <td>${esc(r.display_name || r.market_hash_name)}</td>
      </tr>`).join("") + "</table>" +
      `<div class="empty" style="margin-top:14px;text-align:left">
         <b>怎么看这张表</b><br>
         · <b>年化</b> =（净租金 + 价格变动 − 卖出成本）÷ 买入价，按持有天数年化 —— 点任意一行看完整分解<br>
         · <b>出租率</b> 由「平台年化 ÷ 理论年化」推算；平台未给年化时按保守值假设<br>
         · <b>风险调整</b> = 年化 ÷ 年化波动率，越高说明收益相对波动越划算<br>
         · 同一件饰品在不同持有周期下结论可能反转：<b>租金是线性累积的，价格变动不是</b>
       </div>`;
  }catch(e){
    document.getElementById("rent").innerHTML =
      `<div class="empty neg">加载失败：${esc(e.message)}</div>`;
  }
}

async function loadRentDetail(name){
  try{
    const d = await get(`/api/rent/${encodeURIComponent(name)}`);
    const s = d.snapshot || {};
    const v = d.verdict || {};
    const rows = d.scenarios || [];
    document.getElementById("rentDetail").innerHTML = `
      <h2>${esc(d.display_name || name)} — 收益分解</h2>
      <div class="row sub">
        买入价 ${fmt(s.market_price)} ｜ 存世量 ${s.supply ?? "—"}
        ｜ 在售量 ${s.sell_num ?? "—"} ｜ 出租挂单 ${s.lease_listings ?? "—"}
        ｜ 转租价 ${fmt(s.transfer_price)}
      </div>
      <table><tr><th>模式</th><th>天数</th><th>日租</th><th>出租率</th>
        <th>毛租金</th><th>净租金</th><th>价格变动</th><th>卖出成本</th>
        <th>总收益</th><th>年化</th></tr>` +
        rows.map(r => `<tr>
          <td>${esc(r.mode_cn)}</td><td>${r.horizon_days}</td>
          <td>${fmt(r.daily_rent,2)}</td>
          <td>${(r.occupancy*100).toFixed(0)}%</td>
          <td>${fmt(r.gross_rent)}</td><td>${fmt(r.net_rent)}</td>
          <td class="${r.price_move>=0?'neg':'pos'}">${r.price_move>=0?"+":""}${fmt(r.price_move)}</td>
          <td>${fmt(r.exit_cost)}</td>
          <td class="${r.total_return>=0?'neg':'pos'}"><b>${r.total_return>=0?"+":""}${fmt(r.total_return)}</b></td>
          <td class="${r.annualized_pct>=0?'neg':'pos'}"><b>${r.annualized_pct.toFixed(1)}%</b></td>
        </tr>`).join("") + "</table>" +
      `<div class="metrics" style="margin-top:14px">
         ${[["理论年化（满租）", (rows[0]?.theoretical_annual_pct?.toFixed(1) ?? "—")+"%"],
            ["平台口径年化", s.long_annual_pct != null ? s.long_annual_pct+"%" : "—"],
            ["推算出租率", rows[0] ? (rows[0].occupancy*100).toFixed(0)+"%" : "—"],
            ["估计年化波动率", rows[0]?.volatility_pct != null ? rows[0].volatility_pct.toFixed(1)+"%" : "—"],
            ["流动性评分", rows[0]?.liquidity_score != null ? rows[0].liquidity_score.toFixed(0)+"/100" : "—"]
           ].map(([k,val]) => `<div class="metric"><div class="k">${k}</div>
             <div class="v">${val}</div></div>`).join("")}
       </div>
      <div style="margin-top:12px"><b>结论：${esc(v.headline||"—")}</b></div>
      <div class="readings" style="margin-top:8px">
        ${(v.reasons||[]).map(r => "· " + esc(r)).join("<br>")}
        ${(v.caveats||[]).length ? "<br><br>" + (v.caveats||[]).map(c =>
            `<span style="color:#e3b341">! ${esc(c)}</span>`).join("<br>") : ""}
      </div>`;
  }catch(e){
    document.getElementById("rentDetail").innerHTML =
      `<div class="empty neg">加载失败：${esc(e.message)}</div>`;
  }
}

// ── LLM 建议 ───────────────────────────────────────────────
const ADVICE_STYLE = {
  buy:'color:#3fb950;border-color:#1c3a24',
  sell:'color:#f85149;border-color:#5c1c1c',
  hold:'color:#58a6ff;border-color:#1f3d5c',
  avoid:'color:#e3b341;border-color:#5c4a1a',
  watch:'color:#8b949e',
};

async function loadAdvice(){
  try{
    const d = await get("/api/advice?limit=40");
    const llm = d.llm || {};
    document.getElementById("llmStatus").innerHTML = llm.configured
      ? `模型：${esc(llm.model)}（${esc(llm.provider)}）`
      : `<span class="neg">LLM 未配置</span> —— 在 .env 里设置 CSMON_LLM_PRESET 与 CSMON_LLM_API_KEY，然后 <code>python -m csmon advice probe</code>`;

    const el = document.getElementById("advice");
    if(!d.items.length){
      el.innerHTML = '<div class="empty">还没有生成建议。<br>' +
        '先在关注清单里加标的，然后运行 <code>python -m csmon advice ask</code></div>';
      return;
    }
    el.innerHTML = `<table><tr><th>时间</th><th>饰品</th><th>动作</th><th>把握</th>
      <th>目标买</th><th>目标卖</th><th>止损</th><th>周期</th><th>模型</th></tr>` +
      d.items.map(r => `<tr>
        <td class="sub">${esc((r.created_at||"").replace("T"," ").slice(0,19))}</td>
        <td>${esc(r.market_hash_name)}</td>
        <td><span class="tag" style="${ADVICE_STYLE[r.action]||''}">${esc(r.action)}</span></td>
        <td>${r.confidence != null ? (r.confidence*100).toFixed(0)+"%" : "—"}</td>
        <td>${fmt(r.target_buy)}</td><td>${fmt(r.target_sell)}</td>
        <td>${fmt(r.stop_loss)}</td>
        <td>${r.horizon_days != null ? r.horizon_days+"天" : "—"}</td>
        <td class="sub">${esc(r.model)}</td>
      </tr>`).join("") + "</table>" +
      `<div class="sub" style="margin-top:14px;line-height:1.9">
        ${d.items.slice(0,6).map(r => `<div><b>${esc(r.market_hash_name)}</b> ——
          ${esc(r.reasoning || "")}<br>
          <span style="color:#e3b341">风险：${esc(r.risks || "—")}</span></div>`).join("")}
      </div>
      <div class="empty" style="margin-top:16px;text-align:left">
        这些是模型的第二意见，<b>不是投资建议</b>。CS2 饰品流动性差、单件差异大，
        模型看不到赛事日程、版本更新等关键信息 —— 请自己核对数据后再决定。
      </div>`;
  }catch(e){
    document.getElementById("advice").innerHTML =
      `<div class="empty neg">加载失败：${esc(e.message)}</div>`;
  }
}

// ── 涨跌 / 流动性 ──────────────────────────────────────────
function renderMovers(d){
  const block = (title, rows) => {
    if(!rows.length) return `<div class="empty">${title}：数据不足</div>`;
    return `<table><tr><th>${title}</th><th>平台</th><th>现价</th><th>变动</th></tr>` +
      rows.map(r => `<tr>
        <td>${esc(r.market_hash_name)}</td>
        <td>${platTag(r.platform)}</td>
        <td>${fmt(r.current)}</td>
        <td class="${r.change_percent>=0?"pos":"neg"}">${pct(r.change_percent,2)}</td>
      </tr>`).join("") + "</table>";
  };
  $("movers").innerHTML = block("涨幅榜", d.gained||[]) + "<div style='height:16px'></div>" +
                          block("跌幅榜", d.lost||[]);
}

function renderLiquidity(rows){
  if(!rows.length){ $("liquidity").innerHTML = '<div class="empty">暂无数据</div>'; return; }
  $("liquidity").innerHTML = `<table><tr><th>饰品</th><th>平台</th><th>在售量</th>
    <th>在售价</th><th>求购价</th></tr>` + rows.map(r => `<tr>
    <td>${esc(r.market_hash_name)}</td><td>${platTag(r.platform)}</td>
    <td>${r.sell_count}</td><td>${fmt(r.sell_price)}</td><td>${fmt(r.bid_price)}</td>
  </tr>`).join("") + "</table>";
}

// ── 告警 / 源 ──────────────────────────────────────────────
function renderAlerts(rows){
  if(!rows.length){ $("alerts").innerHTML = '<div class="empty">暂无告警</div>'; return; }
  $("alerts").innerHTML = `<table><tr><th>时间</th><th>等级</th><th>饰品</th><th>平台</th>
    <th>规则</th><th>说明</th></tr>` + rows.map(r => `<tr>
    <td class="sub">${esc((r.created_at||"").replace("T"," ").slice(0,19))}</td>
    <td class="sev-${esc(r.severity)}">${esc(r.severity)}</td>
    <td>${esc(r.market_hash_name)}</td><td>${platTag(r.platform)}</td>
    <td>${esc(r.rule)}</td><td>${esc(r.message)}</td>
  </tr>`).join("") + "</table>";
}

function renderSources(rows){
  if(!rows.length){ $("sources").innerHTML = '<div class="empty">还没有采集记录</div>'; return; }
  $("sources").innerHTML = `<table><tr><th>源</th><th>成功率</th><th>成功/请求</th>
    <th>报价</th><th>最后运行</th><th>错误</th></tr>` + rows.map(r => {
    const rate = r.requested ? Math.round(100*r.succeeded/r.requested) : 0;
    let err = "";
    try { err = (JSON.parse(r.errors||"[]")||[]).slice(0,1).join(""); }
    catch(e){ err = r.errors||""; }
    return `<tr><td>${esc(r.source)}</td>
      <td><span class="bar"><i style="width:${rate}%"></i></span> ${rate}%</td>
      <td>${r.succeeded}/${r.requested}</td><td>${r.quotes}</td>
      <td class="sub">${esc((r.started_at||"").replace("T"," ").slice(0,19))}</td>
      <td class="sub">${esc(String(err).slice(0,70))}</td></tr>`;
  }).join("") + "</table>";
}

function renderExtreme(d){
  if(!d.task_count){
    $("extreme").innerHTML = '<div class="empty">没有追踪任务。<br>用 <code>python -m csmon extreme add "饰品名" --interval 30</code> 添加</div>';
    return;
  }
  const s = d.stats || {};
  $("extreme").innerHTML =
    `<div class="sub" style="margin-bottom:8px">
       运行中：${d.running ? "是" : "否"} ｜ 采样 ${s.ticks||0} 次 ｜
       变动 ${s.changes||0} 次 ｜ 降频 ${s.backoffs||0} 次</div>` +
    `<table><tr><th>饰品</th><th>平台</th><th>间隔</th><th>当前间隔</th><th>采样</th></tr>` +
    d.tasks.map(t => `<tr><td>${esc(t.market_hash_name)}</td><td>${platTag(t.platform)}</td>
      <td>${t.interval_seconds}s</td><td>${t.current_interval}s</td><td>${t.ticks}</td></tr>`
    ).join("") + "</table>";
}

// ── 动作 ───────────────────────────────────────────────────
async function runOnce(){
  const btn = event.target; btn.disabled = true; btn.textContent = "采集中…";
  try{
    const d = await (await fetch("/api/run", {method:"POST"})).json();
    btn.textContent = `完成（报价 ${d.quotes} / 告警 ${d.alerts}）`;
    await refreshAll();
  }catch(e){ btn.textContent = "失败"; }
  setTimeout(() => { btn.disabled = false; btn.textContent = "立即采集"; }, 2500);
}

async function loadItemList(){
  try{
    const rows = await get("/api/items?limit=300");
    const names = [...new Set(rows.map(r => r.market_hash_name).filter(Boolean))];
    $("itemList").innerHTML = names.map(n => `<option value="${esc(n)}">`).join("");
  }catch(e){}
}

async function refreshAll(){
  try{
    const [stats, quotes, alerts, sources, extreme] = await Promise.all([
      get("/api/stats"), get("/api/quotes?limit=300"), get("/api/alerts?limit=50"),
      get("/api/sources"), get("/api/extreme"),
    ]);
    renderCards(stats); renderQuotes(quotes); renderAlerts(alerts);
    renderSources(sources); renderExtreme(extreme);

    const [movers, liq] = await Promise.all([
      get("/api/movers?hours=168&limit=12"), get("/api/liquidity?limit=20")]);
    renderMovers(movers); renderLiquidity(liq);

    if(current === "focus") loadFocus();
    if(current === "kline" && chartDrawn) loadKline();
    if(current === "spread" && spreadDrawn) loadSpread();
  }catch(e){
    $("meta").textContent = "加载失败：" + e.message;
  }
}

(async function init(){
  buildTabs();
  try{
    const p = await get("/api/platform");
    $("footer").textContent =
      `运行于 ${p.os} / ${p.arch}｜Python ${p.python_version}｜` +
      `并发 ${p.max_fetch_workers}｜默认轮询 ${p.default_poll_interval}s` +
      (p.is_pi ? "｜Raspberry Pi" : "") +
      ((p.notes||[]).length ? "｜" + p.notes.join("；") : "");
  }catch(e){ $("footer").textContent = ""; }
  loadItemList();
  await loadFocus();          // 关注页是默认页，首屏就要有内容
  await refreshAll();
  setInterval(refreshAll, 30000);
})();
</script>
</body>
</html>
"""
