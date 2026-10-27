# minidm

Gerenciador de downloads em Python, escrito como exercício de engenharia —
inspirado nos conceitos observados em gerenciadores comerciais (fila de
downloads, retomada, segmentação multi-thread, integração com navegador),
porém com código próprio e sem qualquer mecanismo de licenciamento.

## Recursos (v5 — perfis estilo YT-DLnis)

| Recurso | Como usar |
|---|---|
| Retomada por `Range` | `add --start`; Ctrl+C/kill salva e `start --all` continua |
| Segmentação multi-thread | `--segments N` (0 = automático por tamanho) |
| **Retry por segmento** (backoff exponencial + jitter) | automático; `--retries N` / `config set retries N` |
| **Mirrors** (URLs alternativas) | `add --mirror URL --mirror URL2` |
| **Pool global de concorrência** | `config set max_concurrent 3` e `max_connections 16` |
| **Controle de banda** (token bucket global) | `--throttle 3000000` (bytes/s) ou `config set throttle_bps` |
| **Modo daemon** (scheduler + clipboard + API) | `python -m minidm daemon` |
| **TUI com dashboard ao vivo** | `python -m minidm tui` |
| **Proxy/cookies/User-Agent** | `--proxy`, `--cookie`, `--user-agent` |
| **Checksum do servidor** (`.sha256`/`.md5`) | automático ao concluir; `add --sha256 HASH` |
| **Assinatura Authenticode** | automático em `.exe/.dll/.msi`; `python -m minidm verify <arq>` |
| Fila persistente | `add`, `list`, `start`, `pause`, `remove` |
| Área de transferência | `python -m minidm watch` (ou no daemon) |
| API p/ extensão do navegador | `python -m minidm serve` + `browser_ext/` |
| **Captura de vídeo (HLS/DASH/direto)** | extensão detecta vídeos e lista qualidades (abaixo) |
| **Download de stream HLS/DASH** | `media <url>` — segmentos `.ts`/`.m4s` em paralelo, concatena e decifra AES-128 |
| **Detecção de DRM** (Widevine/FairPlay/PlayReady) | analisa e **recusa** com aviso — nunca tenta contornar (DMCA §1201) |
| **Faixa de áudio separada** | HLS `EXT-X-MEDIA`/DASH áudio viram qualidade "áudio" (baixar música) |
| **Nome de arquivo legível** | `Content-Disposition` → URL → título da página (editável no popup) |
| **Pasta de destino escolhida** | popup e `/add` aceitam `dir`; a última pasta fica guardada |
| **Análise via yt-dlp** (opcional) | `media <url> --ytdlp` — cobre sites complexos (chama a CLI do yt-dlp) |
| **Perfis de download** (conceito do YT-DLnis) | `add --profile Música` — perfil define qualidade, formato, pasta e nome |
| **Templates de comando yt-dlp** | `CommandTemplate` com `url_regex` de ativação automática (`profiles` no CLI) |
| **Template de nome de arquivo** | motor `{title} [{id}].{ext}` com preview e sanitização (`nametpl.py`) |
| **Download-archive** (não rebaixar) | `config set download_archive true` — URLs concluídas são puladas |
| **`--concurrent-fragments`** | `config set concurrent_fragments 4` — fragmentos simultâneos por stream |
| **Downloader yt-dlp** (delegação) | `add --ytdlp` ou perfil `use_ytdlp_downloader` — mesma interface do scheduler |

## Instalação

Requer Python 3.11+ e `requests` + `rich` (a interface usa `tkinter`, que já
vem com o Python). Para streams **criptografados (HLS AES-128)**, instale
também `cryptography`:

```bat
python -m pip install requests rich cryptography
```

## Captura de vídeos no navegador (estilo IDM)

O IDM detecta vídeos enquanto você navega e oferece download com as
qualidades disponíveis. O `minidm` faz o mesmo com a extensão `browser_ext/`:

1. **Ligue a API** — rode `python -m minidm serve` (ou `daemon`, ou marque
   **API do navegador** na GUI). A API fica em `http://127.0.0.1:9742`.
2. **Instale a extensão** — no Chrome/Edge: `chrome://extensions` →
   **Modo programador** → **Carregar expandida** → escolha a pasta
   `browser_ext/`. (No Firefox, use a versão MV2 — não testada aqui.)
3. **Navegue** — a extensão observa as requisições da aba (`webRequest`) e as
   tags `<video>/<source>`, guardando tudo que parece mídia (`.mp4`, `.m3u8`,
   `.mpd`, `.webm`, áudio…). O ícone mostra um badge com o nº de mídias.
