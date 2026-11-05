// content.js — varre a página por <video>/<source> e reporta ao background.
//
// O IDM detecta o player da página e mostra o painel "Download this video".
// Aqui fazemos o equivalente mínimo: lemos as URLs de mídia já conhecidas
// pelo DOM (o webRequest pega o resto, inclusive HLS/DASH pedidos via XHR).

(function () {
  "use strict";

  function collect() {
    const out = [];
    const seen = new Set();

    function push(url, contentType, duration) {
      if (!url || seen.has(url)) return;
      if (url.startsWith("blob:") || url.startsWith("data:")) return;
      if (!/^https?:/i.test(url)) return;
      seen.add(url);
      out.push({ url, contentType: contentType || "", duration: duration || null });
    }

    for (const v of document.querySelectorAll("video")) {
      push(v.currentSrc || v.src, "", v.duration || null);
      for (const s of v.querySelectorAll("source")) {
        push(s.src, s.type || "", null);
      }
    }
    for (const a of document.querySelectorAll("audio")) {
      push(a.currentSrc || a.src, "", a.duration || null);
      for (const s of a.querySelectorAll("source")) {
        push(s.src, s.type || "", null);
      }
    }
    return out;
  }

  function report() {
    const items = collect();
    if (items.length) {
      try {
        chrome.runtime.sendMessage({ type: "media-found", items }, () => {
          // ignora erro "receiving end does not exist" quando o worker dormiu
          void chrome.runtime.lastError;
        });
      } catch (e) { /* contexto invalidado */ }
    }
  }

  report();

  // observa mudanças dinâmicas (players SPA carregam o src depois)
  const mo = new MutationObserver(() => {
    clearTimeout(mo._t);
    mo._t = setTimeout(report, 800);
  });
  mo.observe(document.documentElement, { childList: true, subtree: true });

  // quando o <video> começa a tocar, o currentSrc costuma estar completo
  document.addEventListener("play", (e) => {
    if (e.target && e.target.tagName === "VIDEO") report();
  }, true);
})();
