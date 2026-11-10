"use strict";
/* miniDM webui — fala com a API local do server.py. Sem dependências.
   Servida em http://127.0.0.1:9742/ (WebView do APK e navegador). */

const $ = (s) => document.querySelector(s);

/* ------------------------------------------------------------------ tema */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("minidm-theme", t); } catch (_) { /* privado */ }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = t === "dark" ? "#0f1419" : "#f6f7f9";
}
try {
  applyTheme(localStorage.getItem("minidm-theme")
    || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
} catch (_) {
  applyTheme("dark");
}
$("#theme-btn").addEventListener("click", () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

/* -------------------------------------------------------------- formatar */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const nf = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });
function fmtBytes(n) {
  n = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${nf.format(n)} ${units[i]}`;
}
const fmtSpeed = (n) => (Number(n) > 0 ? `${fmtBytes(n)}/s` : "");

const STATE = {
  queued: "na fila",
  downloading: "baixando",
  paused: "pausado",
  done: "concluído",
  error: "erro",
};

/* ------------------------------------------------------------------- API */
async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
const post = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

let toastTimer = 0;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4500);
}

/* ------------------------------------------------------------ categorias */
let catBase = "";        // pasta base das categorias (/categories)
let catFolders = {};     // nome da categoria -> subpasta
let catDataDir = "";     // sem categoria: destino interno (/config)

/** Destino padrão da categoria atual (o servidor usa igual). */
function destFor(name) {
  if (!name) return catDataDir;
  const sub = catFolders[name] || name;
  if (!catBase) return sub;
  return catBase.replace(/[\\/]+$/, "") + "/" + sub;
}

function refreshDestPlaceholder() {
  const d = destFor($("#category").value);
  $("#dest").placeholder = d ? `Salva em: ${d}` : "Pasta padrão da categoria";
}

async function loadCategories() {
  try {
    const d = await api("/categories");
    catBase = d.base || "";
    catFolders = {};
    for (const c of d.categories || []) catFolders[c.name] = c.folder;
    const def = (d.categories || []).find((c) => c.default);
    $("#category").innerHTML = `<option value="">Automático</option>` +
      (d.categories || []).map((c) =>
        `<option value="${esc(c.name)}"${c === def ? " selected" : ""}>` +
        `${esc(c.name)}</option>`).join("");
    $("#category").addEventListener("change", refreshDestPlaceholder);
    refreshDestPlaceholder();
  } catch (_) { /* offline: fica só "Automático" */ }
  try {
    const c = await api("/config");
    catDataDir = c.data_dir || "";
    refreshDestPlaceholder();
  } catch (_) { /* sem /config novo: placeholder genérico */ }
}

/* ---------------------------------------------------------- render (diff) */
const cards = new Map();   // id -> <article>

function makeCard(t) {
  const el = document.createElement("article");
  el.className = "card";
  el.dataset.id = t.id;
  el.innerHTML = `
    <div class="card-head">
      <span class="name"></span><span class="badge"></span>
    </div>
    <div class="bar"><div class="fill"></div></div>
    <div class="meta">
      <span class="pct"></span><span class="size"></span>
      <span class="speed"></span><span class="seg"></span>
      <span class="note"></span>
    </div>
    <div class="chips"><span class="chip cat"></span><span class="chip q"></span><span class="chip path" hidden></span></div>
    <div class="err" hidden></div>
    <div class="acts">
      <button data-act="start" type="button" title="Iniciar">&#9654;</button>
      <button data-act="pause" type="button" title="Pausar">&#10074;&#10074;</button>
      <button data-act="remove" type="button" title="Remover da fila">&#10005;</button>
    </div>`;
  cards.set(t.id, el);
  return el;
}

function updateCard(el, t) {
  const st = t.state || "queued";
  el.dataset.state = st;
  el.querySelector(".name").textContent = t.filename || t.url || t.id;
  el.querySelector(".name").title = t.url || "";
  el.querySelector(".badge").textContent = STATE[st] || st;

  const pct = Math.round((Number(t.progress) || 0) * 100);
  el.querySelector(".fill").style.width = `${pct}%`;
  el.querySelector(".pct").textContent = t.live ? "ao vivo" : `${pct}%`;
  el.querySelector(".size").textContent = t.total > 0
    ? `${fmtBytes(t.received)} / ${fmtBytes(t.total)}`
    : (t.received > 0 ? fmtBytes(t.received) : "");
  el.querySelector(".speed").textContent =
    st === "downloading" ? fmtSpeed(t.speed) : "";
  el.querySelector(".seg").textContent =
    t.segments_total > 0 ? `${t.segments_done}/${t.segments_total} seg` : "";
  // fase do yt-dlp (ex.: "[youtube] Extraindo…") — aparece enquanto a
  // barra ainda não tem bytes; some ao concluir/pausar
  el.querySelector(".note").textContent =
    st === "downloading" ? (t.note || "") : "";

  const cat = el.querySelector(".chip.cat");
  cat.textContent = t.effective_category || "";
  cat.hidden = !t.effective_category;
  const q = el.querySelector(".chip.q");
  q.textContent = t.quality_label || "";
  q.hidden = !t.quality_label;

  // destino público (Android: Download/ do sistema) — encurta o
  // prefixo de armazenamento e deixa o caminho completo no tooltip
  const path = el.querySelector(".chip.path");
  const dest = t.published_path || "";
  path.textContent = dest.replace(/^\/storage\/emulated\/0\//, "");
  path.title = dest;
  path.hidden = !dest;

  const err = el.querySelector(".err");
  err.textContent = t.error || "";
  err.hidden = !t.error;

  // paleta de botões por estado
  el.querySelector('[data-act="start"]').hidden =
    st === "downloading" || st === "queued";
  el.querySelector('[data-act="pause"]').hidden =
    st !== "downloading" && st !== "queued";
}

function render(tasks) {
  const list = $("#list");
  const seen = new Set();
  for (const t of tasks) {
    seen.add(t.id);
    const el = cards.get(t.id) || makeCard(t);
    updateCard(el, t);
    list.appendChild(el);          // move p/ a ordem do servidor
  }
  for (const [id, el] of cards) {
    if (!seen.has(id)) { el.remove(); cards.delete(id); }
  }

  const by = (s) => tasks.filter((t) => t.state === s).length;
  const parts = [];
  if (by("downloading")) parts.push(`<b>${by("downloading")}</b> baixando`);
  if (by("queued")) parts.push(`<b>${by("queued")}</b> na fila`);
  if (by("paused")) parts.push(`<b>${by("paused")}</b> pausado${by("paused") > 1 ? "s" : ""}`);
  if (by("done")) parts.push(`<b>${by("done")}</b> ok`);
  if (by("error")) parts.push(`<b>${by("error")}</b> erro${by("error") > 1 ? "s" : ""}`);
  $("#counts").innerHTML = parts.length ? parts.join(" &middot; ") : "&nbsp;";
  $("#empty").hidden = tasks.length > 0;
}

function setConn(ok) {
  $("#conn").classList.toggle("off", !ok);
  $("#conn").title = ok ? "API local conectada" : "sem conexão com a API";
  if (!ok) $("#counts").innerHTML = "<b>sem conexão</b> com a API local";
}

/* -------------------------------------------------------------- polling */
let busy = false;
async function tick() {
  if (busy || document.hidden) return;
  busy = true;
  try {
    const d = await api("/status");
    setConn(true);
    render(d.tasks || []);
  } catch (_) {
    setConn(false);
  } finally {
    busy = false;
  }
}
setInterval(tick, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

/* ------------------------------------------------------------- ações */
/* escolher qualidade (diálogo pós-análise, estilo IDM) */
let pickResolve = null;
function pickQuality(res) {
  if (pickResolve) pickResolve(null);   // nunca dois diálogos abertos
  return new Promise((resolve) => {
    pickResolve = (val) => {
      pickResolve = null;
      $("#pick").hidden = true;
      resolve(val);
    };
    $("#pick-title").textContent = res.page_title || "Escolher qualidade";
    const box = $("#pick-list");
    box.textContent = "";
    for (const q of res.qualities || []) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "pick-opt";
      const det = [];
      if (q.resolution) det.push(q.resolution);
      if (q.estimated_size) det.push(fmtBytes(q.estimated_size));
      else if (q.bandwidth) det.push(`${nf.format(q.bandwidth / 1000)} kbps`);
      if (q.audio_only) det.push("só áudio");
      if (q.live) det.push("ao vivo");
      const lab = document.createElement("span");
      lab.className = "pick-label";
      lab.textContent = q.label || "original";
      b.appendChild(lab);
      if (det.length) {
        const d = document.createElement("span");
        d.className = "pick-det";
        d.textContent = det.join(" · ");
        b.appendChild(d);
      }
      b.addEventListener("click", () => pickResolve && pickResolve(q));
      box.appendChild(b);
    }
    $("#pick").hidden = false;
  });
}
$("#pick-close").addEventListener("click",
  () => pickResolve && pickResolve(null));
$("#pick").addEventListener("click", (ev) => {
  if (ev.target.id === "pick" && pickResolve) pickResolve(null);
});

$("#add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const url = $("#url").value.trim();
  if (!url) return;
  const payload = { url, category: $("#category").value };
  if ($("#analyze").checked) payload.analyze = true;
  // pasta escolhida pelo usuário (vazia = o destino padrão da categoria,
  // mostrado no placeholder)
  const dest = $("#dest").value.trim();
  if (dest) payload.dir = dest;
  const btn = ev.submitter;
  const btnText = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; if (payload.analyze) btn.textContent = "Analisando…"; }
  try {
    // com "Analisar mídia", analisa ANTES da tarefa: aí a caixa de
    // qualidades aparece (o usuário escolhe) e a escolha vai direto
    // no /add (quality_url/kind/…) — sem refazer a análise no servidor
    if (payload.analyze) {
      let res = null;
      try { res = await post("/media/analyze", { url }); }
      catch (_) { /* falhou aqui: o /add analisa de novo e escolhe/erro */ }
      const quals = (res && res.ok && !res.page) ? (res.qualities || []) : [];
      if (quals.length > 1) {
        if (btn) btn.textContent = btnText;   // diálogo assume o fluxo
        const q = await pickQuality(res);
        if (!q) return;                 // cancelou — o link fica na caixa
        applyQuality(payload, res, q);
      } else if (quals.length === 1) {
        applyQuality(payload, res, quals[0]);   // qualidade única: direto
      }
      // página (page=true) ou sem análise: segue com analyze pro /add
      // decidir (delega ao yt-dlp ou recusa com clareza)
    }
    await post("/add", payload);
    $("#url").value = "";
    $("#url").focus();
    $("#dest").value = "";
    tick();
  } catch (e) {
    toast(e.message);   // ex.: DRM recusado, URL inválida, sem qualidade
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = btnText; }
  }
});

/** Leva a escolha do diálogo para o payload do /add (sem re-analisar). */
function applyQuality(payload, res, q) {
  delete payload.analyze;
  payload.quality_url = q.url;
  payload.kind = q.kind || "";
  payload.quality_label = q.label || "";
  if (q.dash && q.dash.id) payload.rep_id = q.dash.id;
  if (q.audio) payload.audio = q.audio;
  if (res.page_title) payload.page_title = res.page_title;
  if (res.filename) payload.filename = res.filename;
}

$("#start-all").addEventListener("click", async () => {
  try { await post("/start", { all: true }); tick(); }
  catch (e) { toast(e.message); }
});

$("#list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest(".card").dataset.id;
  try {
    if (btn.dataset.act === "start") {
      await post("/start", { id });
    } else if (btn.dataset.act === "pause") {
      await post("/pause", { id });
    } else if (btn.dataset.act === "remove") {
      if (!confirm("Remover esta tarefa da fila?")) return;
      await post("/remove", { id });
    }
    tick();
  } catch (e) {
    toast(e.message);
  }
});

/* -------------------------------------------------------------- início */
loadCategories();
tick();
