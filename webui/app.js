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
    // guarda: recarregar p/ atualizar categorias após salvar config
    // não deve duplicar o listener de change
    if (!loadCategories._bound) {
      $("#category").addEventListener("change", refreshDestPlaceholder);
      loadCategories._bound = true;
    }
    refreshDestPlaceholder();
  } catch (_) { /* offline: fica só "Automático" */ }
  try {
    const c = await api("/config");
    catDataDir = c.data_dir || "";
    refreshDestPlaceholder();
  } catch (_) { /* sem /config novo: placeholder genérico */ }
}

/* ---------------------------------------------------------- render (diff) */
const cards = new Map();     // id -> <article>
const taskById = new Map();  // id -> tarefa do snapshot (menu/propriedades)
let lastOrder = [];          // ids na ordem da fila (1º = próxima a baixar)

/* busca + filtro por estado (histórico — as concluídas ficam na fila) */
let filterState = "all";
let filterQuery = "";

/** A tarefa passa no filtro atual (busca por nome/URL + chip de estado)? */
function matchesFilter(t) {
  const st = t.state || "queued";
  if (filterState === "active" &&
      !(st === "downloading" || st === "queued" || st === "paused")) return false;
  if (filterState === "done" && st !== "done") return false;
  if (filterState === "error" && st !== "error") return false;
  if (filterQuery) {
    const hay = `${t.filename || ""} ${t.url || ""} ${t.page_title || ""}`
      .toLowerCase();
    if (!hay.includes(filterQuery)) return false;
  }
  return true;
}

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
      <button data-act="menu" type="button" title="Mais ações (abrir, copiar, reordenar)">&#8943;</button>
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
  taskById.clear();
  for (const t of tasks) taskById.set(t.id, t);
  lastOrder = tasks.map((t) => t.id);   // ordem completa p/ subir/descer
  const seen = new Set();
  let shown = 0;
  for (const t of tasks) {
    seen.add(t.id);
    const el = cards.get(t.id) || makeCard(t);
    updateCard(el, t);
    el.hidden = !matchesFilter(t);   // busca/chip escondem sem destruir a card
    if (!el.hidden) shown += 1;
    // move p/ a ordem do servidor — menos durante o arrasto, senão o
    // poll de 1s "snapava" o card de volta para a posição antiga
    if (!dragId) list.appendChild(el);
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
  const filtering = filterState !== "all" || filterQuery;
  if (filtering) parts.push(`<b>${shown}</b> exibido${shown === 1 ? "" : "s"}`);
  $("#counts").innerHTML = parts.length ? parts.join(" &middot; ") : "&nbsp;";
  $("#empty").hidden = shown > 0;
  $("#empty").textContent = tasks.length === 0
    ? "Nada na fila ainda — cole um link acima."
    : "Nada bate com a busca/filtro — limpe ou troque a aba.";
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

/* --------------------------------------------------------- busca/filtros */
$("#q").addEventListener("input", () => {
  filterQuery = $("#q").value.trim().toLowerCase();
  tick();                       // re-render na hora (não espera o poll)
});
document.querySelectorAll(".tab").forEach((b) => {
  b.addEventListener("click", () => {
    filterState = b.dataset.filter;
    document.querySelectorAll(".tab").forEach((o) => {
      const on = o === b;
      o.classList.toggle("on", on);
      o.setAttribute("aria-selected", String(on));
    });
    tick();
  });
});

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
  if (btn.dataset.act === "menu") {
    //⋯ abre o menu de contexto ancorado no botão (estilo IDM)
    const r = btn.getBoundingClientRect();
    openCtx(id, r.right, r.bottom + 6);
    return;
  }
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

/* ------------------------------------------------- menu de contexto (⋯) */
/* botão direito no card ou⋯: abrir pasta/arquivo, copiar URL,
   propriedades, subir/descer na fila e remover (mesmas ações do
   menu de contexto da GUI desktop — gui.py). */
const ctx = { id: null };

function openCtx(id, x, y) {
  ctx.id = id;
  const menu = $("#ctx");
  // "Abrir arquivo" só faz sentido com o arquivo já concluído
  const t = taskById.get(id);
  menu.querySelector('[data-ctx="open-file"]').hidden =
    !t || t.state !== "done";
  menu.hidden = false;
  // ancora perto do clique/⋯ sem sair da tela
  menu.style.left =
    `${Math.max(6, Math.min(x, innerWidth - menu.offsetWidth - 6))}px`;
  menu.style.top =
    `${Math.max(6, Math.min(y, innerHeight - menu.offsetHeight - 6))}px`;
}

function closeCtx() { $("#ctx").hidden = true; }

document.addEventListener("click", (ev) => {
  if ($("#ctx").hidden) return;
  if (ev.target.closest("#ctx") ||
      ev.target.closest('[data-act="menu"]')) return;
  closeCtx();
});
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  closeCtx();
  $("#props").hidden = true;
  closeCfg();
});

