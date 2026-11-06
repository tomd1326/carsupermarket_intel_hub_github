// Ad-Hoc Analysis > Deliveries: Buyer Estimates vs Original vs Actual (ad_hoc_buyer_estimates.html).
// The server sends one row per delivery (routers/ad_hoc_analysis.py). Every chart is worked out here
// from those rows, so the charts and the cars behind each bar always agree.

const BASE = window.BASE_URL || "";
const HEX = { buyer: "#156082", original: "#e97132", actual: "#196b24", total: "#0f9ed5", units: "#a02b93", dis: "#0f9ed5", dts: "#a02b93" };
const BAR_OPACITY = 0.7;
// Standard admin fee inc VAT. The rows hold the three prices with it added; the chart tables below hold
// them without it, and the price charts add it back.
const ADMIN_FEE = 199;

// ---------- Chart tables, built from the rows in buildTables() ----------
// D rows: [period type, period start, sales type, price cars, buyer rrp, original price, selling price after discount,
//  prep cars, buyer est prep, actual prep, metal cars, buyer metal, original metal, actual metal,
//  gpu cars, buyer gpu-m, original gpu-m, actual gpu-m, channel]
// U, FULL_SELL, DAYS, OVER60 are keyed "type|period|sales type|channel".
let D = [], U = {}, FULL_SELL = {}, DAYS = {}, OVER60 = {};
const over60 = r => { const v = OVER60[r[0] + "|" + r[1] + "|" + r[2] + "|" + r[18]]; return v == null ? null : v; };
const days = (r, i) => { const v = DAYS[r[0] + "|" + r[1] + "|" + r[2] + "|" + r[18]]; return v ? v[i] : null; };
const fullSell = r => { const v = FULL_SELL[r[0] + "|" + r[1] + "|" + r[2] + "|" + r[18]]; return v == null ? null : v; };

// Which cars sit behind each section's bars: only cars with every figure that section compares
const HAS_PRICE = v => v.BUYER_RRP > 0 && v.ORIGINAL_PRICE > 0 && v.SELLING_PRICE > 0;
const HAS_PREP = v => v.BUYER_RECON != null && v.ACTUAL_RECON != null;
const HAS_METAL = v => v.BUYER_METAL != null && v.ORIGINAL_METAL != null && v.ACTUAL_METAL != null;
const HAS_GPU = v => v.BUYER_GPU_M != null && v.ORIGINAL_GPU_M != null && v.ACTUAL_GPU_M != null;

function buildTables(list) {
  const groups = {};
  const avg = (s, n) => n ? s / n : null;
  list.forEach(v => {
    [["M", v._m], ["W", v._w]].forEach(([pt, p]) => {
      [v.SALES_TYPE, "All"].forEach(b => [v.CHANNEL, "All"].forEach(c => {
        const k = pt + "|" + p + "|" + b + "|" + c;
        const g = groups[k] || (groups[k] = { pt, p, b, c, all: 0, withRrp: 0,
          pn: 0, rrp: 0, orig: 0, sell: 0, full: 0, disN: 0, dis: 0, dtsN: 0, dts: 0, o60: 0,
          prn: 0, bPrep: 0, aPrep: 0, mn: 0, bm: 0, om: 0, am: 0, gn: 0, bg: 0, og: 0, ag: 0 });
        g.all++;
        if (v.BUYER_RRP != null) g.withRrp++;
        if (HAS_PRICE(v)) {
          g.pn++;
          g.rrp += v.BUYER_RRP - ADMIN_FEE;
          g.orig += v.ORIGINAL_PRICE - ADMIN_FEE;
          g.full += v.SELLING_PRICE - ADMIN_FEE;
          g.sell += v.SELLING_PRICE - ADMIN_FEE - (v.DISCOUNT || 0);
          if (v.DAYS_IN_STOCK != null) { g.disN++; g.dis += v.DAYS_IN_STOCK; if (v.DAYS_IN_STOCK > 60) g.o60++; }
          if (v.DAYS_TO_SELL != null) { g.dtsN++; g.dts += v.DAYS_TO_SELL; }
        }
        if (HAS_PREP(v)) { g.prn++; g.bPrep += v.BUYER_RECON; g.aPrep += v.ACTUAL_RECON; }
        if (HAS_METAL(v)) { g.mn++; g.bm += v.BUYER_METAL; g.om += v.ORIGINAL_METAL; g.am += v.ACTUAL_METAL; }
        if (HAS_GPU(v)) { g.gn++; g.bg += v.BUYER_GPU_M; g.og += v.ORIGINAL_GPU_M; g.ag += v.ACTUAL_GPU_M; }
      }));
    });
  });
  D = []; U = {}; FULL_SELL = {}; DAYS = {}; OVER60 = {};
  Object.keys(groups).forEach(k => {
    const g = groups[k];
    D.push([g.pt, g.p, g.b, g.pn, avg(g.rrp, g.pn), avg(g.orig, g.pn), avg(g.sell, g.pn),
      g.prn, avg(g.bPrep, g.prn), avg(g.aPrep, g.prn), g.mn, avg(g.bm, g.mn), avg(g.om, g.mn), avg(g.am, g.mn),
      g.gn, avg(g.bg, g.gn), avg(g.og, g.gn), avg(g.ag, g.gn), g.c]);
    U[k] = [g.all, g.withRrp];
    if (g.pn) {
      FULL_SELL[k] = g.full / g.pn;
      // Over 60 days is a share of all the price cars, as in the first version of this report
      OVER60[k] = 100 * g.o60 / g.pn;
    }
    if (g.disN || g.dtsN) DAYS[k] = [avg(g.dis, g.disN), avg(g.dts, g.dtsN)];
  });
}

