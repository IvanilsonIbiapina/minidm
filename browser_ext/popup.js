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

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tab || null;
  pageTitle = (tab && tab.title) || "";

  const { up } = await chrome.runtime.sendMessage({ type: "ping" });
  const dot = $("#apiDot");
  dot.classList.add(up ? "up" : "down");
  $("#status").textContent = up ? "conectado" : "minidm offline";

  const { items } = await chrome.runtime.sendMessage({
    type: "get-media",
    tabId: currentTab ? currentTab.id : -1,
  });
  render(items || []);
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
