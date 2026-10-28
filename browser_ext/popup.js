// popup.js — lista mídia capturada na aba e envia para o minidm.
//
// Fluxo (igual ao IDM ao capturar um vídeo):
//   1. lista o que a aba tem, com OS QUE TÊM QUALIDADE PRIMEIRO (HLS/DASH);
//   2. "Baixar" abre a caixa "Salvar como": qualidade, nome do arquivo e
//      pasta (a última usada fica guardada);
//   3. envia para o minidm com Referer + cookies + título da página.
// DRM: quando o analyze recusa, mostramos o aviso em vermelho (não baixamos).

const $ = (sel) => document.querySelector(sel);

let currentTab = null;
let pageTitle = "";

function _option(value, label) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = label;
  return o;
}

// nomes dos perfis do minidm (via API /profiles)
let _profilesCache = null;
async function _profiles() {
  if (_profilesCache) return _profilesCache;
  try {
    const r = await fetch("http://127.0.0.1:9742/profiles");
    const d = await r.json();
    _profilesCache = (d && d.profiles ? d.profiles : []).map((p) => p.name);
  } catch (e) {
    _profilesCache = [];
  }
  return _profilesCache;
}

// categorias estilo IDM (via API /categories) — pasta + perfil
let _catCache = null;
async function _categories() {
  if (_catCache) return _catCache;
  try {
    const r = await fetch("http://127.0.0.1:9742/categories");
    _catCache = await r.json();
  } catch (e) {
    _catCache = null;
  }
  return _catCache;
}

// analisa a PÁGINA com o yt-dlp e devolve as qualidades REAIS do vídeo
async function _analyzePage(url) {
  try {
    return await chrome.runtime.sendMessage({
      type: "analyze",
      payload: { url, referer: url, ytdlp: true },
    });
  } catch (e) {
    return null;
  }
}

// alturas distintas (desc) + áudio, a partir da análise do yt-dlp
function _heightsFrom(analysis) {
  const out = { heights: [], audio: false };
  const qs = (analysis && analysis.qualities) || [];
  const seen = new Set();
  for (const q of qs) {
    if (q.audio_only) { out.audio = true; continue; }
    const h = q.height || 0;
    if (h && !seen.has(h)) { seen.add(h); out.heights.push(h); }
  }
  out.heights.sort((a, b) => b - a);
  return out;
}

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tab || null;
  pageTitle = (tab && tab.title) || "";

  const { up } = await chrome.runtime.sendMessage({ type: "ping" });
  const dot = $("#apiDot");
  dot.classList.add(up ? "up" : "down");
  $("#status").textContent = up ? "conectado" : "minidm offline";

  // card "baixar esta página" — para links de página (YouTube, Instagram…)
  // que o minidm delega ao yt-dlp. Só aparece quando faz sentido.
  renderPageDownload(tab, up);

  const { items } = await chrome.runtime.sendMessage({
    type: "get-media",
    tabId: currentTab ? currentTab.id : -1,
  });
  render(items || []);
}

// ------------------------------------------------ card "baixar esta página"
// Espelha o `looks_direct_media` do minidm: extensão de mídia no caminho?
function looksDirectMedia(url) {
  try {
    const u = new URL(url);
    const name = u.pathname.split("/").filter(Boolean).pop() || "";
    const m = name.match(/\.([a-z0-9]+)$/i);
    if (!m) return false;                       // sem extensão -> é página
    const ext = m[1].toLowerCase();
    return /^(mp4|m4v|webm|mkv|mov|flv|avi|wmv|ts|m4s|m3u8|mpd|mp3|m4a|aac|ogg|opus|wav|zip|rar|7z|gz|tar|iso|exe|msi|pdf|apk|jpg|jpeg|png|gif|webp|srt|vtt)$/i
      .test(ext);
  } catch (e) {
    return false;
  }
}

function isPageUrl(url) {
  if (!url) return false;
  try {
    const u = new URL(url);
    if (u.protocol !== "http:" && u.protocol !== "https:") return false;
  } catch (e) {
    return false;
  }
  return !looksDirectMedia(url);
}

