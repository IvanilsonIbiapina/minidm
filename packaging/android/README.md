# minidm no Android (Termux)

**Não há binário nativo do minidm para Android** — a GUI usa tkinter, que
não roda em Android (não existe X11/Display). O caminho realista e testado
da comunidade é o **Termux**: um terminal Linux completo dentro do Android,
onde o minidm roda inteiro (CLI + fila + downloads em segundo plano).

> Por que não Kivy/Chaquopy agora: exigiria reescrever a interface (ou
> empacotar um Python inteiro por APK). Fica no backlog; o Termux dá o
> mesmo resultado hoje — "qualquer site, só o link" — com a CLI que já
> existe.

## Instalar (5 minutos)

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

## Uso em segundo plano

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
  continua útil p/ automação (`curl -X POST .../add`).
* A fila fica em `~/.minidm/tasks.json` — sobrevive a fechar o app.

## O que funciona / o que não

| Recurso | Android (Termux) |
|---|---|
| CLI (`add`, `start`, `list`, `media`, `config`) | ✅ |
| Fila persistente + retomada + mirrors + throttle | ✅ |
| HLS/DASH (segmentos, AES-128, mux com ffmpeg) | ✅ |
| yt-dlp (delegação p/ sites complexos) | ✅ `pkg install yt-dlp` |
| FTP/SFTP | ✅ (`pip install paramiko`) |
| API local + automação por curl | ✅ |
| **GUI (janela)** | ❌ sem display — use a CLI/TUI |
| Dashboard TUI (`minidm daemon --dashboard`) | ✅ (precisa de `termux-open` p/ cores 256) |
| Extensão de navegador | ❌ (não há extensões no Chrome mobile) |
| Notificação ao concluir | ⚠️ use `termux-notification` no `--exec` |

## Alternativa: TUI bonita no celular

```bash
pkg install python rich
minidm daemon --dashboard    # barras, velocidade e ETA ao vivo
```

## Backlog (Android nativo)

1. **Kivy + Buildozer** — GUI touch sobre a mesma engine (reuso total de
   `minidm/*`); gera APK via CI (Linux-only).
2. **Chaquopy** — Python dentro de um app Android Studio com UI nativa
   (Kotlin) chamando a engine.
3. **Servidor + PWA** — `minidm daemon` + página web no celular
   (a API já existe; faltaria um `webui/` servido por ela).

Nenhum dos três toca no escopo de DRM (fora de propósito permanente) nem
em bypass de licença — ver `docs/analise-idm.md`.
