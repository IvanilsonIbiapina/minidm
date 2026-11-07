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

### 2. Android: downloads visíveis no sistema
Hoje os arquivos ficam no **armazenamento interno** do app
(`/data/data/br.com.minidm.app/files/Downloads/<categoria>`) — invisíveis
para o app "Downloads" do sistema e para file managers. Evolução:

* API 29+: gravar via **MediaStore.Downloads** (API Java exposta pelo
  Chaquopy) — sem permissão, arquivo aparece em `Download/` do sistema;
* API 24–28: `WRITE_EXTERNAL_STORAGE` (permissão em runtime) +
  `Environment.getExternalStoragePublicDirectory(DIRECTORY_DOWNLOADS)`;
* a fila/config continua no armazenamento interno (só o arquivo final
  muda de lugar).

### 3. "Programa de verdade" estilo IDM (desktop)
O IDM é sempre-presente: bandeja, inicializar com o Windows, captura do
navegador. O miolo do minidm já existe (daemon, `extsetup` com registro,
clipboard watch, scheduler, extensão do navegador) — falta a camada de
integração com o sistema:

* **duplo clique → GUI** (feito: `minidm.exe` sem args abre a janela);
* **LEIA-ME.txt** no zip com instruções + aviso do SmartScreen (feito);
* **bandeja (tray)** + fechar-para-bandeja na GUI;
* **autostart** (chave `Run` do registro — o `extsetup` já escreve
  registro) + atalhos no Menu Iniciar;
* **extensão do navegador instalada por padrão** no primeiro uso
  (`minidm ext guided` automatizado, com consentimento);
* **SmartScreen/assinatura**: exe sem assinatura é bloqueado uma vez;
  certificado de código assinatura é caro — manter a instrução no
  LEIA-ME e reavaliar se o projeto crescer.

### 4. Versões portable
O onedir já é quase portátil (extraia e rode). Falta o modo completo:

* arquivo `portable.flag` ao lado do exe → config/fila **ao lado do
  exe** (em vez de `~/.minidm`);
* empacotar um zip "portable" distinto (sem atalhos/inicialização);
* candidato opcional: build `onefile` p/ uso avulso (inicia mais lento
  que onedir — medir antes de trocar).

## Já feito (referência)

* Rodadas A–F do yt-dlp (CLI/subprocesso, nunca embutido).
* UI completa: tema, ícones, busca, DnD, menu de contexto, fila por
  arrasto, toasts, onboarding, detalhes ao vivo.
* Empacotamento: PyInstaller onedir (GUI+CLI), CI Win/Linux/macOS,
  bundle sem yt-dlp e sem numpy (57 MB), Termux p/ Android.
* **APK Android** (`android/`): Chaquopy 17.2 + WebView + `webui/`
  (página servida pela própria API); job `apk` na CI e anexo
  `minidm-android.apk` na Release por tag.