// The bar pieces for one period. Normal charts: each series grows from the baseline.
// Waterfall charts: step 1 grows from zero, step 2 starts where step 1 ended, then the total from zero.
function segs(sec, r) {
  if (!sec.waterfall) {
    return sec.series.map(s => { const v = s.get(r); return (v == null || isNaN(v)) ? null : { from: null, to: v, val: v }; });
  }
  const a = sec.series[0].get(r), b = sec.series[1].get(r);
  if (a == null || b == null || isNaN(a) || isNaN(b)) return [null, null, null];
  return [{ from: 0, to: a, val: a }, { from: a, to: a + b, val: b }, { from: 0, to: a + b, val: a + b }];
}
const S = (name, key, get) => ({ name, key, get });

const SECTIONS = [
  { title: "Deliveries (Units)",
    note: "All deliveries, and how many have a buyer RRP for the sales type they sold on (their 0% RRP for 0% deals). The gap is deliveries the buyer figures could not be matched to. Few 0% deliveries have a 0% RRP on the Cars Bought sheet.",
    units: true, count: true,
    n: r => r[3],
    series: [S("All Deliveries", "units", r => r[3]), S("With A Buyer RRP", "buyer", r => r[4])] },
  { title: "Buyer RRP vs Original Price vs Selling Price",
    note: "Only cars with all three prices. The buyer RRP is their 0% RRP for 0% deals and their normal RRP otherwise. All prices inc VAT and the standard £199 admin fee, with no discounts taken off. Selling price is the full price the car sold at, before any deal discount.",
    n: r => r[3],
    series: [S("Buyer RRP", "buyer", r => r[4] == null ? null : r[4] + ADMIN_FEE), S("Original Price", "original", r => r[5] == null ? null : r[5] + ADMIN_FEE),
      S("Selling Price", "actual", r => fullSell(r) == null ? null : fullSell(r) + ADMIN_FEE)] },
  { title: "Price Changes (Waterfall)",
    note: "A bar above zero means the price was increased on average; below zero means it was reduced. Step 1: original price minus buyer RRP. Step 2 carries on from where step 1 ends: selling price minus original price. The third bar is the whole gap, selling price minus buyer RRP. Same cars as above.",
    n: r => r[3], waterfall: true,
    series: [S("Buyer RRP To Original", "original", r => (r[4] == null || r[5] == null) ? null : r[5] - r[4]),
      S("Original To Selling", "actual", r => (fullSell(r) == null || r[5] == null) ? null : fullSell(r) - r[5]),
      S("Total: Buyer RRP To Selling", "total", r => (fullSell(r) == null || r[4] == null) ? null : fullSell(r) - r[4])] },
  { title: "Days In Stock And Days To Sell",
    note: "Average days in stock (DIS) and days to sell (DTS) at order, the same measures as the Orders & Deliveries page. Same deliveries as the price charts above. The longer a car sits, the more price reductions it tends to get.",
    days: true,
    n: r => r[3],
    series: [S("Days In Stock", "dis", r => days(r, 0)), S("Days To Sell", "dts", r => days(r, 1))] },
  { title: "Share Of Deliveries Over 60 Days In Stock",
    note: "Percentage of the same deliveries that were over 60 days in stock when ordered. Older stock carries more price reductions (see Matthew's WK43 GPU-M email).",
    pct: true,
    n: r => r[3],
    series: [S("Over 60 Days In Stock", "dis", r => over60(r))] },
  { title: "Buyer Est Prep vs Actual Prep",
    note: "Only cars with both figures. Actual prep is the AutoView recon cost, or the Apex vehicle costs where AutoView has no match.",
    n: r => r[7],
    series: [S("Buyer Est Prep", "buyer", r => r[8]), S("Actual Prep", "actual", r => r[9])] },
  { title: "Prep Gap (Actual Minus Buyer Est)",
    note: "Positive means prep cost more than the buyer estimated.",
    n: r => r[7],
    series: [S("Prep Gap", "actual", r => (r[8] == null || r[9] == null) ? null : r[9] - r[8])] },
  { title: "Buyer Est Metal vs Original Metal vs Actual Metal",
    note: "Only cars with all three. Buyer est metal uses the buyer RRP and their est prep plus the actual other AutoView costs. Original metal uses the original price. All three use the actual purchase price, finance subsidy and write-down.",
    n: r => r[10],
    series: [S("Buyer Est Metal", "buyer", r => r[11]), S("Original Metal", "original", r => r[12]), S("Actual Metal", "actual", r => r[13])] },
  { title: "Metal Changes (Waterfall)",
    note: "Step 1: original metal minus buyer est metal. Step 2 carries on from where step 1 ends: actual metal minus original metal. The third bar is the whole gap, actual metal minus buyer est metal.",
    n: r => r[10], waterfall: true,
    series: [S("Buyer To Original", "original", r => (r[11] == null || r[12] == null) ? null : r[12] - r[11]),
      S("Original To Actual", "actual", r => (r[12] == null || r[13] == null) ? null : r[13] - r[12]),
      S("Total: Buyer To Actual", "total", r => (r[11] == null || r[13] == null) ? null : r[13] - r[11])] },
  { title: "Buyer Est GPU-M vs Original GPU-M vs Actual GPU-M",
    note: "Metal plus the same standard GPU-M costs for all three, so the gaps match the metal changes above.",
    n: r => r[14],
    series: [S("Buyer Est GPU-M", "buyer", r => r[15]), S("Original GPU-M", "original", r => r[16]), S("Actual GPU-M", "actual", r => r[17])] }
];
[() => true, HAS_PRICE, HAS_PRICE, HAS_PRICE, HAS_PRICE, HAS_PREP, HAS_PREP, HAS_METAL, HAS_METAL, HAS_GPU].forEach((f, i) => SECTIONS[i].has = f);