$("#list").addEventListener("contextmenu", (ev) => {
  const card = ev.target.closest(".card");
  if (!card) return;
  ev.preventDefault();                     // menu nativo não abre
  openCtx(card.dataset.id, ev.clientX, ev.clientY);
});

/** Copia o URL da tarefa — clipboard API com fallback execCommand. */
async function copyUrl(id) {
  const t = taskById.get(id);
  const url = (t && t.url) || "";
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    toast("URL copiada");
    return;
  } catch (_) { /* WebView/origem sem permissão: fallback antigo */ }
  const ta = document.createElement("textarea");
  ta.value = url;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { /* sem suporte */ }
  ta.remove();
  toast(ok ? "URL copiada" : "não deu para copiar a URL");
}

/** Sobe/desce uma posição na fila (1º = próxima) e persiste. */
async function moveBy(id, delta) {
  const order = lastOrder.slice();
  const i = order.indexOf(id);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= order.length) return;   // já está na ponta
  [order[i], order[j]] = [order[j], order[i]];
  await post("/reorder", { ids: order });
  tick();
}

$("#ctx").addEventListener("click", async (ev) => {
  const b = ev.target.closest("button[data-ctx]");
  if (!b || !ctx.id) return;
  const id = ctx.id;
  const act = b.dataset.ctx;
  closeCtx();
  try {
    if (act === "open-folder") {
      await post("/open", { id, what: "folder" });
    } else if (act === "open-file") {
      await post("/open", { id, what: "file" });
    } else if (act === "copy") {
      await copyUrl(id);
    } else if (act === "props") {
      await showProps(id);
    } else if (act === "up") {
      await moveBy(id, -1);
    } else if (act === "down") {
      await moveBy(id, 1);
    } else if (act === "remove") {
      if (!confirm("Remover esta tarefa da fila?")) return;
      await post("/remove", { id });
      tick();
    }
  } catch (e) {
    toast(e.message);   // ex.: "pasta ainda não existe", sem permissão
  }
});

/* ------------------------------------------------ propriedades (GET /task) */
/* campos do registro completo (to_dict) — o snapshot do /status não
   tem dest_dir/referer/section/created_at. */
const PROP_ROWS = [
  ["filename", "Nome"], ["state", "Estado"], ["kind", "Tipo"],
  ["profile", "Perfil"], ["category", "Categoria"],
  ["quality_label", "Qualidade"], ["page_title", "Título da página"],
  ["origin_url", "Página de origem"], ["url", "URL"],
  ["referer", "Referer"], ["dest_dir", "Pasta"],
  ["published_path", "Destino publicado"], ["section", "Seção"],
  ["total_size", "Tamanho"], ["created_at", "Criada em"],
  ["sha256", "SHA-256"], ["error", "Erro"],
];

async function showProps(id) {
  let t;
  try {
    t = await api(`/task?id=${encodeURIComponent(id)}`);
  } catch (e) {
    toast(e.message);
    return;
  }
  const rows = [];
  for (const [k, label] of PROP_ROWS) {
    let v = t[k];
    if (v === undefined || v === null || v === "") continue;
    if (k === "state") v = STATE[v] || v;
    else if (k === "total_size") v = fmtBytes(v);
    else if (k === "created_at") {
      const d = new Date(Number(v) * 1000);
      v = Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString("pt-BR");
    } else v = String(v);
    rows.push(`<dt>${esc(label)}</dt><dd>${esc(v)}</dd>`);
  }
  $("#props-list").innerHTML = rows.join("");
  $("#props-title").textContent = t.filename || "Propriedades";
  $("#props").hidden = false;
}

$("#props-close").addEventListener("click", () => { $("#props").hidden = true; });
$("#props").addEventListener("click", (ev) => {
  if (ev.target.id === "props") $("#props").hidden = true;   // clique fora
});

/* --------------------------------------------- arrastar p/ reordenar (DnD) */
/* HTML5 drag & drop: soltar entre os cards reordena a fila inteira
   (ordem do DOM -> POST /reorder). `dragId` congela o render durante
   o arrasto para o poll não voltar o card para a posição antiga. */
let dragId = null;

$("#list").addEventListener("dragstart", (ev) => {
  const card = ev.target.closest(".card");
  if (!card) return;
  dragId = card.dataset.id;
  card.classList.add("dragging");
  $("#list").classList.add("dragging-active");
  ev.dataTransfer.effectAllowed = "move";
  ev.dataTransfer.setData("text/plain", dragId);   // Firefox exige setData
});

$("#list").addEventListener("dragover", (ev) => {
  if (!dragId) return;
  ev.preventDefault();                             // autoriza o drop
  ev.dataTransfer.dropEffect = "move";
  const card = ev.target.closest(".card");
  for (const el of document.querySelectorAll(".card.over")) {
    if (el !== card) el.classList.remove("over");
  }
  if (card && card.dataset.id !== dragId) card.classList.add("over");
});

