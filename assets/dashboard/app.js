const D = JSON.parse(document.getElementById("payload").textContent);
const V = D.vocab, TH = D.thresholds;
const $ = (s) => document.querySelector(s);
const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c;
  if (x != null) n.textContent = x; return n; };

// row の列位置。data.json の並びと対応
const R = { UID:0, DS:1, INST:2, STUDY:3, FILE:4, TY:5, CT:6, USR:7, TS:8,
            DC:9, UC:10, FD:11, RSN:12, SRC:13, MSEV:14, AREA:15, NC:16,
            CONT:17, OUT:18, KEPT:19, DGRP:20, REF:21, PAT:22 };
const rows = D.rows;
function countCases(rs) {
  const p = new Set(), s = new Set(), im = new Set();
  for (const r of rs) {
    p.add(`${r[R.DS]}/${r[R.PAT]}`);
    s.add(`${r[R.DS]}/${r[R.STUDY]}`);
    im.add(`${r[R.DS]}/${r[R.STUDY]}/${r[R.FILE]}`);
  }
  return { patients: p.size, studies: s.size, images: im.size, annotations: rs.length };
}
const issuesByUid = new Map();
for (const [ci, uid, sev, st, msg, det] of D.issues) {
  if (!uid) continue;
  if (!issuesByUid.has(uid)) issuesByUid.set(uid, []);
  issuesByUid.get(uid).push({ cid: V.cid[ci], sev, st, msg, det });
}
const checkById = new Map(D.checks.map((c) => [c.id, c]));

/* ── 規模。annotation 数だけでは症例の規模が伝わらない ── */
const SC = D.meta.scale, POP = D.meta.population || {};
const scaleEl = document.getElementById("scale");
// 大きい数は「データセットに入っている全体」を出す。annotation を持つ分は内訳。
// 以前は 863（annotationを持つ画像）を主に出していて、
// 1083枚あるデータセットの規模が伝わらなかった。
const T = POP.total || {};
[["患者", T.patients ?? SC.patients], ["study", T.studies ?? SC.studies],
 ["画像", T.images ?? SC.images], ["annotation", SC.annotations]].forEach(([k, v]) => {
  const d = el("div");
  d.append(el("dt", null, k));
  d.append(el("dd", null, v.toLocaleString()));
  scaleEl.append(d);
});
if (POP.annotated) {
  document.getElementById("population").innerHTML =
    `画像 <b>${POP.total.images.toLocaleString()}</b> 枚の内訳: `
    + `annotation あり <b>${POP.annotated.images}</b>（検証対象）／ `
    + `正常例 <b>${POP.negative.images}</b>（No Findings。study 全体がアノテーションなし = 意図的な陰性症例）／ `
    + `未アノテーション <b>${POP.unannotated.images}</b>（アノテーション済み study の2枚目以降）。`
    + `以降の検証の数字は annotation あり ${POP.annotated.images} 枚が母数。`;
}
document.getElementById("fp").textContent =
  `fingerprint ${D.meta.fingerprint} · ${D.meta.sources.length} データセット`;

/* 採否ごとの症例数。レベル間で合計が一致しない（1症例が複数の採否を持つ）ので
   「その採否を含む症例数」と明記する。 */
const REASON_NOTE = {
  keep: "使う。内訳は下の理由別を参照",
  exclude: "完全一致の重複の古い方（自動判定）",
  pending: "目視待ち",
  uncertain: "見たが判断できなかったもの",
};

/* ── 採否の流れバー・凡例・作業量。所見（f-flow-ct）で絞り込める ──
   label==="" のときは既存どおり D.cases.* をそのまま使い、数値を据え置く。
   所見を選んだときだけ rows から countCases() で再集計する。 */