// Page order: the order the sections are written in above, unless moved with the arrows on each
// title; the order is kept in this browser.
const ORDER_KEY = "buyer_est_section_order";
let sectionOrder = SECTIONS.map(s => s.title);
try {
  const saved = JSON.parse(localStorage.getItem(ORDER_KEY) || "null");
  if (Array.isArray(saved)) {
    const known = saved.filter(t => sectionOrder.indexOf(t) >= 0);
    sectionOrder = known.concat(sectionOrder.filter(t => known.indexOf(t) < 0));
  }
} catch (e) { /* keep the default order */ }
function orderedSections() { return sectionOrder.map(t => SECTIONS.find(s => s.title === t)); }
function moveSection(title, dir) {
  const i = sectionOrder.indexOf(title), j = i + dir;
  if (i < 0 || j < 0 || j >= sectionOrder.length) return;
  const t = sectionOrder[i]; sectionOrder[i] = sectionOrder[j]; sectionOrder[j] = t;
  try { localStorage.setItem(ORDER_KEY, JSON.stringify(sectionOrder)); } catch (e) { /* not kept, still works */ }
  build();
  const moved = Array.from(document.querySelectorAll("#main .chart-row")).find(el => el.dataset.title === title);
  if (moved) moved.scrollIntoView({ block: "center" });
}
// Value formats: pounds everywhere except the Units chart, which counts deliveries
const fmtFull = (sec, v) => sec.pct ? (v == null ? "-" : v.toFixed(1) + "%") : sec.days ? (v == null ? "-" : v.toFixed(1) + " days") : sec.count ? (v == null ? "-" : Math.round(v).toLocaleString("en-GB")) : money(v);
const fmtShort = (sec, v) => sec.pct ? (v == null ? "" : v.toFixed(1) + "%") : sec.days ? (v == null ? "" : v.toFixed(1)) : sec.count ? (v == null ? "" : Math.round(v).toLocaleString("en-GB")) : moneyShort(v);
const fmtAxis = (sec, v) => sec.pct ? Math.round(v) + "%" : sec.days ? String(Math.round(v)) : sec.count ? Math.round(v).toLocaleString("en-GB") : axisMoney(v);

