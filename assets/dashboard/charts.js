/* ── 図。外部ライブラリは使わず手描きSVG。色はトークン参照でテーマに追従する ── */
const NS = "http://www.w3.org/2000/svg";
const mk = (t, a = {}) => { const n = document.createElementNS(NS, t);
  for (const k in a) n.setAttribute(k, a[k]); return n; };
const txt = (x, y, s, cls = "ax", extra = {}) => {
  const n = mk("text", { x, y, class: cls, ...extra }); n.textContent = s; return n; };

let tip = document.getElementById("tip");
if (!tip) {
  tip = document.createElement("div");
  tip.id = "tip";
  tip.style.cssText = "position:fixed;z-index:50;pointer-events:none;display:none;"
    + "background:var(--ink);color:var(--ground);padding:6px 9px;font:400 11.5px var(--f-mono);"
    + "max-width:320px;line-height:1.5;box-shadow:var(--shadow)";
  document.body.append(tip);
}
function hoverable(node, html) {
  node.addEventListener("pointerenter", (e) => {
    tip.innerHTML = html; tip.style.display = "block";
    tip.style.left = Math.min(e.clientX + 12, innerWidth - 340) + "px";
    tip.style.top = e.clientY + 14 + "px";
  });
  node.addEventListener("pointermove", (e) => {
    tip.style.left = Math.min(e.clientX + 12, innerWidth - 340) + "px";
    tip.style.top = e.clientY + 14 + "px";
  });
  node.addEventListener("pointerleave", () => { tip.style.display = "none"; });
}

/* ── 1. pending の内訳（annotation と画像を同じ数量軸で並べる） ──
   annotation 数だけでは目視で開く枚数が分からない。1画像に複数 annotation が
   あるので、D04 は 69 annotation でも 32 画像しかない。同じ「件数」という単位
   なので1本の軸に載せてよい（別スケールの二軸にはしない）。 */
