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
| **API ligada por padrão** | a GUI já abre com a API local ativa (`api_enabled`) — a extensão funciona sem clicar em nada; porta ocupada por outro minidm é só avisada, não quebra |
| **Tempo restante (ETA) de verdade** | usa a velocidade impressa pelo yt-dlp (`at 1.00MiB/s`) com fallback por diferença de amostras, suavizada p/ não dançar; ETA **total** na barra de status |
| **Barra de progresso viva** | barra em blocos (`████░░░`) por linha, linha colorida pelo estado, listras zebradas e barra **agregada** na base da janela |
| **GUI responsiva** | a snapshot da fila (lock de arquivo + disco) roda em **thread** — a janela nunca congela ao buscar, ordenar, abrir detalhes ou com outra instância disputando a fila; a UI só renderiza o cache mais novo |
| **Iniciar com o sistema (autostart)** | `minidm autostart on\|off\|status` ou menu **Arquivo → Iniciar com o sistema** — Windows: chave `Run` do registro (HKCU, sem admin); Linux: desktop entry `~/.config/autostart`; detecta registro de instalação antiga |
| **Captura de vídeo (HLS/DASH/direto)** | extensão detecta vídeos e lista qualidades (abaixo) |
| **Download de stream HLS/DASH** | `media <url>` — segmentos `.ts`/`.m4s` em paralelo, concatena e decifra AES-128 |
| **HLS ao vivo** (playlist sem `#EXT-X-ENDLIST`) | a engine re-busca a playlist no período do `#EXT-X-TARGETDURATION` até o `ENDLIST`/limite de duração; pausa/retoma salva; segmento que sai da janela vira `lost`, não erro |
| **Download FTP/SFTP** | `add ftp://…` / `add sftp://…` — mesma engine segmentada (retomada, retry, mirrors, throttle); FTP é nativo, SFTP precisa de `pip install paramiko` |
| **Torrents (magnet/.torrent)** | `add "magnet:?xt=…"` — delega ao **aria2 externo** (JSON-RPC local com token, só 127.0.0.1); `--seed-time=0`, pausa preserva o parcial, sem aria2 o `/add` recusa com a instrução de instalação |
| **Mux vídeo+áudio (ffmpeg)** | streams com faixa de áudio separada (DASH/HLS) baixam as duas pernas e o `ffmpeg -c copy` mescla — sem ffmpeg, segue com uma faixa; **embutido no APK** (FFmpeg 8.0 estático, ver `android/setup_ffmpeg.py`) |
| **Detecção de DRM** (Widevine/FairPlay/PlayReady) | analisa e **recusa** com aviso — nunca tenta contornar (DMCA §1201) |
| **Faixa de áudio separada** | HLS `EXT-X-MEDIA`/DASH áudio viram qualidade "áudio" (baixar música) |
| **Nome de arquivo legível** | `Content-Disposition` → URL → título da página (editável no popup) |
| **Pasta de destino escolhida** | popup e `/add` aceitam `dir`; a última pasta fica guardada |
| **Análise via yt-dlp** (opcional) | `media <url> --ytdlp` — cobre sites complexos (chama a CLI do yt-dlp) |
| **Perfis de download** (conceito do YT-DLnis) | `add --profile Música` — perfil define qualidade, formato, pasta e nome |
| **Escolha de qualidade/formato** | `add --height 720 --ext mp4` (ou o card da extensão, ou a **caixa de qualidades da webui** antes do Baixar) — teto de altura + container, com fallback p/ DASH-only |
| **Categorias estilo IDM** (pasta+perfil) | `--category Vídeos` — separa por pastas (Vídeos, Músicas, Programas…); base configurável (`download_base`), Vídeos é a padrão |
| **Templates de comando yt-dlp** | `CommandTemplate` com `url_regex` de ativação automática (`profiles` no CLI) |
| **Template de nome de arquivo** | motor `{title} [{id}].{ext}` com preview e sanitização (`nametpl.py`) |
| **Download-archive** (não rebaixar) | `config set download_archive true` — URLs concluídas são puladas |
| **`--concurrent-fragments`** | `config set concurrent_fragments 4` — fragmentos simultâneos por stream |
| **Downloader yt-dlp** (delegação) | `add --ytdlp` ou perfil `use_ytdlp_downloader` — mesma interface do scheduler |
| **Delegação automática** | link de **página** (YouTube, Instagram…) + yt-dlp instalado → usa yt-dlp **sozinho**, sem flag |
| **Cookies do navegador** | `add --cookies-from-browser chrome` — usa a sessão do usuário p/ sites com login; a engine própria recebe o mesmo cookie via jar exportado e cacheado (`cookies.py`) |
| **Caminho exato do arquivo** | `--print after_move` — o yt-dlp informa o arquivo gravado; fim da adivinhação por mtime no `_finalize` |
| **Playlists** | `add --playlist` (ou `/add` com `"playlist": true`) — expande em várias tarefas na fila (`--flat-playlist`, limite `playlist_limit`) |
| **Trecho do vídeo** | `add --section 01:00-02:00` — baixa só o intervalo (yt-dlp `--download-sections` + keyframes) |
| **SponsorBlock** | `config set sponsorblock all` (ou `add --sponsorblock all`) — remove trechos de patrocínio no pós-processamento |
| **Aviso de yt-dlp velho** | `deps` mostra a versão e avisa se tem >90 dias (sites mudam; como o próprio upstream) |
| **Descoberta de dependências** | `python -m minidm deps` — acha yt-dlp, ffmpeg e runtime JS (PATH/winget/scoop/choco) e mostra o que falta |
| **Runtime JS automático** | acha node/deno/bun e passa `--js-runtimes`; no APK usa o **quickjs-ng embutido** (`libqjs.so`) — extração completa do YouTube em qualquer plataforma |
| **Erros traduzidos** | mensagem do yt-dlp vira texto claro ("vídeo indisponível", "instale ffmpeg"…) |
| **Funciona sem ffmpeg** | sem ffmpeg, o download segue no melhor formato nativo (só faltam conversão/embed/mux) |