function renderFlow(label) {
  const rs = label ? rows.filter((r) => (V.ct[r[R.CT]] || "(ラベルなし)") === label) : rows;
  const byDecision = (name) => label
    ? countCases(rs.filter((r) => V.fd[r[R.FD]] === name))
    : D.cases.by_decision[name];
  const pending = label ? byDecision("pending") : D.cases.pending;

  document.getElementById("workload").innerHTML =
    `<b>目視の実作業量</b>　pending ${pending.annotations} annotation は `
    + `<b>${pending.images} 画像</b>（study ${pending.studies} / 患者 ${pending.patients}）に含まれる。`
    + `1画像に複数の annotation があるので、開く枚数は annotation 数より少ない。`;

  const legend = document.getElementById("flow-legend");
  legend.replaceChildren();
  V.fd.forEach((name) => {
    const c = byDecision(name);
    const s = el("span");
    const b = el("b", null, `${name} ${c.annotations.toLocaleString()}`);
    s.append(el("span", `dot ${name}`), b);
    const detail = c.annotations
      ? `　${c.images} 画像 / ${c.studies} study / ${c.patients} 患者　— ${REASON_NOTE[name]}`
      : `　— ${REASON_NOTE[name]}`;
    s.append(el("span", null, detail));
    legend.append(s);
  });

  const segCount = [0, 0, 0, 0];
  for (const r of rs) segCount[r[R.FD]]++;
  const flow = $("#flow");
  flow.replaceChildren();
  V.fd.forEach((name, i) => {
    if (!segCount[i]) return;
    const b = el("button", "flow-seg");
    b.dataset.s = name;
    b.style.flex = segCount[i];
    b.setAttribute("aria-pressed", "false");
    b.title = `${name} ${segCount[i]} 件 — クリックで一覧を絞る`;
    b.append(el("span", "v", segCount[i].toLocaleString()), el("span", "k", name));
    b.addEventListener("click", () => {
      const on = b.getAttribute("aria-pressed") === "true";
      $("#f-dec").value = on ? "" : name;
      apply();
    });
    flow.append(b);
  });
}
renderFlow("");

const flowSel = $("#f-flow-ct");
const flowCtCount = new Map();
for (const r of rows) {
  const label = V.ct[r[R.CT]] || "(ラベルなし)";
  flowCtCount.set(label, (flowCtCount.get(label) || 0) + 1);
}
[...flowCtCount].sort((a, b) => b[1] - a[1])
  .forEach(([label, n]) => flowSel.append(new Option(`${label} (${n})`, label)));
flowSel.addEventListener("change", () => renderFlow(flowSel.value));

/* ── 配色の切り替え ── */
$("#theme").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme;
  const dark = cur ? cur === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  drawAll();
});

/* decCount: #f-dec ドロップダウンの候補ラベルの件数表示に使う（グローバル・全所見合算）。
   流れバー本体は renderFlow() が描画する。 */
const decCount = [0, 0, 0, 0];
for (const r of rows) decCount[r[R.FD]]++;

/* ── 自動ルールの Precision。目視前は分母だけが埋まる ── */
const PR = D.precision || {};
if (PR.rows && PR.rows.length) {
  document.getElementById("sec-precision").hidden = false;
  const s = PR.summary;
  document.getElementById("prec-note").innerHTML = s.reviewed
    ? `目視の進捗 <b>${s.reviewed} / ${s.detected}</b>（延べ）。`
      + `exclude ${s.excluded} / keep ${s.kept} / uncertain ${s.uncertain}。`
      + `Precision が低い check は「そのルール自体が的を外している」ということ。`
    : `まだ判定が1件も無いので Precision は計算できない（検出 <b>${s.detected}</b> 件が分母）。`
      + ` App で目視して <span class="mono">review export</span> すると埋まる。`;
  const tb = document.querySelector("#tbl-precision tbody");
  for (const r of PR.rows) {
    const tr = el("tr");
    const td = el("td");
    td.append(el("code", "mono", r.check_id));
    tr.append(td);
    tr.append(el("td", "r num", r.detected.toLocaleString()));
    tr.append(el("td", "r num", r.reviewed ? String(r.reviewed) : "—"));
    tr.append(el("td", "r num", r.excluded ? String(r.excluded) : "—"));
    tr.append(el("td", "r num", r.kept ? String(r.kept) : "—"));
    tr.append(el("td", "r num", r.uncertain ? String(r.uncertain) : "—"));
    const p = el("td", "r num", r.precision == null ? "—" : r.precision.toFixed(2));
    if (r.precision != null) p.style.fontWeight = "600";
    tr.append(p);
    tb.append(tr);
  }
}