function chartPending(host) {
  const data = D.checks.filter((c) => c.pend > 0)
    .sort((a, b) => b.pend - a.pend);
  // padR は「121 / 105」という値ラベル（mono 10.5px で約57px）が入る幅を確保する
  const W = 760, rowH = 32, padL = 250, padR = 86, padT = 8;
  const H = padT + data.length * rowH + 44;
  const max = Math.max(...data.map((c) => c.pend));
  const x = (v) => padL + (v / max) * (W - padL - padR);
  const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "check_id 別の pending annotation 数と画像枚数" });

  for (const t of [0, Math.round(max / 2), max]) {
    svg.append(mk("line", { x1: x(t), y1: padT, x2: x(t), y2: padT + data.length * rowH,
      class: "grid" }));
    svg.append(txt(x(t), H - 26, String(t), "ax", { "text-anchor": "middle" }));
  }
  data.forEach((c, i) => {
    const y = padT + i * rowH, img = c.cases ? c.cases.images : 0;
    const g = mk("g");
    g.append(txt(padL - 10, y + rowH / 2 + 3.5, c.id, "ax-strong", { "text-anchor": "end" }));
    // 上段=annotation（実線）／下段=画像（同じ色相を薄く）。2px の隙間で分ける
    const wa = Math.max(x(c.pend) - padL, 2), wi = Math.max(x(img) - padL, 2);
    g.append(mk("rect", { x: padL, y: y + 5, width: wa, height: 9, rx: 2,
      fill: "var(--pending)" }));
    g.append(mk("rect", { x: padL, y: y + 16, width: wi, height: 9, rx: 2,
      fill: "var(--pending)", opacity: ".42" }));
    g.append(txt(padL + Math.max(wa, wi) + 8, y + rowH / 2 + 3.5,
      `${c.pend} / ${img}`, "ax-strong"));
    hoverable(g, `<b>${c.id}</b><br>${c.title}<br>`
      + `pending ${c.pend} annotation<br>開く画像 ${img} 枚`
      + `<br>study ${c.cases ? c.cases.studies : 0} / 患者 ${c.cases ? c.cases.patients : 0}`
      + `<br>Issue 合計 ${c.n} 件`);
    g.style.cursor = "pointer";
    g.addEventListener("click", () => {
      document.querySelector("#f-check").value = c.id;
      document.querySelector("#f-dec").value = "pending";
      apply();
      document.querySelector("#tbl-ann").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    svg.append(g);
  });
  // 凡例。色だけに頼らず必ずラベルを添える
  const ly = H - 8;
  svg.append(mk("rect", { x: padL, y: ly - 8, width: 16, height: 9, rx: 2,
    fill: "var(--pending)" }));
  svg.append(txt(padL + 22, ly, "annotation 数", "ax"));
  svg.append(mk("rect", { x: padL + 118, y: ly - 8, width: 16, height: 9, rx: 2,
    fill: "var(--pending)", opacity: ".42" }));
  svg.append(txt(padL + 140, ly, "開く画像の枚数", "ax"));
  host.replaceChildren(svg);
}

/* ── 2. 面積分布（log軸ヒストグラム＋クラス族の下限） ── */
function chartArea(host) {
  const areas = rows.map((r) => r[R.AREA]).filter((v) => v != null && v > 0);
  const logs = areas.map(Math.log10);
  const lo = Math.floor(Math.min(...logs) * 4) / 4, hi = Math.ceil(Math.max(...logs) * 4) / 4;
  const step = 0.25, nb = Math.round((hi - lo) / step);
  const bins = new Array(nb).fill(0);
  for (const v of logs) bins[Math.min(Math.floor((v - lo) / step), nb - 1)]++;
  const max = Math.max(...bins);

  const W = 560, H = 268, padL = 38, padR = 12, padT = 26, padB = 46;
  const x = (lv) => padL + ((lv - lo) / (hi - lo)) * (W - padL - padR);
  const y = (n) => padT + (1 - n / max) * (H - padT - padB);
  const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "マスク面積の対数分布" });

  for (const n of [0, Math.round(max / 2), max]) {
    svg.append(mk("line", { x1: padL, y1: y(n), x2: W - padR, y2: y(n), class: "grid" }));
    svg.append(txt(padL - 7, y(n) + 3.5, String(n), "ax", { "text-anchor": "end" }));
  }
  const bw = (W - padL - padR) / nb;
  bins.forEach((n, i) => {
    if (!n) return;
    const lv = lo + i * step;
    const below = lv + step <= Math.log10(TH.tiny);
    const rect = mk("rect", { x: x(lv) + 1, y: y(n), width: Math.max(bw - 2, 1),
      height: H - padB - y(n), rx: 2, class: below ? "bar lo" : "bar" });
    hoverable(rect, `${(10 ** lv).toPrecision(3)} – ${(10 ** (lv + step)).toPrecision(3)} mm²`
      + `<br><b>${n} 件</b>`);
    svg.append(rect);
  });
  for (const e of [-2, -1, 0, 1, 2, 3, 4]) {
    if (e < lo || e > hi) continue;
    svg.append(txt(x(e), H - padB + 15, e === 0 ? "1" : `10${sup(e)}`, "ax",
      { "text-anchor": "middle" }));
  }
  svg.append(txt((padL + W - padR) / 2, H - 8, "マスク面積 mm²（対数）", "ax",
    { "text-anchor": "middle" }));

  const marks = [["focal 20", TH.byclass.focal], ["localized 50", TH.byclass.localized],
                 ["regional 150", TH.byclass.regional]];
  // log軸では 20 と 50 が約31pxしか離れないので縦にずらす。
  // さらに、右側は度数が最大のバー（271件）があるので、ラベルは左向きに置く。
  // 閾値より左＝低面積側は度数が少なく空いている。
  marks.forEach(([label, v], i) => {
    const lv = Math.log10(v);
    svg.append(mk("line", { x1: x(lv), y1: padT, x2: x(lv), y2: H - padB, class: "rule-th" }));
    const below = areas.filter((a) => a < v).length;
    svg.append(txt(x(lv) - 5, padT + 11 + i * 13, `未満 ${below}件 ｜ ${label}`, "ax-strong",
      { "text-anchor": "end" }));
  });
  host.replaceChildren(svg);
}
const sup = (n) => String(n).replace("-", "⁻").replace(/\d/g, (d) => "⁰¹²³⁴⁵⁶⁷⁸⁹"[d]);