async function renderPageDownload(tab, up) {
  const box = $("#pageDl");
  if (!tab || !tab.url || !isPageUrl(tab.url)) {
    box.classList.add("hidden");       // mídia direta: o fluxo normal basta
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "";

  const title = document.createElement("div");
  title.className = "ptitle";
  title.textContent = tab.title || "Baixar vídeo desta página";
  title.title = tab.title || "";
  box.appendChild(title);

  const hint = document.createElement("div");
  hint.className = "phint";
  hint.textContent = up
    ? "Página detectada — analisando as qualidades…"
    : "minidm offline — inicie com `python -m minidm serve`.";
  box.appendChild(hint);

  // categorias (API local, rápido) + qualidades REAIS (yt-dlp, pode demorar)
  const [cats, analysis] = await Promise.all([
    _categories(),
    up ? _analyzePage(tab.url) : Promise.resolve(null),
  ]);
  hint.textContent = up
    ? "Página detectada — baixa com o yt-dlp (qualquer site)."
    : "minidm offline — inicie com `python -m minidm serve`.";

  // --- categoria estilo IDM (Vídeos é a padrão) -> pasta + perfil
  const catRow = document.createElement("div");
  catRow.className = "row";
  const catLab = document.createElement("label");
  catLab.textContent = "Categoria:";
  const catSelect = document.createElement("select");
  const catList = (cats && cats.categories) || [];
  for (const c of catList) catSelect.appendChild(_option(c.name, c.name));
  if (!catList.length) catSelect.appendChild(_option("Vídeos", "Vídeos"));
  const defCat = catList.find((c) => c.default);
  if (defCat) catSelect.value = defCat.name;
  catRow.append(catLab, catSelect);
  box.appendChild(catRow);

  // --- qualidades REAIS do vídeo (só nas categorias de vídeo)
  const info = _heightsFrom(analysis);
  const qRow = document.createElement("div");
  qRow.className = "row";
  const qLab = document.createElement("label");
  qLab.textContent = "Qualidade:";
  const qSelect = document.createElement("select");
  qRow.append(qLab, qSelect);
  box.appendChild(qRow);

  const VIDEO_CATS = new Set(["Vídeos", "Videos", "Video"]);
  function fillQuality() {
    const isVideo = VIDEO_CATS.has(catSelect.value) || !catList.length;
    qRow.style.display = isVideo ? "" : "none";
    if (!isVideo) return;
    qSelect.innerHTML = "";
    qSelect.appendChild(_option("", "Melhor"));
    // alturas REAIS detectadas; se a análise falhou, cai numa lista fixa
    const hs = info.heights.length
      ? info.heights : [2160, 1440, 1080, 720, 480, 360];
    for (const h of hs) qSelect.appendChild(_option(String(h), `${h}p`));
    if (info.audio) qSelect.appendChild(_option("audio", "Só áudio"));
  }
  fillQuality();
  catSelect.addEventListener("change", fillQuality);

  // --- botão baixar (estilo IDM: um botão; a categoria manda na pasta)
  const btnRow = document.createElement("div");
  btnRow.className = "row";
  const dlBtn = document.createElement("button");
  dlBtn.textContent = "Baixar";
  btnRow.appendChild(dlBtn);
  box.appendChild(btnRow);

  const status = document.createElement("div");
  status.className = "row";
  box.appendChild(status);

  async function sendPage() {
    dlBtn.disabled = true;
    status.textContent = "enviando…";
    const payload = {
      url: tab.url,
      page_title: tab.title || "",
      referer: tab.url,
      category: catSelect.value || "Vídeos",
    };
    const q = qSelect.value;
    if (q === "audio") {
      // "Só áudio" -> usa a categoria Músicas (extrai áudio/ mp3)
      payload.category = "Músicas";
    } else if (q) {
      payload.height = parseInt(q, 10);   // teto de altura escolhido
    }
    const sent = await chrome.runtime.sendMessage({ type: "download", payload });
    dlBtn.disabled = false;
    if (sent && sent.ok) {
      status.textContent = `✓ na fila${sent.id ? ` [${sent.id}]` : ""}` +
        (sent.filename ? ` — ${sent.filename}` : "");
    } else {
      status.textContent = `falha: ${(sent && sent.error) || "sem resposta"}`;
    }
  }

  dlBtn.addEventListener("click", sendPage);
}

function humanSize(n) {
  if (!n && n !== 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function shortUrl(url) {
  try {
    const u = new URL(url);
    const path = decodeURIComponent(u.pathname);
    const name = path.split("/").filter(Boolean).pop() || u.hostname;
    return `${u.hostname}/…/${name}`.slice(0, 90);
  } catch (e) {
    return url.slice(0, 90);
  }
}

function kindOf(url, contentType) {
  const u = url.toLowerCase();
  if (contentType && /mpegurl/i.test(contentType)) return "HLS";
  if (contentType && /dash\+xml/i.test(contentType)) return "DASH";
  if (u.includes(".m3u8")) return "HLS";
  if (u.includes(".mpd")) return "DASH";
  if (u.match(/\.(mp4|m4v|webm|mkv|mov|flv|avi|wmv|ts)$/)) return "vídeo";
  if (u.match(/\.(mp3|m4a|aac|ogg|opus|wav)$/)) return "áudio";
  return "mídia";
}

// prioridade: quem tem QUALIDADE primeiro (HLS/DASH), depois vídeo, áudio,
// o resto — dentro do grupo, os mais recentes primeiro.
function priorityOf(item) {
  const k = kindOf(item.url, item.contentType);
  if (k === "HLS" || k === "DASH") return 0;
  if (k === "vídeo") return 1;
  if (k === "áudio") return 2;
  return 3;
}

function render(items) {
  const list = $("#list");
  if (!items.length) {
    list.innerHTML = `<div class="empty">Nenhuma mídia detectada nesta aba ainda.<br>
      Toque um vídeo ou abra uma página com player.</div>`;
    return;
  }
  items = items.slice().sort((a, b) =>
    priorityOf(a) - priorityOf(b) || (b.time || 0) - (a.time || 0));
  list.innerHTML = "";
  for (const item of items) {
    list.appendChild(renderItem(item));
  }
}

function renderItem(item) {
  const el = document.createElement("div");
  el.className = "item";

  const kind = kindOf(item.url, item.contentType);
  const isStream = kind === "HLS" || kind === "DASH";

  const head = document.createElement("div");
  head.className = "head";

  const badge = document.createElement("span");
  badge.className = `badge ${isStream ? "stream" : ""} ` +
    (kind === "áudio" ? "audio" : "");
  badge.textContent = isStream ? `${kind} · qualidades` : kind;
  head.appendChild(badge);

  const urlEl = document.createElement("div");
  urlEl.className = "url";
  urlEl.textContent = shortUrl(item.url);
  urlEl.title = item.url;
  head.appendChild(urlEl);
  el.appendChild(head);

  const meta = document.createElement("div");
  meta.className = "meta";
  const bits = [];
  if (item.contentType) bits.push(item.contentType.split(";")[0]);
  if (item.size) bits.push(humanSize(item.size));
  if (item.filename) bits.push(item.filename);
  bits.push(item.via === "dom" ? "detectado na página" : "capturado da rede");
  meta.textContent = bits.join(" · ");
  el.appendChild(meta);

  const actions = document.createElement("div");
  actions.className = "actions";

  const dl = document.createElement("button");
  dl.textContent = isStream ? "Escolher qualidade…" : "Baixar…";
  dl.addEventListener("click", () => openSaveDialog(el, item));
  actions.appendChild(dl);

  el.appendChild(actions);
  return el;
}

// --------------------------------------------------------- caixa Salvar como
// Estilo IDM: qualidade (quando há), nome editável e pasta de destino.
async function openSaveDialog(el, item, preselectedQuality) {
  const existing = el.querySelector(".save");
  if (existing) { existing.remove(); return; }

  const box = document.createElement("div");
  box.className = "save";
  box.innerHTML = `<div class="q">analisando…</div>`;
  el.appendChild(box);

  const res = await chrome.runtime.sendMessage({
    type: "analyze",
    payload: {
      url: item.url,
      referer: item.referer || (currentTab && currentTab.url) || "",
      page_title: pageTitle,
    },
  });

  if (!res || !res.ok) {
    const err = (res && res.error) || "sem resposta";
    const drm = res && res.drm;
    box.innerHTML = "";
    const msg = document.createElement("div");
    msg.className = drm ? "q drm" : "q error";
    msg.textContent = drm
      ? `DRM (${drm}) — conteúdo protegido, não é possível baixar`
      : `falha: ${err}`;
    box.appendChild(msg);
    return;
  }

  const qualities = res.qualities || [];
  box.innerHTML = "";

  // --- qualidade (só quando o servidor listou mais de uma opção)
  let qualitySelect = null;
  if (qualities.length > 1) {
    const row = document.createElement("div");
    row.className = "row";
    const lab = document.createElement("label");
    lab.textContent = "Qualidade:";
    qualitySelect = document.createElement("select");
    qualities.forEach((q, i) => {
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = q.label +
        (q.estimated_size ? ` — ${humanSize(q.estimated_size)}` : "");
      qualitySelect.appendChild(opt);
    });
    // padrão: a melhor qualidade (servidor já ordenou por conveniência,
    // mas garantimos a maior resolução/banda aqui)
    let best = 0;
    qualities.forEach((q, i) => {
      const c = qualities[best];
      const better = (q.height || 0) > (c.height || 0) ||
        ((q.height || 0) === (c.height || 0) &&
         (q.bandwidth || 0) > (c.bandwidth || 0) && !c.audio_only);
      if (better && !q.audio_only) best = i;
    });
    if (preselectedQuality != null) {
      const idx = qualities.findIndex((q) => q.label === preselectedQuality);
      if (idx >= 0) best = idx;
    }
    qualitySelect.value = String(best);
    row.append(lab, qualitySelect);
    box.appendChild(row);
  }
  const qualityAt = () =>
    qualitySelect ? qualities[parseInt(qualitySelect.value, 10)] : qualities[0];

  // --- perfil (estilo YT-DLnis: Música/Vídeo/… preenchem tudo)
  const profileRow = document.createElement("div");
  profileRow.className = "row";
  const profileLab = document.createElement("label");
  profileLab.textContent = "Perfil:";
  const profileSelect = document.createElement("select");
  profileSelect.appendChild(_option("", "— nenhum —"));
  for (const name of await _profiles()) {
    profileSelect.appendChild(_option(name, name));
  }
  profileRow.append(profileLab, profileSelect);
  box.appendChild(profileRow);

  // --- nome do arquivo (editável, como o "Salvar como" do IDM)
  const nameRow = document.createElement("div");
  nameRow.className = "row";
  const nameLab = document.createElement("label");
  nameLab.textContent = "Nome:";
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.value = res.filename || item.filename || "video";
  nameRow.append(nameLab, nameInput);
  box.appendChild(nameRow);

  // --- pasta de destino (lembra a última usada)
  const dirRow = document.createElement("div");
  dirRow.className = "row";
  const dirLab = document.createElement("label");
  dirLab.textContent = "Pasta:";
  const dirInput = document.createElement("input");
  dirInput.type = "text";
  dirInput.placeholder = "padrão do minidm (Downloads)";
  dirRow.append(dirLab, dirInput);
  box.appendChild(dirRow);
  chrome.storage.local.get("lastDir").then(({ lastDir }) => {
    if (lastDir) dirInput.value = lastDir;
  });

  // --- botão confirmar
  const btnRow = document.createElement("div");
  btnRow.className = "row";
  const go = document.createElement("button");
  go.textContent = "Iniciar download";
  const cancel = document.createElement("button");
  cancel.className = "secondary";
  cancel.textContent = "Cancelar";
  cancel.addEventListener("click", () => box.remove());
  btnRow.append(go, cancel);
  box.appendChild(btnRow);

  go.addEventListener("click", async () => {
    const q = qualityAt() || {};
    const payload = {
      url: q.url || item.url,
      quality_url: q.url || item.url,
      kind: q.kind || undefined,
      referer: item.referer || (currentTab && currentTab.url) || "",
      page_title: pageTitle,
      filename: nameInput.value.trim() || undefined,
      dir: dirInput.value.trim() || undefined,
      quality_label: q.label || undefined,
      profile: profileSelect.value || undefined,
    };
    btnRow.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const sent = await chrome.runtime.sendMessage({ type: "download", payload });
    btnRow.querySelectorAll("button").forEach((b) => (b.disabled = false));

    if (sent && sent.ok) {
      if (dirInput.value.trim()) {
        chrome.storage.local.set({ lastDir: dirInput.value.trim() });
      }
      box.innerHTML = "";
      const ok = document.createElement("div");
      ok.className = "q ok";
      ok.textContent = `✓ na fila${sent.id ? ` [${sent.id}]` : ""}` +
        (sent.filename ? ` — ${sent.filename}` : "") +
        (q.label && qualities.length > 1 ? ` (${q.label})` : "");
      box.appendChild(ok);
    } else {
      const err = document.createElement("div");
      err.className = "q error";
      err.textContent = `falha: ${(sent && sent.error) || "sem resposta"}`;
      box.appendChild(err);
    }
  });
}

init();