4. **Clique no ícone** — o painel lista o que foi capturado **com os que têm
   qualidade primeiro** (HLS/DASH no topo, depois vídeo, áudio e o resto).
   **Baixar…** abre a caixa *Salvar como* (estilo IDM): qualidade (quando
   há), **perfil** (Música/Vídeo/…, vem do `GET /profiles`), **nome do
   arquivo editável** (sugerido pelo `Content-Disposition`, URL ou título
   da página) e **pasta de destino** — a última usada fica guardada. O
   envio leva `Referer`, cookies do domínio e o título da aba, como o IDM
   faz.

Também há botão de contexto **Enviar para o minidm** (link/página) e
**Baixar vídeo desta página**.

### Análise via terminal

```bat
python -m minidm media "https://…/master.m3u8"
python -m minidm media "https://…/master.m3u8" --download 2 --start
python -m minidm media "https://exemplo.com/watch?v=…" --ytdlp --download 0 --start
```

O `media` detecta o tipo (master HLS, media HLS, DASH ou arquivo direto),
lista as qualidades com bitrate/tamanho estimado, e `--download N` baixa a
 escolhida. Streams HLS viram `.ts`; DASH e fMP4 viram `.mp4`.

Com `--ytdlp`, a análise é delegada ao **yt-dlp instalado** (chamado como
CLI externa — o código deles é GPL-3.0 e não é copiado). Isso cobre sites
que exigem player próprio; se o yt-dlp falhar ou não existir, o parser
HLS/DASH próprio assume.

## Perfis de download (conceito do YT-DLnis)

Inspirado no sistema de **categorias/CommandTemplate** do YT-DLnis
(GPL-3.0 — só o conceito é usado, nunca código). Um **perfil** é um "modo
de baixar": qualidade, formato alvo, pasta e template de nome. Em vez de
configurar tudo a cada download, escolhe o perfil.

Perfis embutidos:

| Perfil | Qualidade | Alvo | Nome |
|---|---|---|---|
| **Padrão** | `best` | — | `{title} [{id}].{ext}` |
| **Música** | `bestaudio` | `mp3` | `{title}.{ext}` |
| **Vídeo** | `best[height<=1080]` | `mp4` | `{title} [{id}].{ext}` |

```bat
:: baixa como música (áudio extraído, convertido p/ mp3 via yt-dlp)
python -m minidm add "https://youtube.com/watch?v=…" --profile Música

:: lista perfis e templates de comando
python -m minidm profiles
python -m minidm profiles --show Música
python -m minidm profiles --new Podcast     # cria um perfil novo (edite profiles.json)
```

O perfil é salvo em `<data_dir>/profiles.json` (editável à mão). Cada
perfil pode referenciar **templates de comando** — argumentos extras do
yt-dlp (ex.: `--embed-thumbnail`) ativáveis por perfil ou automaticamente
por `url_regex` (como o `CommandTemplate` do YTDLnis).

### Template de nome de arquivo

Motor próprio (`nametpl.py`) com placeholders `{...}` e sanitização
Windows. Placeholders: `{title}`, `{id}`, `{ext}`, `{uploader}`,
`{duration}`, `{height}`, `{quality}`, `{date}`, `{n}`, `{filename}`.
Grupos que ficam vazios são removidos (`aula [].mp4` → `aula.mp4`),
caracteres inválidos viram `_`, `{{literal}}` fica literal.

### Download-archive e fragmentos

```bat
python -m minidm config set download_archive true   # não rebaixar URLs já concluídas
python -m minidm config set concurrent_fragments 4  # fragmentos HLS/DASH simultâneos
```

### Delegar download ao yt-dlp

Com o yt-dlp instalado, o perfil ou `add --ytdlp` fazem o scheduler
delegar o download à CLI do yt-dlp (`YtdlpDownloader`), mantendo a mesma
interface de progresso/pausa. Sem yt-dlp, a engine própria (segmentada)
assume automaticamente.

### DRM: detectamos, nunca contornamos

Streams protegidos por **Widevine/FairPlay/PlayReady** (Netflix, Disney+,
YouTube Premium…) têm as chaves entregues por um CDM licenciado dentro do
navegador. Baixar exige extrair essas chaves, o que viola a DMCA §1201 /
Lei 9.610 (proteção de medidas tecnológicas) — e o IDM também não baixa
isso. O `minidm` apenas **detecta** e recusa com aviso claro:

* HLS: `#EXT-X-SESSION-KEY` no master ou `#EXT-X-KEY` com
  `SAMPLE-AES`/`KEYFORMAT` de DRM na playlist;
* DASH: `<ContentProtection schemeIdUri="urn:uuid:…">` (Widevine, PlayReady,
  FairPlay, Marlin) no `AdaptationSet` ou `Representation`;
* a recusa acontece na análise **e** no downloader (defesa em profundidade).

Streams com **AES-128 comum** (sem DRM) continuam totalmente suportados —
é o caso típico de CDN que só quer criptografar a entrega.