/* ── 3. 体外領域の裾（点の帯。x=包含率、状態色は severity） ── */
function chartOutside(host) {
  const pts = rows.filter((r) => r[R.CONT] != null && r[R.CONT] < 1)
    .map((r) => ({ c: r[R.CONT], out: r[R.OUT] || 0, uid: r[R.UID],
                   inst: V.inst[r[R.INST]], usr: V.usr[r[R.USR]], area: r[R.AREA],
                   file: r[R.FILE] }));
  const sevOf = (p) => p.c < TH.ob_err ? "e"
    : (p.c < TH.ob_warn && p.out > TH.ob_mm2) ? "w" : "i";
  const W = 560, H = 260, padL = 34, padR = 16, padT = 34, padB = 52;
  const lo = 0.4, hi = 1.0;
  const x = (v) => padL + ((v - lo) / (hi - lo)) * (W - padL - padR);
  const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": "体外領域の包含率の分布" });

  // 閾値の帯
  svg.append(mk("rect", { x: x(lo), y: padT, width: x(TH.ob_err) - x(lo), height: H - padT - padB,
    fill: "var(--sev-error)", opacity: ".10" }));
  svg.append(mk("rect", { x: x(TH.ob_err), y: padT, width: x(TH.ob_warn) - x(TH.ob_err),
    height: H - padT - padB, fill: "var(--sev-warning)", opacity: ".10" }));
  for (const v of [TH.ob_err, TH.ob_warn]) {
    svg.append(mk("line", { x1: x(v), y1: padT - 6, x2: x(v), y2: H - padB, class: "rule-th" }));
  }
  svg.append(txt(x(TH.ob_err) + 4, padT - 11, `error < ${TH.ob_err}`, "ax-strong",
    { fill: "var(--sev-error)" }));
  svg.append(txt(x(TH.ob_warn) - 4, padT - 11,
    `warning < ${TH.ob_warn} かつ >${TH.ob_mm2}mm²`, "ax-strong",
    { "text-anchor": "end", fill: "var(--sev-warning)" }));

  // x軸
  for (const v of [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]) {
    svg.append(mk("line", { x1: x(v), y1: H - padB, x2: x(v), y2: H - padB + 4, class: "grid" }));
    svg.append(txt(x(v), H - padB + 16, v.toFixed(1), "ax", { "text-anchor": "middle" }));
  }
  svg.append(txt((padL + W - padR) / 2, H - 20,
    `包含率（マージン ${TH.margin}mm 以内にある画素の割合）`, "ax", { "text-anchor": "middle" }));

  // 同じ x に重なる点を上へ積む
  const cols = new Map();
  const r = 4.2;
  const laid = pts.sort((a, b) => a.c - b.c).map((p) => {
    const key = Math.round(x(p.c) / (r * 2));
    const k = cols.get(key) || 0; cols.set(key, k + 1);
    return { p, cx: x(p.c), cy: H - padB - 8 - k * (r * 2 + 1.5) };
  });
  for (const { p, cx, cy } of laid) {
    const s = sevOf(p);
    const c = mk("circle", { cx, cy, r,
      fill: `var(--sev-${{ e: "error", w: "warning", i: "info" }[s]})`,
      stroke: "var(--surface)", "stroke-width": 2 });
    hoverable(c, `<b>${p.uid.slice(0, 8)}</b> ${p.inst}<br>${p.file}<br>`
      + `包含率 ${p.c.toFixed(4)}／はみ出し ${p.out} mm²<br>面積 ${p.area} mm²<br>${p.usr}`);
    c.style.cursor = "pointer";
    c.addEventListener("click", () => {
      document.querySelector("#f-dec").value = "";
      document.querySelector("#f-check").value = "";
      document.querySelector("#f-q").value = p.uid.slice(0, 8);
      apply();
      document.querySelector("#tbl-ann").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    svg.append(c);
  }
  // 凡例（色だけに頼らないよう必ずラベルを添える）
  const leg = mk("g");
  [["error", 6], ["warning", 20 + 52], ["info", 20 + 52 + 74]].forEach(([name, dx], i) => {
    const gx = padL + dx, gy = H - 4;
    leg.append(mk("circle", { cx: gx, cy: gy - 3.5, r: 4,
      fill: `var(--sev-${name})`, stroke: "var(--surface)", "stroke-width": 2 }));
    const n = laid.filter(({ p }) => sevOf(p) === name[0]).length;
    leg.append(txt(gx + 8, gy, `${name} ${n}`, "ax"));
  });
  svg.append(leg);
  host.replaceChildren(svg);
}

function drawAll() {
  chartPending(document.getElementById("chart-pending"));
  chartArea(document.getElementById("chart-area"));
  chartOutside(document.getElementById("chart-outside"));
}
drawAll();