## Instalação

Requer Python 3.11+ e `requests` + `rich` (a interface usa `tkinter`, que já
vem com o Python). Para streams **criptografados (HLS AES-128)**, instale
também `cryptography`:

```bat
python -m pip install requests rich cryptography
```

Opcional (mostra o que falta com `python -m minidm deps`):

```bat
python -m pip install yt-dlp paramiko     # sites complexos + SFTP
winget install Gyan.FFmpeg                # mux, conversão, embed
```

## Captura de vídeos no navegador (estilo IDM)

O IDM detecta vídeos enquanto você navega e oferece download com as
qualidades disponíveis. O `minidm` faz o mesmo com a extensão `browser_ext/`:

1. **A API já vem ligada** — `serve`/`daemon` sobem a API sempre, e a
   **GUI abre com ela ativa** por padrão (`api_enabled`, campo em
   `config.json`; a checkbox **API do navegador** liga/desliga e grava a
   escolha). Fica em `http://127.0.0.1:9742`.
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

**Card "Baixar esta página"** — quando a aba atual é uma *página* (YouTube,
Instagram, um curso…), o popup mostra um card no topo com um seletor de
**categoria** (estilo IDM — Vídeos é a padrão; dá pra mudar para Músicas,
Programas…) e um seletor de **qualidade com as opções REAIS** do vídeo
(o servidor analisa com o yt-dlp e lista as alturas disponíveis + "Só
áudio"), mais o botão **Baixar**. Ele envia a URL da página (com
`category`/`height`) para o `/add`, que aplica a **pasta da categoria** e
**delega ao yt-dlp automaticamente** — é assim que a extensão baixa de
sites complexos, já separado por pastas, sem abrir a GUI.

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

### Transmissão ao vivo (HLS sem `#EXT-X-ENDLIST`)

Playlist sem `#EXT-X-ENDLIST` é **ao vivo** — não tem tamanho nem duração
ainda. O `media` sinaliza isso (`** AO VIVO **` e a qualidade "ao vivo") e
a engine:

- re-busca a playlist a cada `target_duration / 2` (entre 1 e 10s) e
  anexa os segmentos **novos** à fila — a janela anda sozinha, como num
  player;
- termina no `#EXT-X-ENDLIST` ou quando bate `live_max_seconds`
  (padrão 4h; `0` = sem limite, pare com `pause`);
- **pausa/retomada funcionam**: o progresso fica no sidecar `.meta.json`
  e a retomada continua dos segmentos pendentes + os novos;
- segmento que sai da janela ou responde 404 vira `lost` (contado em
  `stream["lost"]`) e **não falha a tarefa** — é o que um player tolera;
- a barra mostra `ao vivo` em vez de um `%` falso (o total só existe
  depois do `ENDLIST`);
- com mux (vídeo+áudio separados), as **duas pernas gravam ao mesmo
  tempo** — senão o áudio pegaria outra janela da transmissão.

```bat
python -m minidm config set live_poll_seconds 0    # 0 = automático (meio do targetduration)
python -m minidm config set live_max_seconds 3600  # 0 = sem limite
python -m minidm config set live_playlist_retries 20
```

### Download FTP e SFTP

O IDM baixa de FTP e o `minidm` também — com **toda a engine que já
existe**: segmentos em paralelo, `.part` pré-alocado, checkpoint,
retomada, retry com backoff, mirrors, throttle e SHA-256. Só a camada de
transporte muda:

| | FTP (`ftp://`) | SFTP (`sftp://`) |
|---|---|---|
| transporte | `ftplib` (nativo do Python) | `paramiko` (`pip install paramiko`) |
| faixa | `REST` + `RETR` | `open` + `seek` |
| probe | `SIZE` + `MDTM` | `stat` |
| sem `REST` | baixa em 1 segmento (sequencial) | sempre segmenta |
| credenciais | anônimo por padrão | agent/chaves locais por padrão |

```bat
minidm add ftp://usuario:senha@host:21/pasta/arquivo.zip
minidm add sftp://usuario@host/srv/dados.iso --segments 8
minidm add ftp://host/arquivo.iso            # anônimo (config muda isso)
```

Precedência de credenciais: `user:pass@` no URL >
`config set ftp_user …` / `sftp_user …` > anônimo (FTP) ou agent do
sistema (SFTP). No SFTP, a host key do servidor é **aceita e salva** no
`known_hosts` na primeira conexão (igual ao `ssh` com "yes"):