### Áudio (música)

Faixas de áudio separadas viram qualidades baixáveis marcadas como áudio:

* HLS: `#EXT-X-MEDIA:TYPE=AUDIO,URI="…"` no master → item "áudio — nome";
* DASH: `Representation` de `mimeType="audio/*"` → marcada `audio_only`.

No terminal: `media <master.m3u8>` lista a faixa; no popup ela aparece com
badge roxo. O `--download` correspondente grava o áudio (extensão `.ts`).

### O que a engine de stream faz

* resolve master → variante (maior bitrate, ou a escolhida);
* baixa os segmentos em paralelo (semáforo de conexões + token bucket
  globais, retry com backoff);
* decifra **AES-128/CBC** (HLS `#EXT-X-KEY`), com IV do playlist ou número
  de sequência;
* entende `#EXT-X-MAP` (fMP4), `#EXT-X-BYTERANGE`, DASH `SegmentTemplate`
  (+ `SegmentTimeline`) e `SegmentList`;
* concatena tudo num arquivo final e calcula o SHA-256;
* **retoma** de onde parou (segmentos já baixados ficam em
  `.<arquivo>.segs/` e são pulados).

## interface gráfica (sem terminal)

Dê duplo clique em **`minidm-gui.bat`** — abre a janela do minidm. O
scheduler roda dentro da própria GUI (funciona sozinho, sem daemon separado).

O que dá pra fazer na janela:

| Ação | Como |
|---|---|
| Adicionar download | botão **+ Adicionar URL** (ou `Ctrl+N`) — URL, mirrors, pasta, SHA-256, segmentos |
| Iniciar / Pausar / Remover | botões da barra (ou `Delete`) — pausar salva o progresso |
| Ver detalhes | **duplo clique** na tarefa (segmentos, hash, assinatura, erro) |
| Abrir pasta do arquivo | selecionar e **Abrir pasta** |
| Capturar links copiados | marcar **Área de transferência** |
| Aceitar links do navegador | marcar **API do navegador** (a extensão manda pra `127.0.0.1:9742`) |
| Ajustar tudo | **Configurações…** — concorrência, banda, retries, proxy, cookies, verificação |

`minidm-gui-debug.bat` abre o mesmo com janela de terminal, para ver mensagens.

Via terminal (se preferir): `python -m minidm gui`

---

## uso via terminal

```bat
cd C:\Users\ivani\projects\minidm

:: download com 8 segmentos, validando hash publicado
python -m minidm add https://exemplo.com/grande.iso --segments 8 ^
    --sha256 <hash> --mirror https://espelho/grande.iso --start

:: com limite de 3 MB/s
python -m minidm add https://exemplo.com/video.mp4 --throttle 3000000 --start

:: rodar em background (scheduler + clipboard + API do navegador)
python -m minidm daemon

:: idem, com dashboard ao vivo no terminal
python -m minidm tui

:: ver/editar configuração persistente
python -m minidm config show
python -m minidm config set max_concurrent 4

:: revalidar um arquivo já baixado (hash + assinatura)
python -m minidm verify C:\Downloads\app.exe --url https://exemplo.com/app.exe
```

### Extensão do navegador

`browser_ext/` é uma extensão MV3. Em `chrome://extensions`, ative **Modo de
desenvolvedor** → **Carregar descompactada** → aponte para a pasta
`browser_ext`. Com o `minidm daemon`/`serve` rodando (ou **API do navegador**
marcado na GUI), a extensão captura vídeos da navegação e o painel (ícone)
mostra as qualidades. Botão direito → **Enviar para o minidm**.

## API local (127.0.0.1:9742)

```
GET  /status                  -> tarefas + config resumida
GET  /config                  -> configuração efetiva
GET  /profiles                -> perfis + templates de comando (popup)
POST /add      {url, filename?, dir?, kind?, referer?, page_title?,
                quality?|quality_url?, profile?, ytdlp?, mirrors?, sha256?,
                proxy?, cookie?, user_agent?, start?}
POST /start    {id|ids|all}
POST /pause    {id}
POST /remove   {id, delete?}
POST /media/analyze {url, referer?, page_title?, cookie?, user_agent?,
                     proxy?, ytdlp?}
                   -> {ok, kind, qualities:[{label,url,bandwidth,
                       resolution,height,estimated_size,audio_only}],
                       duration, encrypted, drm, filename}
                   -> {ok:false, error, drm?}   # DRM recusado com aviso
```

`/add` com `kind:"auto"` faz o servidor **analisar e escolher** (como o
"Baixar vídeo" do IDM): `quality` aceita índice (`0`), rótulo
(`"720p (1280x720)"`) ou vazio = a melhor. `quality_url` envia a mídia
já escolhida (fluxo do popup, evita reanalisar). `dir`/`filename` são o
caminho e nome escolhidos pelo usuário; `profile` aplica o perfil
(preence nome/pasta/args yt-dlp — o `filename` explícito tem prioridade).
Se a URL tiver DRM, o `/add` responde `400 {ok:false, drm, error}`.

