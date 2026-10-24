// minidm bridge v2 — captura vídeos da navegação e envia para o minidm.
//
// Inspirado no monitor de navegação do IDM (IEMonitor/IDMNetMon): observa as
// requisições da aba e as tags <video> do DOM, guarda o que parece mídia e
// deixa o popup mostrar as qualidades (via POST /media/analyze do minidm).
//
// Requer `python -m minidm serve` (ou a GUI com "API do navegador" marcada).

const API = "http://127.0.0.1:9742";
const MAX_PER_TAB = 60;          // guarda no máximo N itens por aba

// extensões/tipos que tratamos como mídia (IDM faz parecido no sniffer)
const MEDIA_EXT = /\.(mp4|m4v|webm|mkv|mov|flv|avi|wmv|ts|m4s|m3u8|mpd|mp3|m4a|aac|ogg|opus|wav)(\?|$)/i;
const MEDIA_MIME = /(video\/|audio\/|application\/vnd\.apple\.mpegurl|application\/x-mpegurl|application\/dash\+xml)/i;

// ------------------------------------------------------------- contexto
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "send-to-minidm",
    title: "Enviar para o minidm",
    contexts: ["link", "page"],
  });
  chrome.contextMenus.create({
    id: "download-video",
    title: "Baixar vídeo desta página com o minidm",
    contexts: ["page", "video"],
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "send-to-minidm") {
    const url = info.linkUrl || (tab && tab.url);
    if (url) send({
      url,
      referer: tab ? tab.url : "",
      page_title: tab ? tab.title || "" : "",
    });
  } else if (info.menuItemId === "download-video") {
    // abre o popup mostrando o que foi capturado nesta aba
    if (tab) chrome.action.openPopup();
  }
});

// ------------------------------------------------------ captura webRequest
// Observa requisições de mídia em todas as abas (não bloqueia nada — só lê).
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (details.tabId < 0) return;                 // ignora requisições da extensão/service worker
    const url = details.url;
    const isMediaExt = MEDIA_EXT.test(url);
    const isMediaType = ["media", "xmlhttprequest", "other", "sub_frame"].includes(details.type);
    if (!isMediaExt && !isMediaType) return;
    record(details.tabId, {
      url,
      via: "webRequest",
      resourceType: details.type,
      time: Date.now(),
    });
  },
  { urls: ["<all_urls>"] }
);

// pega Content-Type/Content-Length quando a resposta chega
chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (details.tabId < 0) return;
    const ctype = headerValue(details.responseHeaders, "content-type");
    const clen = headerValue(details.responseHeaders, "content-length");
    const disp = headerValue(details.responseHeaders, "content-disposition");
    const looksMedia = (ctype && MEDIA_MIME.test(ctype)) ||
                       MEDIA_EXT.test(details.url);
    if (!looksMedia) return;
    record(details.tabId, {
      url: details.url,
      via: "webRequest",
      contentType: ctype || "",
      size: clen ? parseInt(clen, 10) || null : null,
      filename: filenameFromDisposition(disp),
      time: Date.now(),
    });
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

function headerValue(headers, name) {
  if (!headers) return null;
  const h = headers.find((x) => x.name.toLowerCase() === name);
  return h ? h.value : null;
}

function filenameFromDisposition(disp) {
  if (!disp) return null;
  const m = disp.match(/filename\*?=(?:UTF-8''|"?)([^";]+)/i);
  return m ? decodeURIComponent(m[1].trim()) : null;
}

// ------------------------------------------- captura via content script
// O content script varre <video>/<source> e manda as URLs pra cá.
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === "media-found") {
    const tabId = sender.tab ? sender.tab.id : -1;
    if (tabId >= 0) {
      for (const item of msg.items || []) {
        record(tabId, {
          url: item.url,
          via: "dom",
          contentType: item.contentType || "",
          duration: item.duration || null,
          time: Date.now(),
        });
      }
    }
    reply && reply({ ok: true });
    return true;
  }
  if (msg && msg.type === "get-media") {
    getMedia(msg.tabId).then((items) => reply({ items }));
    return true;                                  // resposta assíncrona
  }
  if (msg && msg.type === "download") {
    downloadItem(msg.payload).then((res) => reply(res));
    return true;
  }
  if (msg && msg.type === "analyze") {
    analyze(msg.payload).then((res) => reply(res));
    return true;
  }
  if (msg && msg.type === "ping") {
    probeApi().then((up) => reply({ up }));
    return true;
  }
});

// ------------------------------------------------------------- storage
async function record(tabId, item) {
  const key = `tab:${tabId}`;
  const store = await chrome.storage.session.get(key);
  const items = store[key] || [];
  const norm = normalize(item.url);
  const existing = items.findIndex((x) => normalize(x.url) === norm);
  if (existing >= 0) {
    // completa os metadados que faltavam
    const cur = items[existing];
    items[existing] = { ...cur, ...item, url: cur.url };
  } else {
    items.push(item);
    if (items.length > MAX_PER_TAB) items.shift();
  }
  await chrome.storage.session.set({ [key]: items });
  // badge discreto no ícone mostrando quantos vídeos a aba tem
  try {
    await chrome.action.setBadgeText({ tabId, text: String(items.length) });
    await chrome.action.setBadgeBackgroundColor({ tabId, color: "#0a7bd8" });
  } catch (e) { /* aba pode já ter fechado */ }
}

async function getMedia(tabId) {
  const key = `tab:${tabId}`;
  const store = await chrome.storage.session.get(key);
  return store[key] || [];
}

// limpa o storage quando a aba fecha
chrome.tabs.onRemoved.addListener((tabId) => {
  chrome.storage.session.remove(`tab:${tabId}`);
});

// ------------------------------------------------------------- API minidm
async function probeApi() {
  try {
    const r = await fetch(`${API}/status`, { method: "GET" });
    return r.ok;
  } catch (e) {
    return false;
  }
}

// cookies do domínio da mídia — alguns CDNs exigem (o IDM também manda)
async function cookiesFor(url) {
  try {
    const list = await chrome.cookies.getAll({ url });
    return list.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (e) {
    return "";
  }
}

async function analyze({ url, referer, page_title }) {
  const cookie = await cookiesFor(url);
  try {
    const r = await fetch(`${API}/media/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        referer: referer || "",
        page_title: page_title || "",
        cookie,
        user_agent: navigator.userAgent,
      }),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, error: `minidm não respondeu (${e.message})` };
  }
}

async function downloadItem(payload) {
  const cookie = await cookiesFor(payload.url);
  try {
    const r = await fetch(`${API}/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...payload,
        cookie,
        user_agent: navigator.userAgent,
      }),
    });
    const data = await r.json();
    if (r.ok) {
      notify("Enviado ao minidm", payload.filename || payload.url);
      return { ok: true, ...data };
    }
    throw new Error(data.error || `HTTP ${r.status}`);
  } catch (e) {
    notify("Falha no envio",
           `${e.message}\nRode: python -m minidm serve`);
    return { ok: false, error: e.message };
  }
}

async function send(payload) {
  return downloadItem(payload);
}

function notify(title, message) {
  chrome.notifications.create({
    type: "basic",
    iconUrl: "icon.png",
    title: `minidm — ${title}`,
    message: String(message).slice(0, 180),
  });
}

function normalize(url) {
  try {
    const u = new URL(url);
    u.hash = "";
    return u.href;
  } catch (e) {
    return url;
  }
}