```bat
minidm config set sftp_accept_host_key false   # política estrita: chave nova = erro
minidm config set sftp_known_hosts "C:\cfg\known_hosts"
minidm config set sftp_key "C:\chaves\id_ed25519"
```

Falhas remotas (queda de conexão, `550`) herdam de `OSError` e caem no
**retry por segmento** com backoff — mesma resiliência do HTTP. Sem
tamanho (`SIZE`/`stat` negado) a engine **recusa com aviso claro**: sem
tamanho não há como segmentar nem retomar.

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

:: sobrepõe a qualidade/formato do perfil (teto de altura + container)
python -m minidm add "https://youtube.com/watch?v=…" --profile Vídeo --height 720
python -m minidm add "https://youtube.com/watch?v=…" --profile Vídeo --height 1080 --ext mp4

:: lista perfis e templates de comando
python -m minidm profiles
python -m minidm profiles --show Música
python -m minidm profiles --new Podcast     # cria um perfil novo (edite profiles.json)
```

A cadeia de seleção de formato (`build_video_selector`) prioriza o teto de
altura e usa fallbacks encadeados, para funcionar em vídeos **DASH-only**
(todos os formatos separados em faixa de vídeo e áudio — o `-f best` simples
falharia neles com "Requested format is not available"). Sem ffmpeg, as
opções que exigem mesclagem são descartadas.

O perfil é salvo em `<data_dir>/profiles.json` (editável à mão). Cada
perfil pode referenciar **templates de comando** — argumentos extras do
yt-dlp (ex.: `--embed-thumbnail`) ativáveis por perfil ou automaticamente
por `url_regex` (como o `CommandTemplate` do YTDLnis).

### Categorias (estilo IDM)

Igual o IDM separa os downloads em pastas por categoria, o `minidm`
tem um conjunto completo de categorias — cada uma é um par **pasta +
perfil**:

| Categoria | Pasta | Perfil |
|---|---|---|
| **Vídeos** (padrão) | `<base>/Vídeos` | Vídeo (vídeo) |
| **Músicas** | `<base>/Músicas` | Música (áudio → mp3) |
| **Programas** | `<base>/Programas` | Padrão |
| **Documentos** | `<base>/Documentos` | Padrão |
| **Imagens** | `<base>/Imagens` | Padrão |
| **Comprimidos** | `<base>/Comprimidos` | Padrão |

A **pasta base** é configurável (`download_base` em `config.json`; vazio
= `~/Downloads`). As subpastas são criadas sob demanda.

```bat
:: define a pasta base (estilo IDM: os seus downloads)
python -m minidm config set download_base "C:\Users\voce\Downloads"

:: baixa já na pasta da categoria (Vídeos é a padrão)
python -m minidm add "https://youtube.com/watch?v=…" --category Vídeos
python -m minidm add "https://site.com/musica.mp3" --category Músicas --height 720
```

A extensão mostra um seletor de **categoria** (Vídeos pré-selecionado,
dá pra trocar para Música/Áudio) e um de **qualidade com as opções reais**
do vídeo — o servidor analisa com o yt-dlp e lista as alturas disponíveis.

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
python -m minidm config set api_enabled false       # GUI sem API local (padrão: true)
```

### Delegar download ao yt-dlp

Com o yt-dlp instalado, o perfil ou `add --ytdlp` fazem o scheduler
delegar o download à CLI do yt-dlp (`YtdlpDownloader`), mantendo a mesma
interface de progresso/pausa. Sem yt-dlp, a engine própria (segmentada)
assume automaticamente.

Toda chamada de yt-dlp/análise roda com `CREATE_NO_WINDOW` (nada de CMD
piscando na detecção de vídeo ou download). E quando o **cookie do
navegador** faz o site devolver a página sem formatos (YouTube +
challenge: "só imagens" → `Requested format is not available`), o
minidm habilita o solver `--remote-components ejs:github` (cacheado pelo
yt-dlp) e, se ainda faltar, **repete automaticamente sem o cookie** — o
download segue em vez de morrer.

### Integrações herdadas do yt-dlp (conceitos, nunca código)

O yt-dlp é GPL/Unlicense — o minidm só **chama a CLI dele** e lê os
arquivos/JSON que ele emite. O que aproveitamos do projeto upstream:

**Cookies do navegador** (`--cookies-from-browser`) — para sites que
exigem login, o minidm usa a **sessão do próprio navegador do usuário**
(chrome, edge, firefox, brave, opera…):

```bat
python -m minidm config set cookies_from_browser chrome   # global
python -m minidm add "https://site.com/com/login" --cookies-from-browser edge
```

No yt-dlp (análise e download delegado) a flag vai direto. Na **engine
própria** (segmentada), o `cookies.py` exporta o jar do navegador uma vez
via CLI do yt-dlp, converte em header `Cookie:` e cacheia por 15 min em
`~/.minidm/cookies-<nav>.txt`. Só a sessão do usuário, local — nada de
contorno de DRM. O navegador vale **por tarefa** (`t.cookies_from_browser`
vem do payload/flag) com fallback na config — mesmo esquema de
`cookie`/`proxy`: o da tarefa vence o global.