## Como funciona

1. **Sonda** (`HEAD`/`Range 0-0`): tamanho, `Accept-Ranges`, `ETag`/`Last-Modified`.
   Com mirrors, tenta as URLs em ordem até uma responder.
2. **Planeja** N segmentos; cria o `.part` já no tamanho final (pré-aloca).
3. **Baixa**: cada segmento envia `Range: bytes=a-b` e grava direto na posição
   correta do `.part`. Todos os workers dividem um semáforo de conexões e um
   token bucket de banda (limites globais).
4. **Retry**: falha no segmento → backoff exponencial (1s, 2s, 4s… até
   `backoff_max`) com jitter; nas retentativas **alterna entre URL principal e
   mirrors**.
5. **Checkpoint**: a cada ~2 s o progresso vai para `.meta.json`. Qualquer
   interrupção (Ctrl+C, `kill`, queda de energia) é recuperável.
6. **Retomar**: ao reiniciar, revalida `ETag`/tamanho contra o servidor; se
   mudou, recomeça; senão continua de onde parou.
7. **Conclusão**: renomeia `.part`, calcula SHA-256, busca checksums
   publicados (`<arq>.sha256`/`.md5`), compara, e verifica a assinatura
   Authenticode de executáveis.

## Estrutura

```
minidm-gui.bat         duplo clique -> interface gráfica
minidm-gui-debug.bat   idem, com terminal (depuração)
docs/analise-idm.md    análise reversa do IDM que embasa este projeto
minidm/
  gui.py         interface gráfica (tkinter, roda o scheduler em processo)
  cli.py         argparse + comandos (gui, add, media, profiles, daemon, …)
  config.py      AppConfig (arquivo + flags)
  meta.py        Task/Segment + persistência (kind/referer/stream/profile)
  remote.py      sondagem do servidor
  downloader.py  engine de arquivo: segmentos, retry, mirrors, throttle
  stream.py      engine de stream: HLS/DASH, AES-128, retomada, recusa DRM
  media.py       parsing/análise de mídia (m3u8, MPD, DRM, nome, áudio)
  profiles.py    perfis + CommandTemplate (conceito YTDLnis)
  nametpl.py     motor de template de nome {title} [{id}].{ext}
  archive.py     download-archive (não rebaixar URLs concluídas)
  ytdlp.py       integração opcional com a CLI do yt-dlp (GPL-3.0, externa)
  ytdlpdl.py     downloader que delega ao yt-dlp (mesma interface)
  scheduler.py   pool global: N tarefas, semáforo, bucket, archive
  throttle.py    token bucket
  verify.py      SHA-256, checksums remotos, Authenticode
  clipboard.py   observador Win32 (ctypes)
  server.py      API HTTP local (+ /profiles, /media/analyze, /add auto)
  dashboard.py   TUI (rich)
  daemon.py      scheduler + clipboard + API
  browser_ext/   extensão MV3 (captura + Salvar como + perfil + API)
tests/           109 testes (engine, stream, mídia, DRM, yt-dlp, perfis, API)
```

## Testes

```bat
python -m unittest discover -s tests -v
```

Cobrem: engine de arquivo (segmentação/retomada/retry/mirrors/throttle),
engine de stream (master/media/AES/DASH/retomada/recusa DRM), parsing de
m3u8+MPD, detecção de DRM (HLS SESSION-KEY/KEY e DASH ContentProtection),
nome de arquivo (Content-Disposition/título/genérico), faixas de áudio,
integração yt-dlp (stub), **perfis + templates de nome + archive**,
**YtdlpDownloader** (stub), e a API (`/media/analyze`, `/add` kind=auto e
`/profiles`) de ponta a ponta.

## Limitações conhecidas

- Sem suporte a `sftp`/`ftp`, sem segmentação por `multipart/form-data`.
- Fila em JSON único (bom para uso local; sem trava entre processos).
- A extensão do navegador é um exemplo mínimo — não há auto-instalação.
- `verify` usa `Get-AuthenticodeSignature` (só Windows).
- HLS ao vivo (sem `#EXT-X-ENDLIST`) não é suportado.
- **DRM (Widevine/FairPlay/PlayReady) não é suportado de propósito** —
  apenas detectado e recusado (contornar viola DMCA §1201 / Lei 9.610).
- DASH `SegmentTemplate` assume período único e timeline contínua.
- Streams DRM/áudio+vídeo separados: baixa uma faixa por vez (sem mux —
  não há ffmpeg no ambiente).
