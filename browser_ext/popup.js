// popup.js — lista mídia capturada na aba e envia para o minidm.
//
// Para cada item, o popup pode chamar POST /media/analyze (que o minidm
// resolve HLS/DASH e devolve as qualidades) — é o equivalente ao menu de
// qualidades do IDM ao capturar um vídeo.

const $ = (sel) => document.querySelector(sel);

let currentTab = null;

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tab || null;

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

function render(items) {
  const list = $("#list");
  if (!items.length) {
    list.innerHTML = `<div class="empty">Nenhuma mídia detectada nesta aba ainda.<br>
      Toque um vídeo ou abra uma página com player.</div>`;
    return;
  }
  // mais recentes primeiro; HLS/DASH primeiro (são os que têm qualidades)
  items.sort((a, b) => (b.time || 0) - (a.time || 0));
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

  const urlEl = document.createElement("div");
  urlEl.className = "url";
  urlEl.textContent = shortUrl(item.url);
  urlEl.title = item.url;
  el.appendChild(urlEl);

  const meta = document.createElement("div");
  meta.className = "meta";
  const bits = [kind];
  if (item.contentType) bits.push(item.contentType.split(";")[0]);
  if (item.size) bits.push(humanSize(item.size));
  if (item.filename) bits.push(item.filename);
  bits.push(item.via === "dom" ? "detectado na página" : "capturado da rede");
  meta.textContent = bits.join(" · ");
  el.appendChild(meta);

  const actions = document.createElement("div");
  actions.className = "actions";

  const dl = document.createElement("button");
  dl.textContent = "Baixar";
  dl.addEventListener("click", () => download(el, item, null));
  actions.appendChild(dl);

  if (isStream) {
    const qbtn = document.createElement("button");
    qbtn.className = "secondary";
    qbtn.textContent = "Qualidades…";
    qbtn.addEventListener("click", () => showQualities(el, item));
    actions.appendChild(qbtn);
  }

  el.appendChild(actions);
  return el;
}

async function showQualities(el, item) {
  const container = el.querySelector(".qualities");
  if (container) { container.remove(); return; }

  const box = document.createElement("div");
  box.className = "qualities";
  box.innerHTML = `<div class="q">analisando…</div>`;
  el.appendChild(box);

  const res = await chrome.runtime.sendMessage({
    type: "analyze",
    payload: { url: item.url, referer: item.referer || (currentTab && currentTab.url) || "" },
  });

  if (!res || !res.ok) {
    box.innerHTML = `<div class="q">falha: ${(res && res.error) || "sem resposta"}</div>`;
    return;
  }
  box.innerHTML = "";
  if (!res.qualities || !res.qualities.length) {
    box.innerHTML = `<div class="q">nenhuma qualidade encontrada</div>`;
    return;
  }
  for (const q of res.qualities) {
    const row = document.createElement("div");
    row.className = "q";

    const label = document.createElement("span");
    label.className = "label";
    label.textContent = q.label;

    const size = document.createElement("span");
    size.className = "size";
    size.textContent = q.estimated_size ? humanSize(q.estimated_size) : "";

    const btn = document.createElement("button");
    btn.textContent = "Baixar";
    btn.addEventListener("click", () =>
      download(el, { ...item, url: q.url }, q.label));

    row.append(label, size, btn);
    box.appendChild(row);
  }
}

async function download(el, item, qualityLabel) {
  const btns = el.querySelectorAll("button");
  btns.forEach((b) => (b.disabled = true));

  const isStream = /\.m3u8|\.mpd/i.test(item.url);
  const payload = {
    url: item.url,
    referer: (currentTab && currentTab.url) || "",
    filename: item.filename || undefined,
    kind: isStream ? (/\.mpd/i.test(item.url) ? "dash" : "hls") : undefined,
  };
  if (qualityLabel) payload.filename = undefined; // deixa o minidm nomear

  const res = await chrome.runtime.sendMessage({ type: "download", payload });
  btns.forEach((b) => (b.disabled = false));

  if (res && res.ok) {
    const msg = document.createElement("div");
    msg.className = "meta";
    msg.textContent = `✓ na fila${res.id ? ` [${res.id}]` : ""}` +
      (qualityLabel ? ` — ${qualityLabel}` : "");
    el.appendChild(msg);
  }
}

init();