**Caminho exato do arquivo** — o yt-dlp recebe `--print
after_move:MINIDM_PATH:%(filepath)s` e informa o arquivo que gravou;
o `_finalize` usa esse caminho direto (tamanho/hash certos), com o
heurístico antigo só como fallback. Acaba a confusão quando várias
tarefas dividem a mesma pasta.

**Playlists** — `--playlist` expande a URL em várias tarefas:

```bat
python -m minidm add "https://youtube.com/playlist?list=PL…" --playlist --start
```

Por baixo: `yt-dlp --flat-playlist -J` (extração rassem, rápida), com
limite `playlist_limit` (padrão 50). Na API: `{"url": …, "playlist": true}`.

**Trecho do vídeo** — baixar só um intervalo:

```bat
python -m minidm add "https://exemplo.com/aula.mp4" --section 01:00-02:00 --ytdlp
```

Vira `--download-sections *01:00-02:00 --force-keyframes-at-cuts` no
yt-dlp (o `*` corta em qualquer keyframe). Requer ffmpeg.

**SponsorBlock** — remove trechos de patrocínio no pós-processamento:

```bat
python -m minidm config set sponsorblock all      # ou: sponsor,selfpromo,…
python -m minidm add "https://youtu.be/…" --sponsorblock all
```

Vira `--sponsorblock-remove all` no yt-dlp (só com ffmpeg; sem ele a
flag é filtrada e o download segue normal).

**Fragmentos concorrentes** — `config set concurrent_fragments 4` agora
também chega ao yt-dlp como `--concurrent-fragments 4` quando o download
é delegado (a engine própria já usava).

**Aviso de versão velha** — `minidm deps` mostra a versão do yt-dlp e
avisa se tem mais de 90 dias: sites mudam toda semana, e o upstream
faz a mesma checagem. `python -m pip install -U yt-dlp` resolve.

## "Qualquer site, só o link" — como funciona de fábrica

O minidm decide sozinho quem resolve cada link:

| O link é… | Exemplo | Quem baixa |
|---|---|---|
| mídia direta (`.mp4`, `.m3u8`, `.mpd`, `.zip`…) | `cdn.ex…/filme.mp4` | **engine própria** (segmentada, retomada, mirrors) |
| **página** (sem extensão de mídia) | `youtube.com/watch?v=…`, `instagram.com/reel/…` | **yt-dlp** — automaticamente, sem precisar de flag |

Nos **executáveis e no APK o yt-dlp vem embutido** (frente 3): baixa um
link de página sem instalar nada. Na instalação **a partir do fonte**
(`pip install`), ele é opcional — instale uma vez (`python -m pip
install yt-dlp`) e a partir daí `python -m minidm add "<link do YouTube>"
--start` já baixa — o minidm detecta que é link de página e delega. O
nome sai com o **título real** (o yt-dlp informa), não o id do vídeo.

```bat
:: instala as duas peças (uma vez só)
python -m pip install yt-dlp
winget install Gyan.FFmpeg        :: opcional: mp3, embed, mux vídeo+áudio

:: confere o que está pronto
python -m minidm deps
```

### `minidm deps` — diagnóstico

`python -m minidm deps` mostra se o yt-dlp, o ffmpeg e o **paramiko**
(SFTP) foram encontrados
(o minidm **procura sozinho** no PATH, winget, scoop, chocolatey e
Program Files — você não precisa configurar caminho nenhum), o que cada
um destrava, e o comando de instalação do que faltar. Sem ffmpeg o
download **continua funcionando**; só ficam de fora conversão (mp3),
embed de miniatura/metadados e mux vídeo+áudio — o yt-dlp recebe os
argumentos já filtrados e entrega o melhor formato nativo. Sem paramiko
os downloads **FTP seguem funcionando** (`ftplib` é nativo do Python);
só falta o SFTP, que avisa "instale com: python -m pip install paramiko".

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
| Adicionar download | botão **+ Adicionar URL** (ou `Ctrl+N`) — URL, categoria (define a pasta), mirrors, SHA-256, segmentos |
| **Barra lateral de categorias** | clicar numa categoria (Todas, Vídeos, Músicas…) filtra a fila e mostra a contagem — como o IDM |
| Iniciar / Pausar / Remover | botões da barra (ou `Delete`) — pausar salva o progresso; os que exigem seleção ficam desligados sem seleção |
| **Ordenar por coluna** | clique no cabeçalho (ID, arquivo, estado, %, velocidade, ETA…) — 1º clique sobe, 2º desce, 3º volta à ordem da fila |
| Ver **tempo restante** | coluna **ETA** por tarefa (velocidade medida, suavizada) + **`restante HH:MM:SS`** e barra agregada na barra de status |
| **Visualizar mídia** | botão **▶ Visualizar** — player embutido (via ffmpeg), lista as qualidades (HLS/DASH) e deixa escolher; recusa DRM |
| Ver **propriedades ricas** | **duplo clique** na tarefa (página, origem, qualidade, categoria, perfil, velocidade, tempo restante, segmentos, hash, assinatura, erro) |
| Abrir pasta do arquivo | selecionar e **Abrir pasta** |
| Capturar links copiados | marcar **Área de transferência** |
| Aceitar links do navegador | **API do navegador** já vem marcada (ligada por padrão; a extensão manda pra `127.0.0.1:9742`) |
| **Menu de contexto** | **botão direito** na lista: iniciar, pausar, copiar URL, abrir pasta, subir/descer na fila, propriedades, remover, selecionar tudo |
| **Arrastar e soltar** | solte URLs/atalhos `.url` na janela (via `tkinterdnd2`) — sem ele, **Ctrl+V** faz a mesma coisa (mesmo parsing) |
| **Buscar na lista** | `Ctrl+F` → caixa na barra lateral filtra por arquivo, URL, título ou ID em tempo real (`Esc` limpa) |
| **Fila por arrasto** | arraste as linhas para escolher qual baixa primeiro (ou ↑/↓ no menu) — reordena quem é o próximo (`queue_pos`); desligado enquanto uma coluna estiver ordenada |
| **Tema claro/escuro** | botão **Tema** na barra (persiste em `theme`); ícones desenhados em tempo de execução (PIL) |
| **Onboarding** | na 1ª execução, janela com as dicas rápidas (uma vez; `onboarding_done`) |
| **Toasts + som** | cartão flutuante ao concluir/erro/novo download + "ding" (`notify_sound`) |
| **Detalhes ao vivo** | as propriedades abertas atualizam sozinhas (estado, velocidade, ETA, segmentos) a cada 500 ms |
| **Fechar para a bandeja** | fechar a janela **some para a bandeja do Windows** (fila e API seguem rodando); duplo clique no ícone reabre, botão direito → **Sair** encerra de verdade (`close_to_tray`) |
| Ajustar tudo | **Configurações…** — concorrência, banda, retries, proxy, cookies, verificação |