const WEEKS_SHOWN = 8;
let bucket = "All";
let channel = "All";
// Average lines on or off for every chart: clicking "Dashed: Overall Average" in any legend switches them, remembered in this browser
const AVG_SHOW_KEY = "buyer_est_show_avg";
let showAvg = true;
try { showAvg = localStorage.getItem(AVG_SHOW_KEY) !== "0"; } catch (e) { showAvg = true; }
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
// ISO week number (weeks start Monday; week 1 holds the year's first Thursday)
function isoWeek(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  return Math.ceil(((t - yearStart) / 86400000 + 1) / 7);
}
function periodLabel(pt, p) {
  const d = new Date(p + "T00:00:00");
  // Weeks: week number then the short Monday date, e.g. "W44 26-Oct"
  return pt === "M" ? MONTHS[d.getMonth()] + " " + d.getFullYear() : "W" + String(isoWeek(d)).padStart(2, "0") + " " + weekStart(p);
}
function weekStart(p) {
  const d = new Date(p + "T00:00:00");
  return String(d.getDate()).padStart(2, "0") + "-" + MONTHS[d.getMonth()];
}
function money(v) { if (v == null || isNaN(v)) return "-"; const s = Math.round(Math.abs(v)).toLocaleString("en-GB"); return (v < 0 ? "-£" : "£") + s; }
function moneyShort(v) {
  if (v == null || isNaN(v)) return "";
  const a = Math.abs(v);
  const s = a >= 10000 ? (a / 1000).toFixed(1) + "k" : Math.round(a).toLocaleString("en-GB");
  return (v < 0 ? "-£" : "£") + s;
}
function axisMoney(v) {
  const a = Math.abs(v);
  const s = a >= 1000 ? (a / 1000).toLocaleString("en-GB", { maximumFractionDigits: 1 }) + "k" : String(Math.round(a));
  return (v < 0 ? "-£" : "£") + s;
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function barPath(x, w, y0, yv, r) {
  const h = Math.abs(y0 - yv);
  r = Math.min(r, w / 2, h);
  if (h < 0.5) return "M" + x + "," + y0 + "H" + (x + w);
  if (yv < y0) {
    return "M" + x + "," + y0 + "V" + (yv + r) + "Q" + x + "," + yv + " " + (x + r) + "," + yv +
      "H" + (x + w - r) + "Q" + (x + w) + "," + yv + " " + (x + w) + "," + (yv + r) + "V" + y0 + "Z";
  }
  return "M" + x + "," + y0 + "V" + (yv - r) + "Q" + x + "," + yv + " " + (x + r) + "," + yv +
    "H" + (x + w - r) + "Q" + (x + w) + "," + yv + " " + (x + w) + "," + (yv - r) + "V" + y0 + "Z";
}

const tip = document.getElementById("tip");
function showTip(ev, html) {
  tip.innerHTML = html;
  tip.style.display = "block";
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + w > window.innerWidth - 8) x = ev.clientX - w - pad;
  if (y + h > window.innerHeight - 8) y = ev.clientY - h - pad;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
function hideTip() { tip.style.display = "none"; }
// Clicking a bar shows a small "View Vehicles" button at the click point; pressing it opens the table
const vvBox = document.getElementById("vv");
let vvAction = null;
function showViewBtn(ev, action, label) {
  vvAction = action;
  document.getElementById("vv-label").textContent = label;
  vvBox.style.display = "block";
  const w = vvBox.offsetWidth, h = vvBox.offsetHeight;
  vvBox.style.left = Math.max(6, Math.min(ev.clientX - w / 2, window.innerWidth - w - 6)) + "px";
  vvBox.style.top = Math.max(6, Math.min(ev.clientY + 10, window.innerHeight - h - 6)) + "px";
  ev.stopPropagation();
}
function hideViewBtn() { vvBox.style.display = "none"; vvAction = null; }
document.getElementById("vv-btn").addEventListener("click", () => { const a = vvAction; hideViewBtn(); if (a) a(); });
document.addEventListener("click", ev => { if (!ev.target.closest("#vv")) hideViewBtn(); });
document.addEventListener("keydown", ev => { if (ev.key === "Escape") hideViewBtn(); });
window.addEventListener("scroll", hideViewBtn, true);

function drawChart(holder, sec, pt, rows, withLabels, axisMin) {
  holder.innerHTML = "";
  const W = Math.max(320, holder.clientWidth), H = 297;
  // Week charts have more bars, so their labels run vertically and need more room above
  const m = { l: 54, r: 10, t: withLabels ? (pt === "W" ? 46 : 22) : 12, b: 34 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const svg = el("svg", { viewBox: "0 0 " + W + " " + H, height: H, role: "img" }, holder);
  const vals = [];
  rows.forEach(r => segs(sec, r).forEach(g => { if (g) { vals.push(g.to); if (g.from != null) vals.push(g.from); } }));
  if (!rows.length || !vals.length) {
    el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", fill: "#64748b", "font-size": 13 }, svg).textContent = "No cars with all figures for this selection";
    return;
  }
  let fixedMin = axisMin != null && !isNaN(axisMin);
  if (!fixedMin) {
    // When every value is positive and they sit close together (e.g. prices around £16k), start the
    // axis a little below the lowest value so the differences show. Otherwise start at zero.
    const vMin = Math.min(...vals), vMax = Math.max(...vals);
    if (vMin > 0 && vMin / vMax > 0.5) {
      const pad = Math.max((vMax - vMin) * 0.6, vMax * 0.02);
      const st = niceStep(Math.max(vMax - (vMin - pad), 1) / 5);
      axisMin = Math.max(0, Math.floor((vMin - pad) / st) * st);
      fixedMin = axisMin > 0;
    }
  }
  let lo = fixedMin ? axisMin : Math.min(0, ...vals);
  let hi = Math.max(fixedMin ? axisMin : 0, ...vals);
  if (hi <= lo) hi = lo + 1;
  const step = niceStep((hi - lo) / 5);
  if (fixedMin) {
    // Keep the chosen minimum exactly; round the top up to a whole number of steps
    hi = lo + Math.ceil((hi - lo) / step) * step;
  } else {
    const minV = lo;
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    // Leave room for the value label under a negative bar that reaches near the bottom
    if (withLabels && minV < 0 && minV - lo < step * 0.35) lo -= step;
  }
  const y = v => m.t + (hi - v) / (hi - lo) * ph;
  for (let t = lo; t <= hi + step / 2; t += step) {
    el("line", { x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: t === 0 ? "#94a3b8" : "#e2e8f0", "stroke-width": 1 }, svg);
    const tx = el("text", { x: m.l - 6, y: y(t) + 4, "text-anchor": "end", fill: "#64748b", "font-size": 11 }, svg);
    tx.textContent = fmtAxis(sec, t);
  }
  const k = sec.series.length, n = rows.length, gap = sec.waterfall ? 6 : 2;
  // The card widths are set so that month and week charts get the same width per period (see layoutPairs)
  const band = pw / n;
  const bw = Math.min(32, (band * 0.78 - gap * (k - 1)) / k);
  // Bars grow from zero, or from the axis minimum when that is above zero
  const base = Math.min(Math.max(0, lo), hi);
  rows.forEach((r, i) => {
    const gx = m.l + i * band;
    const x0 = gx + (band - (k * bw + gap * (k - 1))) / 2;
    const clamp = v => Math.min(Math.max(v, lo), hi);
    const gs = segs(sec, r);
    gs.forEach((g, j) => {
      if (!g) return;
      const s = sec.series[j];
      const x = x0 + j * (bw + gap);
      const from = g.from == null ? base : g.from;
      const yf = y(clamp(from)), yt = y(clamp(g.to));
      el("path", { d: barPath(x, bw, yf, yt, 4), fill: HEX[s.key], "fill-opacity": BAR_OPACITY }, svg);
      // Waterfall: dotted line from the end of this step to the start of the next one
      if (sec.waterfall && j < gs.length - 1 && gs[j + 1]) {
        el("line", { x1: x + bw, x2: x + bw + gap, y1: yt, y2: yt, stroke: "#64748b", "stroke-width": 1, "stroke-dasharray": "2 2" }, svg);
      }
      if (withLabels) {
        const up = g.to >= from;
        if (pt === "W") {
          // Vertical label: reads upwards from just above a positive bar, or ends just below a negative one
          const cx = x + bw / 2 + 3, cy = up ? yt - 4 : yt + 4;
          const lt = el("text", { x: cx, y: cy, "text-anchor": up ? "start" : "end", fill: "#475569", "font-size": 9, transform: "rotate(-90 " + cx + " " + cy + ")" }, svg);
          lt.textContent = fmtShort(sec, g.val);
        } else {
          const ly = up ? yt - 5 : yt + 13;
          const lt = el("text", { x: x + bw / 2, y: ly, "text-anchor": "middle", fill: "#475569", "font-size": 10 }, svg);
          lt.textContent = fmtShort(sec, g.val);
        }
      }
    });
    const lab = el("text", { x: gx + band / 2, y: H - m.b + 16, "text-anchor": "middle", fill: "#64748b", "font-size": 11 }, svg);
    lab.textContent = periodLabel(pt, r[1]);
    const hit = el("rect", { x: gx, y: m.t, width: band, height: ph, fill: "transparent", class: "hit" }, svg);
    hit.addEventListener("click", ev => { hideTip(); showViewBtn(ev, () => openVehicles(sec, pt, r[1]), periodLabel(pt, r[1])); });
    hit.addEventListener("mousemove", ev => {
      let html = '<div class="t">' + periodLabel(pt, r[1]) + (pt === "W" ? " (week commencing Monday)" : "") + "</div>";
      html += '<div class="n">' + sec.n(r).toLocaleString("en-GB") + " deliveries</div>";
      sec.series.forEach(s => {
        html += '<div class="r"><span><i style="background:' + HEX[s.key] + '"></i>' + s.name + "</span><b>" + fmtFull(sec, s.get(r)) + "</b></div>";
      });
      html += '<div class="n" style="margin-top:4px">Click to see the cars</div>';
      showTip(ev, html);
    });
    hit.addEventListener("mouseleave", hideTip);
  });
  // Reference lines: overall average of each series across the periods shown (dashed, in the series colour).
  // Waterfall charts show it for the total only, as the step bars float.
  const labelYs = [];
  if (showAvg) sec.series.forEach((s, j) => {
    if (sec.waterfall && j < sec.series.length - 1) return;
    const a = overallAvg(sec, s, rows);
    if (a == null || a < lo || a > hi) return;
    const ya = y(a);
    el("line", { x1: m.l, x2: W - m.r, y1: ya, y2: ya, stroke: HEX[s.key], "stroke-width": 1.5, "stroke-dasharray": "6 4", "pointer-events": "none" }, svg);
    let ly = ya - 4;
    while (labelYs.some(p => Math.abs(p - ly) < 11)) ly -= 11;
    labelYs.push(ly);
    const t = el("text", { x: W - m.r - 2, y: ly, "text-anchor": "end", fill: HEX[s.key], "font-size": 10, "font-weight": 700, "pointer-events": "none" }, svg);
    t.textContent = "Avg " + fmtShort(sec, a);
  });
}

// Overall average of one series over the given periods: weighted by deliveries,
// except on the Units chart where it is the plain average per period
function overallAvg(sec, s, rows) {
  let sum = 0, w = 0;
  rows.forEach(r => {
    const v = s.get(r);
    if (v == null || isNaN(v)) return;
    const wt = sec.count ? 1 : (sec.n(r) || 0);
    if (!wt) return;
    sum += v * wt;
    w += wt;
  });
  return w ? sum / w : null;
}

// Styled as the Orders & Deliveries summary tables (table.summary); negatives in red
function tableHtml(sec, pt, rows) {
  const td = v => "<td" + (v != null && v < 0 ? ' class="neg"' : "") + ">" + fmtFull(sec, v) + "</td>";
  let h = '<table class="summary"><thead><tr><th>' + (pt === "W" ? "Week" : "Month") + "</th>" + (pt === "W" ? "<th>Week Commencing</th>" : "") + "<th>Deliveries</th>";
  sec.series.forEach(s => h += "<th>" + s.name + "</th>");
  h += "</tr></thead><tbody>";
  rows.forEach(r => {
    h += '<tr><td class="dim-label">' + periodLabel(pt, r[1]) + "</td>" + (pt === "W" ? "<td>" + r[1].split("-").reverse().join("/") + "</td>" : "") + "<td>" + sec.n(r).toLocaleString("en-GB") + "</td>";
    sec.series.forEach(s => h += td(s.get(r)));
    h += "</tr>";
  });
  h += '<tr class="total-row"><td>Overall Average</td>' + (pt === "W" ? "<td></td>" : "") + "<td></td>";
  sec.series.forEach(s => h += td(overallAvg(sec, s, rows)));
  h += "</tr>";
  return h + "</tbody></table>";
}

function build() {
  const main = document.getElementById("main");
  main.innerHTML = "";
  const ordered = orderedSections();
  ordered.forEach((sec, idx) => {
    // One KPI-style chart row per section: title bar, note, then the month and week cards
    const s = document.createElement("div");
    s.className = "chart-row";
    s.dataset.title = sec.title;
    const arrows = '<span class="mv"><button type="button" class="chart-title-btn mv-btn" data-move="-1" data-title="' + sec.title + '" title="Move this chart section up"' + (idx === 0 ? " disabled" : "") + '>&#9650;</button>' +
      '<button type="button" class="chart-title-btn mv-btn" data-move="1" data-title="' + sec.title + '" title="Move this chart section down"' + (idx === ordered.length - 1 ? " disabled" : "") + ">&#9660;</button></span>";
    s.innerHTML = '<div class="chart-row-title"><span class="chart-title-text">' + sec.title + "</span>" + arrows + '</div><p class="note">' + sec.note + "</p>";
    const pair = document.createElement("div");
    pair.className = "pair";
    [["M", "By Month"], ["W", "By Week"]].forEach(([pt, label]) => {
      const rows = (sec.units
        // Units rows: [type, period, sales type, all deliveries, with a buyer RRP, channel]
        ? Object.keys(U).map(k => k.split("|")).filter(p => p[0] === pt && p[2] === bucket && p[3] === channel)
            .map(p => { const u = U[p.join("|")]; return [p[0], p[1], p[2], u[0], u[1], p[3]]; })
        : D.filter(r => r[0] === pt && r[2] === bucket && r[18] === channel)
      ).sort((a, b) => a[1] < b[1] ? -1 : 1);
      // Weeks: only the last 8 are shown
      if (pt === "W" && rows.length > WEEKS_SHOWN) rows.splice(0, rows.length - WEEKS_SHOWN);
      const card = document.createElement("div");
      card.className = "chart-card";
      const avgKey = '<span class="legend-item avg-toggle' + (showAvg ? "" : " off") + '" title="Click to show or hide the average lines on every chart"><span class="legend-dot-dashed"></span>Dashed: Overall Average</span>';
      const legend = '<div class="chart-legend-row"><div class="chart-legend">' + (sec.series.length > 1
        ? sec.series.map(x => '<span class="legend-item"><span class="legend-swatch" style="background:' + HEX[x.key] + '"></span>' + x.name + "</span>").join("")
        : "") + avgKey + "</div></div>";
      card.innerHTML = '<div class="chart-card-head"><h2>' + label + '</h2></div>' + legend + '<div class="chart"></div><details><summary>Show Table</summary>' + tableHtml(sec, pt, rows) + "</details>";
      pair.appendChild(card);
      card._draw = () => drawChart(card.querySelector(".chart"), sec, pt, rows, true, null);
    });
    s.appendChild(pair);
    main.appendChild(s);
  });
  main.insertAdjacentHTML("beforeend", '<p class="foot">Source: PROD_CARSUPERMARKET_DB.PRODUCT_SALES_PERFORMANCE.SALES_PERFORMANCE, read live. ' +
    "Months run May to Oct 2026. Weeks are ISO week numbers, Monday to Sunday; the week charts show the last 8 weeks, W37 (from 7 Sep) to W44 (26 Oct to Sunday 1 Nov). " +
    "The 0% bucket is small (26 to 52 cars a month and 4 to 13 a week with a 0% buyer RRP), so its weekly figures move around a lot, and BCB, PX and Other have almost no 0% deals so that filter shows empty charts for them. " +
    "Where the buyer gave no prep estimate (0 to 35 cars a month), buyer est metal and GPU-M use the actual costs; this moves the averages by less than £5.</p>");
  drawAll();
}
// Sizes the month and week cards so every column is the same width on both charts. Spare room goes to
// the sides (the row is centred), not into the charts.
function layoutPairs() {
  const main = document.getElementById("main");
  const avail = main.clientWidth - 96;          // main has 48px padding each side
  // CARD_EXTRA: .chart-card's 14px padding and 1px border on each side (charts.css)
  const GAP = 16, CARD_EXTRA = 30, SVG_EXTRA = 64, MONTH_COUNT = 6, WEEKS = WEEKS_SHOWN;
  // total width = (6b + 64 + 30) + (8b + 64 + 30) + gap
  let b = (avail - GAP - 2 * (CARD_EXTRA + SVG_EXTRA)) / (MONTH_COUNT + WEEKS);
  b = Math.max(18, Math.min(b, 110));            // not narrower than 18px, not wider than 110px per column
  const cols = (MONTH_COUNT * b + SVG_EXTRA + CARD_EXTRA) + "px " + (WEEKS * b + SVG_EXTRA + CARD_EXTRA) + "px";
  // Narrow windows stack the two charts, so leave the widths to the stylesheet
  const narrow = window.innerWidth <= 1000;
  document.querySelectorAll(".pair").forEach(p => { p.style.gridTemplateColumns = narrow ? "" : cols; });
  // Width of the whole chart row: the notes and headings are lined up to it
  const rowW = (MONTH_COUNT + WEEKS) * b + 2 * (SVG_EXTRA + CARD_EXTRA) + GAP;
  if (narrow) document.documentElement.style.removeProperty("--content-w");
  else document.documentElement.style.setProperty("--content-w", rowW + "px");
}
function drawAll() {
  layoutPairs(); document.querySelectorAll(".chart-card").forEach(c => c._draw && c._draw()); }

// ---------- Vehicles behind each bar ----------
// Popup columns: [key, heading, format, group colour]
const VEH_COLS = [
  ["VRM", "VRM", "vrm"], ["SUPPLIER", "Supplier", "text"], ["CHANNEL", "Channel", "text"], ["MILEAGE", "Mileage", "int"],
  ["REGISTERED", "Registered", "date"], ["PURCHASE_INC_VAT", "Purchase Inc VAT", "money"],
  ["BUYER_RRP", "Buyer RRP", "money", "buyer"], ["BUYER_CHASSIS", "Buyer Est Chassis", "money", "buyer"],
  ["BUYER_RECON", "Buyer Est Recon", "money", "buyer"], ["BUYER_METAL", "Buyer Est Metal", "money", "buyer"],
  ["GPU_M_COSTS", "GPU-M Costs", "money"], ["BUYER_GPU_M", "Buyer Est GPU-M", "money", "buyer"],
  ["ORIGINAL_PRICE", "Original Price", "money", "original"], ["ORIGINAL_CHASSIS", "Original Chassis", "money", "original"],
  ["ORIGINAL_METAL", "Original Metal", "money", "original"], ["ORIGINAL_GPU_M", "Original GPU-M", "money", "original"],
  ["SELLING_PRICE", "Selling Price", "money", "actual"], ["DISCOUNT", "Discount", "money", "actual"],
  ["ACTUAL_CHASSIS", "Actual Chassis", "money", "actual"], ["ACTUAL_RECON", "Actual Recon", "money", "actual"],
  ["AUTOVIEW_OTHER_COSTS", "Other Costs (AutoView)", "money", "actual"], ["APEX_VEHICLE_COSTS", "Apex Vehicle Costs", "money", "actual"],
  ["WRITE_DOWN", "Write Down", "money", "actual"], ["FINANCE_SUBSIDY", "Finance Subsidy", "money", "actual"],
  ["ACTUAL_METAL", "Actual Metal", "money", "actual"], ["ACTUAL_GPU_M", "Actual GPU-M", "money", "actual"],
  ["DAYS_IN_STOCK", "Days In Stock", "int"], ["DAYS_TO_SELL", "Days To Sell", "int"],
  ["SALES_TYPE", "Sales Type", "text"], ["INVOICE_POSTED", "Invoice Posted", "date"], ["MAKE_MODEL", "Make Model", "text"],
  ["STOCK_ID", "Stock ID", "text"]
];
let VEH = [];

function ymd(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
// Rows from the server as objects, with the month and the Monday of the week each was delivered in
function toVehicles(columns, rows) {
  return rows.map(r => {
    const v = {};
    columns.forEach((c, i) => { v[c] = r[i]; });
    v.INVOICE_POSTED = String(v.INVOICE_POSTED || "").slice(0, 10);
    v.REGISTERED = v.REGISTERED ? String(v.REGISTERED).slice(0, 10) : "";
    const d = new Date(v.INVOICE_POSTED + "T00:00:00");
    v._m = v.INVOICE_POSTED.slice(0, 7) + "-01";
    d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
    v._w = ymd(d);
    return v;
  }).filter(v => v.INVOICE_POSTED && v.CHANNEL);
}

function cell(v, fmt) {
  if (v == null || v === "") return "";
  if (fmt === "money") return money(v);
  if (fmt === "int") return Math.round(v).toLocaleString("en-GB");
  if (fmt === "date") { const p = String(v).split("-"); return p.length === 3 ? p[2] + "/" + p[1] + "/" + p[0] : v; }
  return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}
let vehList = [], vehSort = { key: null, dir: 1 };
function renderVehTable() {
  const body = document.getElementById("veh-body");
  // Styled as the Orders & Deliveries vehicle popup (table.vehicle-modal-table)
  let h = '<table class="vehicle-modal-table"><thead><tr>';
  VEH_COLS.forEach(([k, label, fmt, grp]) => {
    const on = vehSort.key === k;
    const num = fmt === "money" || fmt === "int";
    const arrow = '<span class="sort-caret' + (on ? " on" : "") + '">' + (on ? (vehSort.dir > 0 ? "&#9650;" : "&#9660;") : "&#8645;") + "</span>";
    const cls = [num ? "num" : "", grp ? "grp-" + grp : ""].filter(Boolean).join(" ");
    h += '<th data-k="' + k + '"' + (cls ? ' class="' + cls + '"' : "") + ' title="Click to sort">' + label + arrow + "</th>";
  });
  h += "</tr></thead><tbody>";
  vehList.forEach(v => {
    h += "<tr>";
    VEH_COLS.forEach(([k, , fmt]) => {
      const num = fmt === "money" || fmt === "int";
      const cls = fmt === "vrm" ? "vrm" : num ? "num" + (v[k] < 0 ? " neg" : "") : "";
      h += "<td" + (cls ? ' class="' + cls + '"' : "") + ">" + cell(v[k], fmt) + "</td>";
    });
    h += "</tr>";
  });
  body.innerHTML = h + "</tbody></table>";
  body.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.k;
    vehSort = { key: k, dir: vehSort.key === k ? -vehSort.dir : 1 };
    vehList.sort((a, b) => {
      const x = a[k], y = b[k];
      if (x == null || x === "") return 1;
      if (y == null || y === "") return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * vehSort.dir;
    });
    renderVehTable();
  }));
}
let vehBase = [], vehFilt = "";
function openVehicles(sec, pt, p) {
  document.getElementById("veh-title").textContent = sec.title + " - " + periodLabel(pt, p);
  vehFilt = "Channel: " + (channel === "BCB" ? "BCB (Z001)" : channel) + ", Sales Type: " + bucket;
  vehBase = VEH.filter(v => (pt === "M" ? v._m : v._w) === p
    && (channel === "All" || v.CHANNEL === channel)
    && (bucket === "All" || v.SALES_TYPE === bucket)
    && sec.has(v));
  // Supplier drop-down: only the suppliers that have cars in this bar, with their counts
  const counts = {};
  vehBase.forEach(v => { const s = v.SUPPLIER || "(none)"; counts[s] = (counts[s] || 0) + 1; });
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  const sel = document.getElementById("veh-supplier");
  sel.innerHTML = '<option value="">All Suppliers (' + vehBase.length.toLocaleString("en-GB") + ")</option>" +
    Object.keys(counts).sort().map(s => '<option value="' + esc(s) + '">' + esc(s) + " (" + counts[s] + ")</option>").join("");
  applyVehSupplier();
  document.getElementById("veh-modal").classList.add("open");
  document.getElementById("veh-backdrop").classList.add("open");
}
function applyVehSupplier() {
  const sup = document.getElementById("veh-supplier").value;
  vehList = vehBase.filter(v => !sup || (v.SUPPLIER || "(none)") === sup);
  vehSort = { key: null, dir: 1 };
  document.getElementById("veh-sub").textContent = vehFilt + (sup ? ", Supplier: " + sup : "") + ". " +
    vehList.length.toLocaleString("en-GB") + " deliveries. Click a heading to sort.";
  renderVehTable();
}
document.getElementById("veh-supplier").addEventListener("change", applyVehSupplier);
function closeVehicles() {
  document.getElementById("veh-modal").classList.remove("open");
  document.getElementById("veh-backdrop").classList.remove("open");
}
document.getElementById("veh-close").addEventListener("click", closeVehicles);
document.getElementById("veh-backdrop").addEventListener("click", closeVehicles);
document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeVehicles(); });

