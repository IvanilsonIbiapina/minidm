# ROADMAP / Backlog

Ideias fora da rodada atual — nada aqui é promessa de data; a ordem pode mudar.

## Backlog

### 1. Rust (se o projeto crescer)
Hoje o gargalo do minidm é **rede (I/O)**, não CPU — Python já entrega o
necessário, e o yt-dlp continua externo. Se um dia for reescrever:

* Alvo certo: o **core de download** (`downloader.py`/`stream.py` —
  segmentação, retry, mirrors, throttle, HLS AES) como crate `minidm-core`
  ligado via **PyO3/maturin**, mantendo CLI/GUI em Python.
* Ganho real esperado: menos overhead por segmento, memória previsível,
  `unsafe` isolado — não "ficar mais rápido" por princípio.
* Custo: build cruzado para Win/Linux/macOS no CI + integração com o
  PyInstaller (extensão nativa precisa entrar no bundle).
* **Sinergia com o APK**: um core em Rust pode ser exposto por FFI
  (uniFFI) e reusado no Android — hoje o APK leva o Python embutido
  (Chaquopy); um core nativo permitiria um app sem Python.

Não reescrever "porque sim": só se o core Python virar o gargalo medido.

### 2. "Programa de verdade" estilo IDM (desktop)
O IDM é sempre-presente: bandeja, inicializar com o Windows, captura do
navegador. O miolo do minidm já existe (daemon, `extsetup` com registro,
clipboard watch, scheduler, extensão do navegador) — falta a camada de
integração com o sistema:

* **duplo clique → GUI** (feito: `minidm.exe` sem args abre a janela);
* **LEIA-ME.txt** no zip com instruções + aviso do SmartScreen (feito);
* **bandeja (tray)** + fechar-para-bandeja na GUI (feito);
* **autostart** (feito: chave `Run` do registro no Windows + desktop
  entry XDG no Linux — `minidm autostart on|off|status` e menu Arquivo);
* **atalhos no Menu Iniciar** (feito: `minidm shortcuts on|off|status`
  e menu Arquivo — `.lnk` no Menu Iniciar do usuário no Windows via
  PowerShell embutido, desktop entry XDG no Linux; o zip portátil
  ganha atalho sem instalador, e o `status` avisa quando o atalho
  aponta para outra instalação);
* **extensão do navegador instalada por padrão** no primeiro uso
  (`minidm ext guided` automatizado, com consentimento);
* **SmartScreen/assinatura**: exe sem assinatura é bloqueado uma vez;
  certificado de código assinatura é caro — manter a instrução no
  LEIA-ME e reavaliar se o projeto crescer.

### 3. Versões portable
O onedir já é quase portátil (extraia e rode). O modo completo:

* **feito**: arquivo `portable.flag` ao lado do bundle → config/fila
  em `<flag>/data` (em vez de `~/.minidm`) — detecção sobe a árvore do
  exe, GUI/CLI/daemon compartilham a mesma pasta (`minidm/portable.py`);
* **feito**: zip "portable" distinto na release
  (`minidm-portable-win64.zip`, mesmo bundle + flag; o normal não tem
  a flag);
* candidato opcional: build `onefile` p/ uso avulso (inicia mais lento
  que onedir — medir antes de trocar).

## Já feito (referência)

* Rodadas A–F do yt-dlp (CLI/subprocesso, nunca embutido).
* **Downloads Android visíveis no sistema**: arquivo concluído é movido
  para `Download/<categoria>/` via MediaStore (API 29+, sem permissão) ou
  escrita direta + `WRITE_EXTERNAL_STORAGE` (24–28, pedido em runtime) —
  `minidm/androidpub.py` + `MainActivity.publish`, registrado em
  `task.published_path`; flag `android_public_downloads` (padrão ligado).
* UI completa: tema, ícones, busca, DnD, menu de contexto, fila por
  arrasto, toasts, onboarding, detalhes ao vivo.
* Empacotamento: PyInstaller onedir (GUI+CLI), CI Win/Linux/macOS,
  bundle sem yt-dlp e sem numpy (57 MB), Termux p/ Android.
* **APK Android** (`android/`): Chaquopy 17.2 + WebView + `webui/`
  (página servida pela própria API); job `apk` na CI e anexo
  `minidm-android.apk` na Release por tag.
