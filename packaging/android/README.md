# minidm no Android (APK + Termux)

O Android tem **dois caminhos oficiais**:

| Caminho | Interface | Bom para |
|---|---|---|
| **APK** (`android/`, Chaquopy + WebView) | webui touch (a mesma do navegador) | instalar como app e tocar |
| **Termux** (CLI + daemon) | terminal / TUI | automação, `curl`, scripts |

---

## APK (Chaquopy + WebView + webui)

O APK embute a **engine Python inteira** com o [Chaquopy 17.2]
(<https://chaquo.com/chaquopy/>) (Python 3.11, minSdk 24, arm64-v8a +
x86_64) e exibe a **`webui/`** — a página que o próprio `minidm/server.py`
serve em `http://127.0.0.1:9742/` — dentro de um `WebView`. Não há UI
duplicada: desktop, navegador e celular falam com a mesma API.

```
MainActivity (WebView)
   └── http://127.0.0.1:9742/  ←  server.py (API + webui)
                    └── scheduler → downloader/stream (requests)
```

### Instalar

1. Baixe `minidm-android.apk` da **GitHub Release** (ou do artefato
   `minidm-android` na aba *Actions* do repositório).
2. Instale no aparelho (aceite "fontes desconhecidas" para arquivos
   baixados pelo navegador).
3. Abra o **miniDM** — a tela já é a fila; cole um link e toque em
   *Baixar*.

### Onde ficam os arquivos

| O quê | Caminho (armazenamento interno do app) |
|---|---|
| Downloads | `/data/data/br.com.minidm.app/files/Downloads/<categoria>/` |
| Fila + config | `/data/data/br.com.minidm.app/files/.minidm/` |

Sem permissão de armazenamento (é espaço interno do app); para exportar
um arquivo, use o compartilhamento do gerenciador de arquivos ou rode a
versão Termux com `~/storage/shared`.

### Webui como controle remoto

A mesma página funciona fora do celular: com o daemon rodando no desktop
(`minidm daemon`), abra <http://127.0.0.1:9742/> no navegador — a fila é
a mesma API, a UI é o mesmo `webui/`.

### Build

```bash
# pré-requisitos: JDK 17, Android SDK, Python 3.11 (buildPython),
# Gradle 8.9+ (AGP 8.7.3)
gradle -p android assembleDebug
# saída: android/app/build/outputs/apk/debug/app-debug.apk
```

A CI (`.github/workflows/build.yml`, job `apk`) já builda e publica o
APK a cada push (artefato `minidm-android`) e o anexa à Release em tag
`v*`. O código Python **não é copiado no repo**: a task `stagePython`
sincroniza `minidm/` e `webui/` para dentro do build.

### O que funciona no APK

| Recurso | APK |
|---|---|
| Fila, retomada, segmentos, mirrors, throttle | ✅ (engine pura: `requests` + stdlib) |
| HLS/DASH (AES-128 via `cryptography`) | ✅ |
| Categorias estilo IDM (pasta + perfil) | ✅ |
| webui (tema claro/escuro, polling ao vivo) | ✅ |
| yt-dlp (sites complexos) | ✅ **embutido** (pip do Chaquopy, in-process — sem Termux, sem instalar nada) |
| ffmpeg (mux DASH/HLS, mp3) | ❌ não empacotado — vídeo HLS/DASH com faixa separada fica mudo |
| FTP/SFTP | ⚠️ `ftplib` ok; SFTP sem `paramiko` (não empacotado) |
| Extensão de navegador / clipboard | ❌ (desktop only) |
| DRM | ❌ fora de propósito permanente — detecta e recusa |

---

## Termux (CLI completa)

O Termux é um terminal Linux completo dentro do Android, onde o minidm
roda inteiro (CLI + fila + downloads em segundo plano).

### Instalar (5 minutos)

1. **Instale o Termux** — F-Droid (recomendado; a versão da Play Store está
   desatualizada): <https://f-droid.org/packages/com.termux/>
2. **Atualize os pacotes**:

   ```bash
   pkg update && pkg upgrade
   ```

3. **Instale as dependências**:

   ```bash
   pkg install python ffmpeg
   pip install -r https://raw.githubusercontent.com/SEU_REPO/main/packaging/requirements.txt
   ```

   (`ffmpeg` é opcional — mux HLS/DASH, conversão de áudio; sem ele o
   minidm baixa arquivos diretos normalmente.)

4. **Instale o minidm** (do repositório clonado ou via pip):

   ```bash
   pkg install git
   git clone https://SEU_REPO/minidm.git
   cd minidm
   pip install -e .
   ```

5. **Baixe um link**:

   ```bash
   minidm add "https://exemplo.com/video.mp4"
   minidm start        # inicia a fila
   minidm list         # acompanha
   ```

### Uso em segundo plano

O Termux continua baixando com a tela desligada enquanto o processo não
morrer. Para não perder a fila ao fechar o app:

```bash
# mantém a sessão viva (instale o plugin do Termux: API/Float)
termux-wake-lock
minidm daemon --no-clipboard
```

* `termux-wake-lock` — evita o Android matar o processo (bateria).
* `minidm daemon` — roda a fila + **API local** (`127.0.0.1:9742`), igual
  ao desktop; a extensão do navegador **não existe no Android**, mas a API
  continua útil p/ automação (`curl -X POST .../add`) e p/ abrir a webui
  num navegador do celular.
* A fila fica em `~/.minidm/tasks.json` — sobrevive a fechar o app.

### O que funciona / o que não (Termux)

| Recurso | Android (Termux) |
|---|---|
| CLI (`add`, `start`, `list`, `media`, `config`) | ✅ |
| Fila persistente + retomada + mirrors + throttle | ✅ |
| HLS/DASH (segmentos, AES-128, mux com ffmpeg) | ✅ |
| yt-dlp (delegação p/ sites complexos) | ✅ `pkg install yt-dlp` |
| FTP/SFTP | ✅ (`pip install paramiko`) |
| API local + automação por curl | ✅ |
| webui no navegador do celular | ✅ (`minidm daemon` + abrir a porta) |
| **GUI (janela)** | ❌ sem display — use a CLI/TUI |
| Dashboard TUI (`minidm daemon --dashboard`) | ✅ (precisa de `termux-open` p/ cores 256) |
| Extensão de navegador | ❌ (não há extensões no Chrome mobile) |
| Notificação ao concluir | ⚠️ use `termux-notification` no `--exec` |

### TUI bonita no celular

```bash
pkg install python rich
minidm daemon --dashboard    # barras, velocidade e ETA ao vivo
```

---

## Backlog (Android)

1. **ffmpeg no APK** — binário estático por ABI + mux DASH/HLS (sem ele
   o yt-dlp embutido ainda baixa os formatos progressivos).
2. **Runtime JS p/ YouTube** — o challenge do YouTube pode exigir
   node/deno (hoje sem eles o yt-dlp avisa e a extração pode vir
   parcial); investigar quickjs/`--remote-components`.
3. **Notificações** — `NotificationManager` na conclusão de downloads.
4. **Kivy/UI nativa** — só se a webui não bastar (a engine já roda).

Feito: **yt-dlp no APK** (frente 3 — embutido via pip; extração **e**
download de páginas rodam in-process: `minidm/ytdlp.py` (runner) +
`_spawn_in_process` em `minidm/ytdlpdl.py`, com progresso, parada e
interrupção por `KeyboardInterrupt` — mesma interface do subprocesso).

Nenhum item toca no escopo de DRM (fora de propósito permanente) nem em
bypass de licença — ver `docs/analise-idm.md`.