// Close button at the bottom of Read This First: folds the box up and scrolls back to its heading
document.querySelectorAll(".box-close-btn").forEach(b => b.addEventListener("click", () => {
  const box = b.closest("details");
  box.open = false;
  box.scrollIntoView({ block: "nearest" });
}));

document.getElementById("main").addEventListener("click", ev => {
  const mv = ev.target.closest(".mv-btn");
  if (mv) { moveSection(mv.dataset.title, Number(mv.dataset.move)); return; }
  if (!ev.target.closest(".avg-toggle")) return;
  showAvg = !showAvg;
  try { localStorage.setItem(AVG_SHOW_KEY, showAvg ? "1" : "0"); } catch (e) { /* not kept, still works */ }
  build();
});

// Channel and Sales Type pills: one selected in each panel
document.querySelectorAll(".be-side [data-ch], .be-side [data-bk]").forEach(b => b.addEventListener("click", () => {
  const isCh = b.dataset.ch != null;
  document.querySelectorAll(".be-side [data-ch], .be-side [data-bk]").forEach(x => {
    if ((x.dataset.ch != null) === isCh) x.classList.toggle("selected", x === b);
  });
  if (isCh) channel = b.dataset.ch; else bucket = b.dataset.bk;
  build();
}));
let rt;
window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(drawAll, 120); });