`minidm-gui-debug.bat` abre o mesmo com janela de terminal, para ver mensagens.

Via terminal (se preferir): `python -m minidm gui`

**Bandeja (Windows)** — com `close_to_tray` ligado (padrão), fechar a
janela não encerra: o ícone fica na bandeja do sistema e os downloads
continuam; reabre com duplo clique e sai pelo menu **Sair**. Em SO sem
bandeja (Linux/macOS) o comportamento é o de sempre. Desligue com
`minidm config set close_to_tray false`.

---

## executáveis (Windows, Linux, macOS, Android)

Sem Python instalado na máquina alvo? Gere binários com **PyInstaller**:

```bat
:: Windows
build\windows.bat
```

```bash
# Linux / macOS
bash build/unix.sh
```

Saída em `dist/`:

| Arquivo | O que é |
|---|---|
| `minidm-gui[.exe]` | interface gráfica (sem console, ícone próprio — duplo clique) |
| `minidm[.exe]` | CLI completa (terminal) |

A **CI no GitHub Actions** (`.github/workflows/build.yml`) roda os testes e
compila nos três sistemas a cada push — baixe os artefatos da aba *Actions*
sem buildar localmente. Cada runner compila o próprio alvo (sem
cross-compile).

### Releases (pacote por sistema)

Para publicar uma versão com os binários anexados (estilo repos que já vêm
buildados), é só mandar uma **tag**:

```bat
git tag v1.1.0
git push origin v1.1.0
```

A CI roda testes + build nos três sistemas e cria uma **GitHub Release**
(`softprops/action-gh-release`) com:

| Pacote | Sistema |
|---|---|
| `minidm-win64.zip` | Windows x64 |
| `minidm-linux-x86_64.tar.gz` | Linux x86_64 |
| `minidm-macos-arm64.tar.gz` | macOS Apple Silicon |

Cada pacote leva o **bundle completo** (`minidm/` + `minidm-gui/` — o
`.exe` solto não roda sem o `_internal/` ao lado). Quem baixa extrai e
roda, sem Python na máquina. Em push normal (sem tag) só roda
teste+build, sem release.

Notas do empacotamento (`packaging/`):

* **onedir** (não onefile): inicia mais rápido e antivírus reclama menos;
* **yt-dlp EMBUTIDO no zip** (frente 3, Python puro — licença Unlicense/
  GPLv2, uso como biblioteca permitido; a CLI roda in-process quando não
  há binário externo) — links de página funcionam sem instalar nada;
  **ffmpeg continua externo** (grande demais p/ embutir; sem ele a engine
  própria segue funcionando para arquivos diretos e formatos
  progressivos) — `minidm deps` mostra o que falta;
* `browser_ext/` vai dentro do bundle (a API local serve a extensão);
* ícone gerado por código: `python packaging/make_icon.py` (PIL, sem asset);
* dependências: `pip install -r packaging/requirements.txt`.

### Android (APK + Termux)

O Android tem dois caminhos:

**APK (`android/`, Chaquopy + WebView)** — a engine Python roda embutida
no app (Chaquopy 17.2, Python 3.11, minSdk 24) e a interface é o próprio
**`webui/`** servido pela API local, exibido num WebView:

* baixe o `minidm-android.apk` da **GitHub Release** (ou do artefato
  `minidm-android` da aba *Actions*) e instale;