$("#list").addEventListener("dragleave", (ev) => {
  const card = ev.target.closest(".card");
  if (card && !card.contains(ev.relatedTarget)) card.classList.remove("over");
});

$("#list").addEventListener("drop", (ev) => {
  ev.preventDefault();
  const card = ev.target.closest(".card");
  for (const el of document.querySelectorAll(".card.over")) {
    el.classList.remove("over");
  }
  if (!card || !dragId || card.dataset.id === dragId) return;
  // ordem do DOM (visíveis) + as escondidas no fim -> fila completa
  const visible = [...$("#list").querySelectorAll(".card:not([hidden])")]
    .map((el) => el.dataset.id);
  const rest = lastOrder.filter((id) => !visible.includes(id));
  const from = visible.indexOf(dragId);
  const to = visible.indexOf(card.dataset.id);
  if (from < 0 || to < 0) return;
  visible.splice(to, 0, visible.splice(from, 1)[0]);
  post("/reorder", { ids: [...visible, ...rest] })
    .then(() => tick())
    .catch((e) => toast(e.message));
});

$("#list").addEventListener("dragend", () => {
  dragId = null;
  $("#list").classList.remove("dragging-active");
  for (const el of document.querySelectorAll(".card.dragging, .card.over")) {
    el.classList.remove("dragging", "over");
  }
  tick();                      // devolve a ordem do servidor (se divergiu)
});

/* ------------------------------------------- configurações + SAF (APK) */
/* Botões "Escolher…" só existem de verdade no APK: a ponte Java
   (window.MinidmBridge) só está lá — no navegador ficam ocultos. */
const ANDROID = !!window.MinidmBridge;
if (ANDROID) {
  $("#dest-pick").hidden = false;
  $("#cfg-pick").hidden = false;
}

/** Callback do seletor de pastas do Android (ACTION_OPEN_DOCUMENT_TREE).
   id = input que pediu; path vazio = cancelou/não deu para usar. */
window.minidmFolderPicked = (id, path) => {
  const el = document.getElementById(id);
  if (!el) return;
  if (!path) {
    toast("não deu para usar essa pasta neste Android");
    return;
  }
  el.value = path;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  toast("pasta escolhida");
};

/** Pede ao Android para abrir o seletor de pastas (SAF). */
function askFolder(inputId) {
  if (!ANDROID) return;
  try { window.MinidmBridge.pickFolder(inputId); }
  catch (_) { toast("seletor de pastas indisponível"); }
}
$("#dest-pick").addEventListener("click", () => askFolder("dest"));
$("#cfg-pick").addEventListener("click", () => askFolder("cf-download_base"));

/** Campos editáveis da tela (espelho do _CFG_EDIT do server.py). */
const CFG_FIELDS = [
  "max_concurrent", "max_connections", "segments", "retries",
  "throttle_bps", "concurrent_fragments", "playlist_limit",
  "proxy", "cookies_from_browser", "sponsorblock",
  "use_ytdlp_downloader", "download_archive", "download_base",
  "android_public_downloads", "verify_signatures", "remote_checksums",
];

/** Lê o form de configurações -> payload (throttle KB/s -> bytes/s). */
function cfgPayload() {
  const p = {};
  for (const k of CFG_FIELDS) {
    const el = $(`#cf-${k}`);
    if (!el) continue;
    if (k === "throttle_bps") {
      p[k] = Math.max(0, Math.round((Number(el.value) || 0) * 1024));
    } else if (el.tagName === "SELECT") {
      p[k] = el.value === "true" ? true : el.value === "false" ? false
        : el.value;
    } else {
      p[k] = el.value;
    }
  }
  return p;
}

/** Abre a tela de configurações já preenchida (GET /config). */
async function openCfg() {
  let c;
  try {
    c = await api("/config");
  } catch (e) {
    toast(e.message);
    return;
  }
  for (const k of CFG_FIELDS) {
    const el = $(`#cf-${k}`);
    if (!el || c[k] === undefined) continue;
    if (k === "throttle_bps") el.value = Math.round((c[k] || 0) / 1024);
    else if (el.tagName === "SELECT") el.value = String(c[k]);
    else el.value = c[k];
  }
  $("#cfg").hidden = false;
}

function closeCfg() { $("#cfg").hidden = true; }

$("#cfg-btn").addEventListener("click", openCfg);
$("#cfg-close").addEventListener("click", closeCfg);
$("#cfg-cancel").addEventListener("click", closeCfg);
$("#cfg").addEventListener("click", (ev) => {
  if (ev.target.id === "cfg") closeCfg();       // clique fora fecha
});

$("#cfg-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await post("/config", cfgPayload());
  } catch (e) {
    // ex.: "config inválida (max_concurrent: máximo 64)" — nada foi gravado
    toast(e.message);
    return;
  }
  closeCfg();
  toast("Configurações salvas");
  // download_base mudou: recarrega categorias/placeholder do destino
  loadCategories();
  tick();
});

/* -------------------------------------------------------------- início */
loadCategories();
tick();