// ---------- Data ----------
async function loadData(force) {
  const status = document.getElementById("data-status"), btn = document.getElementById("data-refresh");
  const loading = document.getElementById("be-loading"), err = document.getElementById("be-error");
  btn.disabled = true;
  status.innerHTML = '<i class="fas fa-sync-alt spin me-1"></i>Reading from Snowflake...';
  err.classList.add("d-none");
  try {
    const resp = await fetch(BASE + "/ad-hoc-analysis/buyer-estimates/data" + (force ? "?force=true" : ""));
    const data = await resp.json();
    if (data.status !== "ok") throw new Error(data.message || "The data could not be read");
    VEH = toVehicles(data.columns, data.rows);
    buildTables(VEH);
    const at = new Date(data.cached_at * 1000);
    status.innerHTML = '<i class="fas fa-snowflake me-1 text-info"></i>' + VEH.length.toLocaleString("en-GB") +
      " deliveries, read from Snowflake at " + String(at.getHours()).padStart(2, "0") + ":" + String(at.getMinutes()).padStart(2, "0") +
      " on " + at.toLocaleDateString("en-GB");
    build();
  } catch (e) {
    status.textContent = "Not loaded";
    err.textContent = "Could not load the deliveries: " + e.message;
    err.classList.remove("d-none");
  } finally {
    loading.style.display = "none";
    btn.disabled = false;
  }
}
document.getElementById("data-refresh").addEventListener("click", () => loadData(true));
loadData(false);