* os arquivos concluídos aparecem em **`Download/<categoria>/` da pasta
  pública do Android** (visíveis no app *Downloads* do sistema e em file
  managers): a engine grava no armazenamento interno e o arquivo é movido
  via **MediaStore** (API 29+, sem permissão) ou escrita direta +
  `WRITE_EXTERNAL_STORAGE` (API 24–28, pedida na primeira abertura);
  desligar: `android_public_downloads false`; a fila fica em `~/.minidm`
  (HOME do app); o card da webui mostra o **destino publicado**
  (`Download/…`, caminho completo no tooltip);
* **nomes de arquivo**: `Content-Disposition` → nome com extensão no
  link → **título da página** (o `<title>` extraído na análise) → o
  segmento do URL — nunca mais o id cru do YouTube (`5dWeeUIZFgA`);
* **downloads nunca concluem vazios**: servidor sem tamanho (o YouTube
  responde `Content-Length: 0` no HEAD) vira **erro claro** e arquivos
  de 0 bytes não são publicados no MediaStore;
* **links de página (YouTube, Instagram…) funcionam sem instalar
  nada** — o yt-dlp é **embutido no APK** (frente 3: pip do Chaquopy,
  rodando in-process); colar um link sem extensão (ex.: `youtu.be/ID`)
  é analisado e baixado direto, estilo ADM. O **runtime JS do YouTube**
  também vem embutido: `android/setup_qjs.py` baixa o quickjs-ng
  estático como `libqjs.so` nos `jniLibs/` (extraído pelo instalador
  para o `nativeLibraryDir` — único local executável no Android 10+)
  e o `minidm/deps.py` o repassa ao yt-dlp como `--js-runtimes
  quickjs:<caminho>`, junto do pacote `yt-dlp-ejs` (solver EJS).
  Sem ffmpeg o yt-dlp ainda
  entrega os formatos progressivos (vídeo+áudio juntos); o Termux
  abaixo continua disponível para quem quer a CLI completa;
* a mesma webui serve de **controle remoto** no desktop: com o daemon
  rodando, abra `http://127.0.0.1:9742/` no navegador;
* **segundo plano**: ao sair do app a fila continua com um **serviço
  em primeiro plano** (`DownloadService`, tipo `dataSync`) —
  notificação de progresso (toque reabre o app), `WifiLock` +
  `PARTIAL_WAKE_LOCK` para a tela desligada não cortar a transferência;
  o serviço **para sozinho quando a fila esvazia** (sem notificação
  fantasma) e o sistema pode recriá-lo (`START_STICKY`);
* **compartilhar para o miniDM**: o menu *Compartilhar* de qualquer
  app (navegador, YouTube, WhatsApp…) oferece o miniDM para texto
  (`ACTION_SEND`) — a URL é extraída, entra na fila via mesma API da
  webui e um Toast confirma (com o app em segundo plano o `onNewIntent`
  processa direto; o Python faz retry enquanto o daemon ainda sobe);
* build local: `python android/setup_qjs.py` (runtime JS) e depois
  `gradle -p android assembleDebug` (a CI — job `apk` em
  `.github/workflows/build.yml` — já faz os dois a cada push); detalhes em
  [`packaging/android/README.md`](packaging/android/README.md).

**Termux** — CLI + fila + daemon completos no celular (sem interface
gráfica); instale o yt-dlp (`pip install yt-dlp`) e os links de página
(YouTube…) passam a funcionar também pelo celular; ver
[`packaging/android/README.md`](packaging/android/README.md).

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

`browser_ext/` é uma extensão MV3. O `minidm` ajuda a instalar de duas
formas (`minidm ext …`) — híbrido, porque o Chrome barra CRX local e não abre
`chrome://` por linha de comando:

```
python -m minidm ext status      # onde a extensão já está (Chrome/Edge, por perfil)
python -m minidm ext guided      # copia a pasta e orienta o "Carregar expandida"
python -m minidm ext install     # tenta automático: política HKCU + CRX3 na API
python -m minidm ext uninstall   # remove a política escrita por `install`
```

- **`guided` (garantido)** — copia o caminho de `browser_ext/` para a área de
  transferência e abre o Explorador. Em `chrome://extensions`, ative **Modo de
  desenvolvedor** → **Carregar expandida** → Ctrl+V. É o caminho que funciona
  sempre (é como a extensão está carregada hoje).
- **`install` (automático)** — gera um pacote **CRX3 assinado** (RSA-PSS, id
  estável por chave), hospeda `update.xml` + `.crx` na API local e escreve a
  política `ExtensionInstallForcelist` no HKCU (sem admin). Requer
  `minidm daemon`/`serve` rodando para o navegador baixar o CRX; a extensão
  entra como "gerenciada" (remova com `ext uninstall`).

Com o `minidm daemon`/`serve` rodando (ou **API do navegador** marcado na
GUI), a extensão captura vídeos da navegação e o painel (ícone) mostra as
qualidades. Botão direito → **Enviar para o minidm**.

## API local (127.0.0.1:9742)