/* ── 画像単位の採否。annotation を持たない画像はここでしか扱えない ── */
const IMG = D.images || {};
if (IMG.summary) {
  document.getElementById("sec-images").hidden = false;
  const s = IMG.summary;
  const MEAN = {
    annotated: "annotation を持つ。採否は annotation 単位で判定済み",
    negative_case: "正常例（No Findings）。DICOM も実在する意図的な陰性症例。陰性サンプルとして残す",
    unannotated_view: "アノテーション済み study の2枚目以降。分類ラベルも無い。側面像なら除外が必要 → 目視",
    unannotated_orphan: "series 全体が未アノテーションで正常例ラベルも無い",
  };
  document.getElementById("img-note").innerHTML =
    `画像 <b>${s.total}</b> 枚の採否: `
    + Object.entries(s.by_decision).map(([k, v]) => `${k} <b>${v}</b>`).join(" / ")
    + (s.pending
        ? `。<b>${s.pending} 枚が目視待ち</b>（build-dataset は既定で停止する）`
        : "。目視待ちなし");

  const tbIc = document.querySelector("#tbl-imgclass tbody");
  Object.entries(s.by_class).sort((a, b) => b[1] - a[1]).forEach(([cls, n]) => {
    const tr = el("tr");
    const td = el("td");
    td.append(el("code", "mono", cls));
    tr.append(td);
    tr.append(el("td", "r num", n.toLocaleString()));
    const m = el("td", null, MEAN[cls] || "");
    m.style.fontSize = "12px";
    tr.append(m);
    tbIc.append(tr);
  });

  const tbIp = document.querySelector("#tbl-imgpend tbody");
  if (!IMG.pending.length) {
    const tr = el("tr");
    const td = el("td", null, "目視待ちの画像なし");
    td.colSpan = 3;
    td.style.color = "var(--ink-3)";
    tr.append(td);
    tbIp.append(tr);
  }
  for (const p of IMG.pending) {
    const tr = el("tr");
    tr.append(el("td", null, p.inst));
    const loc = el("td", "mono", `${p.patient} / ${p.study}`);
    loc.style.fontSize = "11px";
    tr.append(loc);
    tr.append(el("td", "mono", p.file));
    tbIp.append(tr);
  }
}

/* ── 対象病変の構成。「気胸だけなのか」に一目で答える ── */
const LB = D.labels;
const PNE = "Findings/010";
document.getElementById("target-note").innerHTML = LB.target_labels.length
  ? `現在の設定: <b>${LB.target_labels.join(" / ")}</b> のみを検証。`
    + `対象外の annotation はチェックを走らせず、採否マスタに `
    + `<span class="mono">reason = out_of_scope</span> として1行残す。`
  : `現在の設定: <b>全病変を検証</b>（<span class="mono">target_labels</span> が空）。`
    + `気胸だけに絞るなら <span class="mono">--set 'validation.target_labels=["Findings/010"]'</span>。`;

