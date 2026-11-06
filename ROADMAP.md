# ROADMAP / Backlog

Ideias fora da rodada atual — nada aqui é promessa de data; a ordem pode mudar.

## Backlog

### 1. APK nativo para Android (depois)
Hoje o Android só tem o caminho **Termux** (CLI completa; ver
`packaging/android/README.md`). Um **APK de verdade** fica no backlog:

* **Kivy + Buildozer** — GUI touch reusando a engine `minidm/*` inteira
  (é o caminho mais direto: a mesma Python vira APK); build via CI Linux.
* **Chaquopy** — app Android Studio (Kotlin) com Python embutido.
* **PWA** — `minidm daemon` + página web servida pela API local (a API já
  existe; faltaria um `webui/`).

Restrições permanentes continuam valendo: sem DRM, sem bypass de licença.

### 2. Rust (se o projeto crescer)
Hoje o gargalo do minidm é **rede (I/O)**, não CPU — Python já entrega o
necessário, e o yt-dlp continua externo. Se um dia for reescrever:

* Alvo certo: o **core de download** (`downloader.py`/`stream.py` —
  segmentação, retry, mirrors, throttle, HLS AES) como crate `minidm-core`
  ligado via **PyO3/maturin**, mantendo CLI/GUI em Python.
* Ganho real esperado: menos overhead por segmento, memória previsível,
  `unsafe` isolado — não "ficar mais rápido" por princípio.
* Custo: build cruzado para Win/Linux/macOS no CI + integração com o
  PyInstaller (extensão nativa precisa entrar no bundle).
* **Sinergia com o item 1**: um core em Rust pode ser exposto por FFI
  (uniFFI) e reusado no APK, evitando levar o Python p/ o celular.

Não reescrever "porque sim": só se o core Python virar o gargalo medido.

## Já feito (referência)

* Rodadas A–F do yt-dlp (CLI/subprocesso, nunca embutido).
* UI completa: tema, ícones, busca, DnD, menu de contexto, fila por
  arrasto, toasts, onboarding, detalhes ao vivo.
* Empacotamento: PyInstaller onedir (GUI+CLI), CI Win/Linux/macOS,
  bundle sem yt-dlp e sem numpy (57 MB), Termux p/ Android.