```
GET  /status                  -> tarefas + config resumida
GET  /                        -> webui (UI no navegador/WebView do APK)
GET  /webui/<arquivo>         -> assets da webui (style.css, app.js, …)
GET  /config                  -> configuração efetiva
GET  /profiles                -> perfis + templates de comando (popup)
POST /add      {url, filename?, dir?, kind?, referer?, page_title?,
                quality?|quality_url?, profile?, ytdlp?, mirrors?, sha256?,
                proxy?, cookie?, user_agent?, start?,
                playlist?, limit?,                # yt-dlp: expande playlist
                section?,                         # yt-dlp: trecho INI-FIM
                sponsorblock?,                    # yt-dlp: all/sponsor,…
                cookies_from_browser?}            # yt-dlp: chrome, edge,…
                # playlist=true -> {ok, count, ids:[…]} (uma resposta só)
POST /start    {id|ids|all}
POST /pause    {id}
POST /remove   {id, delete?}
POST /media/analyze {url, referer?, page_title?, cookie?, user_agent?,
                     proxy?, ytdlp?}
                   -> {ok, kind, qualities:[{label,url,bandwidth,
                       resolution,height,estimated_size,audio_only}],
                       duration, encrypted, drm, filename}
                   -> {ok:false, error, drm?}   # DRM recusado com aviso
GET  /update.xml              -> manifest da extensão (política forcelist)
GET  /minidm.crx              -> pacote CRX3 assinado da extensão
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
ROADMAP.md             backlog (APK Android, Rust) + o que já foi feito
docs/analise-idm.md    análise reversa do IDM que embasa este projeto
build/
  windows.bat          build Windows (PyInstaller -> dist/)
  unix.sh              build Linux/macOS
packaging/
  minidm.spec          spec PyInstaller (GUI windowed + CLI console)
  requirements.txt     dependências de runtime + build
  make_icon.py         gera minidm.ico/.png com PIL (sem asset)
  android/README.md    minidm no Android via Termux (GUI: ver backlog)
.github/workflows/
  build.yml            CI: testes + executáveis em Win/Linux/macOS
minidm/
  gui.py         interface gráfica (tkinter, tema claro/escuro, menu de contexto, busca, DnD, fila por arrasto, toasts, onboarding, sidebar, ordenação, ETA/barras ao vivo, visualizador, API ligada por padrão, roda o scheduler em processo)
  theme.py       paletas de tema (LIGHT/DARK) + resolve/next_theme
  dnd.py         drop/colagem de URLs (parsing puro, tkinterdnd2 opcional)
  icons.py       ícones da toolbar desenhados com PIL em runtime
  player.py      visualizador de mídia embutido (ffmpeg -> rawvideo em tk.Canvas)
  cli.py         argparse + comandos (gui, add, media, profiles, daemon, …)
  config.py      AppConfig (arquivo + flags; theme/notify_sound/onboarding_done)
  meta.py        Task/Segment + persistência (kind/referer/stream/profile/category/section/queue_pos)
  store.py       fila JSON multi-processo (trava de arquivo, merge, tombstones, reorder)
  categories.py  categorias estilo IDM (pasta + perfil por tipo de arquivo)
  remote.py      sondagem do servidor
  downloader.py  engine de arquivo: segmentos, retry, mirrors, throttle
  ftpdl.py       downloads FTP/SFTP (mesma engine segmentada; ftplib + paramiko)
  stream.py      engine de stream: HLS/DASH, AES-128, ao vivo (re-fetch até ENDLIST), retomada, recusa DRM, mux (MuxDownloader)
  media.py       parsing/análise de mídia (m3u8, MPD, DRM, nome, áudio)
  profiles.py    perfis + CommandTemplate (conceito YTDLnis)
  nametpl.py     motor de template de nome {title} [{id}].{ext}
  archive.py     download-archive (não rebaixar URLs concluídas)
  ytdlp.py       integração com o yt-dlp (Unlicense/GPLv2; embutido no APK e nos executáveis, externo no fonte: describe, playlist, versão; CLI in-process quando não há binário)
  ytdlpdl.py     downloader que delega ao yt-dlp (mesma interface)
  cookies.py     cookies do navegador do usuário (export via yt-dlp -> jar -> header p/ engine própria)
  deps.py        descoberta de yt-dlp/ffmpeg/paramiko/runtime JS (PATH/winget/scoop/choco) + `deps`
  scheduler.py   pool global: N tarefas, semáforo, bucket, archive
  throttle.py    token bucket
  verify.py      SHA-256, checksums remotos, Authenticode
  clipboard.py   observador Win32 (ctypes)
  server.py      API HTTP local (+ /profiles, /media/analyze, /add auto)
  dashboard.py   TUI (rich)
  daemon.py      scheduler + clipboard + API
  browser_ext/   extensão MV3 (captura + Salvar como + perfil + API)
tests/           508 testes (engine, stream, mídia, DRM, yt-dlp (CLI + runner + downloader in-process), integrações yt-dlp A–F, perfis, categorias, deps (incl. runtime JS e ffmpeg do APK), fila multi-processo + reorder, mux, HLS ao vivo, FTP/SFTP, torrent (aria2 RPC), API, GUI/ETA/progresso/tema/DnD/player/snapshot em thread, extensão do navegador, autostart (registro/XDG), share ACTION_SEND, webui (qualidade/destino/busca), Android/tray/pub)
e2e_mux_real.py  E2E real do mux (ffmpeg gera a mídia e mescla)
e2e_live_real.py E2E real do HLS ao vivo (playlist que cresce + mux ao vivo)
e2e_ftp_real.py  E2E real de FTP/SFTP (scheduler -> engine remota -> retry 550)
```