const tbLbl = document.querySelector("#tbl-labels tbody");
Object.entries(LB.by_dataset).sort((a, b) => b[1].total - a[1].total)
  .forEach(([ds, e]) => {
    const pne = e.labels[PNE] ? e.labels[PNE].n : 0;
    const tr = el("tr");
    tr.append(el("td", "mono", ds));
    tr.append(el("td", "r num", e.total.toLocaleString()));
    tr.append(el("td", "r num", pne.toLocaleString()));
    const sh = el("td");
    const wrap = el("div", "share");
    const track = el("div", "share-track");
    const fill = el("div", "share-fill");
    fill.style.width = `${(100 * pne / e.total).toFixed(1)}%`;
    track.append(fill);
    wrap.append(track, el("span", "share-num", `${(100 * pne / e.total).toFixed(0)}%`));
    sh.append(wrap);
    tr.append(sh);
    const others = el("td");
    const list = el("div", "lbl-list");
    Object.entries(e.labels).filter(([k]) => k !== PNE).slice(0, 4)
      .forEach(([, v]) => list.append(el("span", null, `${v.text} ${v.n}`)));
    const rest = Object.keys(e.labels).filter((k) => k !== PNE).length - 4;
    if (rest > 0) list.append(el("span", null, `他${rest}種`));
    if (!list.childElementCount) list.append(el("span", null, "—"));
    others.append(list);
    tr.append(others);
    tbLbl.append(tr);
  });

/* ── 症例単位の状態。「何症例が合格か」 ── */
const CS = D.cases.status, CL = D.cases.status_labels, CO = D.cases.status_order;
const tbCase = document.querySelector("#tbl-cases tbody");
[["患者", "patients"], ["study", "studies"], ["画像", "images"]].forEach(([ja, key]) => {
  const c = CS[key];
  const tr = el("tr");
  tr.append(el("td", null, ja));
  CO.forEach((s) => {
    const cls = s === "passed" ? "r pass" : s === "needs_review" ? "r rev" : "r";
    tr.append(el("td", cls + " num", c[s].toLocaleString()));
  });
  tr.append(el("td", "r num", c.total.toLocaleString()));
  tbCase.append(tr);
});

/* ── チェック台帳（M/D/S のアコーディオン） ── */
const KIND_JA = { automatic: "自動処理", review: "FiftyOne目視",
                  record: "記録のみ", cannot_determine: "判定不能" };
const SEV_ORDER = ["error", "warning", "info"];
const FAMILIES = [
  { cat: "machine", mark: "M", name: "M系",
    desc: "機械的な整合性。ファイルが読めるか、マスクの形式が規定通りか、"
        + "JSONの宣言値と実データが合っているか。自動採否はしない。",
    note: "M01–M05 と M08 が全て0件なのは正常。path_mask は 1665/1665 が "
        + "mode=L・uint8・値{0,255}・DICOMとサイズ一致で規定を完全に満たしている。"
        + "将来のエクスポートで壊れたときに気付くための回帰検知として稼働中。"
        + "M07 はモジュール全体を「記録のみ」にしつつ、座標が明確に壊れている "
        + "M07_BBOX_DEGENERATE と M07_BBOX_OUT_OF_IMAGE の11件だけを目視に上げている。" },
  { cat: "duplicate", mark: "D", name: "D系",
    desc: "重複アノテーション。同一画像内の別 geometry_uid を「形（幾何）」と"
        + "「ラベル」の2軸で分類する。D01だけ自動採否できる。",
    note: "ペアは1組につき2つの Issue を出す（成果物の粒度を annotation に揃えるため）。"
        + "そのため画像枚数は annotation 数の約半分になる。"
        + "D01 の keep は「D01では除外されない」だけで最終keepではない — "
        + "残った側が体外領域にも引っかかっていれば pending になる。" },
  { cat: "suspicious", mark: "S", name: "S系",
    desc: "医学的・形状的に疑わしいもの。面積が小さすぎる / 修正前後が食い違う / "
        + "体外にはみ出している。S01・S02 は欠番（旧S01は D01–D04 へ分割、S02は削除）。",
    note: "S系はどれも自動採否しない。機械で「怪しい」と分かっても、それが誤入力なのか"
        + "医学的に妥当なのかは画像を見ないと決まらない。"
        + "S03 の閾値は目視の結果で確定させる前提の暫定値。" },
];

