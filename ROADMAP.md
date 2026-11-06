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

## Já feito (referência)

* Rodadas A–F do yt-dlp (CLI/subprocesso, nunca embutido).
* UI completa: tema, ícones, busca, DnD, menu de contexto, fila por
  arrasto, toasts, onboarding, detalhes ao vivo.
* Empacotamento: PyInstaller onedir (GUI+CLI), CI Win/Linux/macOS,
  bundle sem yt-dlp e sem numpy (57 MB), Termux p/ Android.
* **APK Android** (`android/`): Chaquopy 17.2 + WebView + `webui/`
  (página servida pela própria API); job `apk` na CI e anexo
  `minidm-android.apk` na Release por tag.