## Testes

```bat
python -m unittest discover -s tests -v
python e2e_mux_real.py        # E2E real: mídia gerada + ffmpeg mesclando
python e2e_live_real.py       # E2E real: playlist AO VIVO + mux ao vivo
python e2e_ftp_real.py        # E2E real: FTP/SFTP + retry 550 pelo scheduler
```

Cobrem: engine de arquivo (segmentação/retomada/retry/mirrors/throttle),
engine de stream (master/media/AES/DASH/retomada/recusa DRM), **HLS ao
vivo** (re-fetch até o ENDLIST, janela que anda, 404 → `lost`,
pausa+retomada, limite de duração, pernas de mux concorrentes e a
exibição "ao vivo"), parsing de
m3u8+MPD, detecção de DRM (HLS SESSION-KEY/KEY e DASH ContentProtection),
nome de arquivo (Content-Disposition/título/genérico), faixas de áudio,
integração yt-dlp (stub), **perfis + templates de nome + archive**,
**YtdlpDownloader** (stub), **descoberta de deps + filtro ffmpeg +
link direto × página**, **FTP/SFTP** (servidor FTP mínimo em Python puro
que registra os offsets de `REST` + servidor SFTP em processo com paramiko:
segmentação, retomada, retry de 550, credenciais URL > config > anônimo,
`known_hosts` estrita × auto-add, paramiko ausente) e a API
(`/media/analyze`, `/add` kind=auto e
`/profiles`) de ponta a ponta.

## Limitações conhecidas

- Sem segmentação por `multipart/form-data` (upload).
- Fila em JSON único com **trava entre processos**: escrita atômica sob
  lock exclusivo (`tasks.json.lock`), merge de criações e tombstones de
  remoção — GUI + CLI podem abrir a mesma fila ao mesmo tempo. Mutação do
  mesmo campo segue "último escritor vence" (com o daemon rodando, prefira
  a API: `/pause`, `/remove`).
- A extensão do navegador usa instalação **híbrida** (limitação superada, ver
  `minidm ext`): `minidm ext guided` copia a pasta e orienta o "Carregar
  expandida" (caminho garantido), e `minidm ext install` tenta automação via
  política `ExtensionInstallForcelist` (HKCU, sem admin) + CRX3 auto-hospedado
  na API. O Chrome barra CRX local e ignora `chrome://` na CLI, por isso o
  modo guiado continua sendo o garantido.
- `verify` usa `Get-AuthenticodeSignature` (só Windows).
- **DRM (Widevine/FairPlay/PlayReady) não é suportado de propósito** —
  apenas detectado e recusado (contornar viola DMCA §1201 / Lei 9.610).
- DASH `SegmentTemplate` assume período único e timeline contínua.
- A **sidebar de categorias agrupa pela extensão** do arquivo
  (`categories.effective_category`): tarefas antigas (criadas antes do campo
  `category`) e as vindas da CLI/API/extensão sem categoria explícita são
  deduzidas sob demanda, sem reescrever a fila. Extensão desconhecida cai
  fora de qualquer categoria (visível só em "Todas").

Limitações **superadas** nesta versão (antes eram limitações):

- ~~"sem suporte a `ftp`/`sftp`"~~ → a engine própria agora baixa dos
  dois protocolos (`minidm/ftpdl.py`), reaproveitando **tudo** o que a
  engine de HTTP já faz: segmentação, `.part` pré-alocado, checkpoint,
  retomada (validada por tamanho + mtime), retry, mirrors, throttle e
  verify. Só a camada de transporte muda: FTP usa `REST` + `RETR`
  (`ftplib`, sem dependência), SFTP usa `open` + `seek` (`paramiko`,
  instalado com `pip install paramiko` — `minidm deps` mostra se falta).
  Credenciais: `user:pass@` no URL > config (`ftp_user`…/`sftp_user`…) >
  anônimo (FTP) ou agent/chaves locais (SFTP); a host key do SSH é aceita
  e salva no `known_hosts` na primeira conexão (use
  `sftp_accept_host_key = false` para política estrita).
- ~~Fila JSON sem trava entre processos~~ → trava de arquivo + merge +
  tombstones (acima, mantida na lista só pelo detalhe do "último
  escritor vence").
- ~~"baixa uma faixa por vez (sem mux)"~~ → a engine própria agora
  **mescla vídeo+áudio com `ffmpeg -c copy`** (`stream.MuxDownloader`):
  a análise pareia a melhor faixa de áudio (DASH `AdaptationSet`
  separado / HLS `EXT-X-MEDIA`), as duas pernas são baixadas com
  retomada própria e o ffmpeg junta. Sem ffmpeg na máquina, cai no
  download de uma faixa (como antes) em vez de quebrar.
- ~~"HLS ao vivo (sem `#EXT-X-ENDLIST`) não é suportado"~~ → a engine
  **re-busca a playlist** até o `ENDLIST`/limite de duração, com
  pausa+retomada, `lost` tolerado para segmento da janela e pernas de mux
  concorrentes (acima, "Transmissão ao vivo").
