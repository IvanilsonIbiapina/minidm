// Extensão de exemplo: envia links para a API local do minidm.
// Requer `python -m minidm serve` rodando na máquina.

const API = "http://127.0.0.1:9742";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "send-to-minidm",
    title: "Enviar para o minidm",
    contexts: ["link", "page"],
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const url = info.linkUrl || (tab && tab.url);
  if (!url) return;
  send(url);
});

async function send(url) {
  try {
    const r = await fetch(`${API}/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await r.json();
    if (r.ok) {
      chrome.notifications.create({
        type: "basic",
        iconUrl: "icon.png",
        title: "minidm",
        message: `Adicionado à fila: ${url}`,
      });
    } else {
      throw new Error(data.error || "falhou");
    }
  } catch (e) {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icon.png",
      title: "minidm",
      message: `Sem resposta do minidm (${e.message}). Rode: python -m minidm serve`,
    });
  }
}