function checkRow(c) {
  const tr = el("tr");
  tr.dataset.kind = c.kind;
  if (!c.n) tr.classList.add("zero");
  tr.style.cursor = "pointer";
  tr.title = `${c.id} で一覧を絞る`;
  const idTd = el("td", "kind");
  idTd.append(el("code", "mono", c.id));
  tr.append(idTd);
  tr.append(el("td", null, c.title || ""));
  const kTd = el("td");
  kTd.append(el("span", `chip k-${c.kind}`, KIND_JA[c.kind]));
  tr.append(kTd);
  tr.append(el("td", "r num", c.n.toLocaleString()));
  tr.append(el("td", "r num", c.pend ? c.pend.toLocaleString() : "—"));
  tr.append(el("td", "r num", c.cases && c.cases.images ? c.cases.images.toLocaleString() : "—"));
  const sTd = el("td");
  for (const s of SEV_ORDER) if (c.sev[s]) {
    const sp = el("span", `sev ${s[0]}`, `${s[0].toUpperCase()}${c.sev[s]}`);
    sp.style.marginRight = "8px";
    sTd.append(sp);
  }
  tr.append(sTd);
  tr.addEventListener("click", () => {
    selCheck.value = c.id;
    selDec.value = "";
    apply();
    document.querySelector("#tbl-ann").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  return tr;
}

const famHost = document.getElementById("families");
for (const fam of FAMILIES) {
  const list = D.checks.filter((c) => c.cat === fam.cat)
    .sort((a, b) => (b.n - a.n) || a.id.localeCompare(b.id));
  // 家族単位の集計はサーバ側（report/gui.py）で集合として数えた値を使う。
  // check ごとの画像枚数を足すと同じ画像を二重に数えてしまう。
  const fs = D.families[fam.cat] || { checks: 0, issues: 0, pending: 0, images: 0 };

  const box = el("div", "fam");
  box.dataset.cat = fam.cat;
  const bodyId = `fam-${fam.cat}`;
  const head = el("button", "fam-head");
  head.type = "button";
  head.setAttribute("aria-expanded", "true");
  head.setAttribute("aria-controls", bodyId);
  head.append(el("span", "fam-mark", "▾"));
  head.append(el("span", "fam-name", `${fam.name} (${fs.checks})`));
  head.append(el("span", "fam-desc", fam.desc));
  const stats = el("span", "fam-stats");
  [["check", fs.checks], ["issue", fs.issues], ["pending", fs.pending], ["画像", fs.images]]
    .forEach(([label, value]) => {
      const s = el("span");
      s.append(el("b", null, value.toLocaleString()));
      s.append(el("i", null, label));
      stats.append(s);
    });
  head.append(stats);
  box.append(head);

  const body = el("div", "fam-body");
  body.id = bodyId;
  const scroll = el("div", "tbl-scroll");
  const table = el("table");
  const thead = el("thead");
  const hrow = el("tr");
  [["check_id", ""], ["何を確認するか", ""], ["扱い", ""],
   ["Issue", "r"], ["pending", "r"], ["画像", "r"], ["severity", ""]]
    .forEach(([label, cls]) => hrow.append(el("th", cls, label)));
  thead.append(hrow);
  table.append(thead);
  const tbody = el("tbody");
  for (const c of list) tbody.append(checkRow(c));
  table.append(tbody);
  scroll.append(table);
  body.append(scroll);
  body.append(el("p", "fam-note", fam.note));
  box.append(body);

  head.addEventListener("click", () => {
    const open = head.getAttribute("aria-expanded") === "true";
    head.setAttribute("aria-expanded", String(!open));
    head.querySelector(".fam-mark").textContent = open ? "▸" : "▾";
    body.hidden = open;
  });
  famHost.append(box);
}

/* ── フィルタ ── */
const selDec = $("#f-dec"), selCheck = $("#f-check"), selInst = $("#f-inst"), selDs = $("#f-ds");
V.fd.forEach((n, i) => { if (decCount[i]) selDec.append(new Option(`${n} (${decCount[i]})`, n)); });
[...D.checks].filter((c) => c.n).sort((a, b) => b.n - a.n)
  .forEach((c) => selCheck.append(new Option(`${c.id} (${c.n})`, c.id)));
const instCount = new Map();
for (const r of rows) instCount.set(V.inst[r[R.INST]], (instCount.get(V.inst[r[R.INST]]) || 0) + 1);
[...instCount].sort((a, b) => b[1] - a[1])
  .forEach(([n, k]) => selInst.append(new Option(`${n} (${k})`, n)));
V.ds.forEach((n) => selDs.append(new Option(n, n)));

let shown = 0, filtered = [];
const PAGE = 120;
const tbAnn = $("#tbl-ann tbody");

function matches(r) {
  if (selDec.value && V.fd[r[R.FD]] !== selDec.value) return false;
  if (selInst.value && V.inst[r[R.INST]] !== selInst.value) return false;
  if (selDs.value && V.ds[r[R.DS]] !== selDs.value) return false;
  if (selCheck.value) {
    const has = r[R.DC].some((i) => V.cid[i] === selCheck.value)
             || r[R.UC].some((i) => V.cid[i] === selCheck.value);
    if (!has) return false;
  }
  const q = $("#f-q").value.trim().toLowerCase();
  if (q && !(r[R.UID].toLowerCase().includes(q) || r[R.STUDY].toLowerCase().includes(q)
          || r[R.FILE].toLowerCase().includes(q))) return false;
  return true;
}

function annRow(r) {
  const tr = el("tr", "ann");
  const uidTd = el("td");
  uidTd.append(el("code", "mono", r[R.UID].slice(0, 8)));
  tr.append(uidTd);
  const loc = el("td");
  loc.append(el("div", null, V.inst[r[R.INST]]));
  const st = el("div", "mono", `${r[R.PAT]} / ${r[R.STUDY]}`);
  st.style.cssText = "font-size:11px;color:var(--ink-3)";
  loc.append(st);
  tr.append(loc);
  tr.append(el("td", null, V.ty[r[R.TY]]));
  tr.append(el("td", null, V.ct[r[R.CT]] || "—"));
  tr.append(el("td", "r mono", r[R.AREA] == null ? "—" : r[R.AREA].toLocaleString()));
  tr.append(el("td", "r mono", r[R.CONT] == null ? "—" : r[R.CONT].toFixed(3)));
  const ck = el("td");
  const names = [...r[R.DC].map((i) => V.cid[i]), ...r[R.UC].map((i) => V.cid[i])];
  if (!names.length) {
    const dash = el("span", null, "—");
    dash.style.color = "var(--ink-3)";
    ck.append(dash);
  }
  for (const n of names) {
    const c = checkById.get(n);
    const sp = el("span", `chip k-${c ? c.kind : "record"}`, n.replace(/^([MDS]\d\d)_/, "$1 "));
    sp.style.cssText = "margin:0 5px 3px 0;font-size:10.5px";
    ck.append(sp);
  }
  tr.append(ck);
  const dTd = el("td");
  dTd.append(el("span", `dot ${V.fd[r[R.FD]]}`));
  const dName = el("span", null, V.fd[r[R.FD]]);
  dName.style.marginLeft = "6px";
  dTd.append(dName);
  dTd.style.whiteSpace = "nowrap";
  tr.append(dTd);
  const rTd = el("td", "mono", V.rsn[r[R.RSN]]);
  rTd.style.fontSize = "11px";
  tr.append(rTd);

  let open = null;
  tr.addEventListener("click", () => {
    if (open) { open.remove(); open = null; return; }
    open = el("tr", "detail");
    const td = el("td"); td.colSpan = 9;
    const box = el("div", "detail-in");
    const dl = el("dl");
    const put = (k, v) => { dl.append(el("dt", null, k)); dl.append(el("dd", "mono", v)); };
    put("geometry_uid", r[R.UID]);
    put("dataset", V.ds[r[R.DS]]);
    put("patient_id", r[R.PAT]);
    put("file_id", r[R.FILE]);
    put("annotator", V.usr[r[R.USR]] || "—");
    put("timestamp", r[R.TS] || "—");
    put("面積", r[R.AREA] == null ? "— (spacingなし)" : `${r[R.AREA]} mm²`);
    put("連結成分", r[R.NC] == null ? "—" : String(r[R.NC]));
    put("包含率 (margin 10mm)", r[R.CONT] == null ? `— (${r[R.REF] || "判定不能"})`
        : `${r[R.CONT]}　はみ出し ${r[R.OUT]} mm²`);
    if (r[R.KEPT]) put("残した方", r[R.KEPT]);
    if (r[R.DGRP]) put("重複グループ", r[R.DGRP]);
    put("unverified_checks", r[R.UC].map((i) => V.cid[i]).join(" | ") || "none");
    box.append(dl);
    const list = issuesByUid.get(r[R.UID]) || [];
    if (list.length) {
      const h = el("div");
      const hdr = el("span", "eyebrow", `この annotation に出た Issue ${list.length}件`);
      hdr.style.cssText = "display:block;margin-bottom:7px";
      h.append(hdr);
      for (const it of list) {
        const d = el("div", `iss ${it.sev}`);
        const hh = el("div", "iss-h");
        hh.append(el("code", "mono", it.cid));
        hh.append(el("span", `sev ${it.sev}`,
          { e: "ERROR", w: "WARNING", i: "INFO" }[it.sev] || it.sev));
        if (it.st === "c") hh.append(el("span", "chip k-cannot_determine", "判定不能"));
        d.append(hh);
        d.append(el("p", null, it.msg));
        if (it.det && it.det !== "{}") d.append(el("pre", null, it.det));
        h.append(d);
      }
      box.append(h);
    }
    td.append(box); open.append(td);
    tr.after(open);
  });
  return tr;
}

function apply() {
  filtered = rows.filter(matches);
  tbAnn.replaceChildren();
  shown = 0;
  push();
  const parts = [];
  if (selDec.value) parts.push(`採否 ${selDec.value}`);
  if (selCheck.value) parts.push(selCheck.value);
  if (selInst.value) parts.push(selInst.value);
  if (selDs.value) parts.push(selDs.value);
  const act = $("#f-active");
  act.replaceChildren();
  if (parts.length) {
    const s = el("span", "active-filter");
    s.append(el("span", null, parts.join(" · ")));
    const b = el("button", null, "×");
    b.title = "絞り込みを解除";
    b.addEventListener("click", () => {
      selDec.value = selCheck.value = selInst.value = selDs.value = "";
      $("#f-q").value = ""; apply();
    });
    s.append(b); act.append(s);
  }
  document.querySelectorAll(".flow-seg").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.s === selDec.value)));
}
function push() {
  const next = filtered.slice(shown, shown + PAGE);
  for (const r of next) tbAnn.append(annRow(r));
  shown += next.length;
  $("#more").hidden = shown >= filtered.length;
  $("#more").textContent = `さらに表示（残り ${(filtered.length - shown).toLocaleString()} 件）`;
  const imgs = new Set(filtered.map((r) => `${r[R.DS]}/${r[R.STUDY]}/${r[R.FILE]}`)).size;
  const pats = new Set(filtered.map((r) => `${r[R.DS]}/${r[R.PAT]}`)).size;
  $("#count").innerHTML = `<b>${filtered.length.toLocaleString()}</b> annotation が該当`
    + `（<b>${imgs.toLocaleString()}</b> 画像 / <b>${pats.toLocaleString()}</b> 患者）　`
    + `／ 全 ${rows.length.toLocaleString()} annotation　`
    + `<span style="color:var(--ink-3)">${shown.toLocaleString()} 件を表示中</span>`;
}
$("#more").addEventListener("click", push);
[selDec, selCheck, selInst, selDs].forEach((s) => s.addEventListener("change", apply));
$("#f-q").addEventListener("input", apply);

// 開いた時点で「対応が必要な集合」を見せる
selDec.value = "pending";
apply();
