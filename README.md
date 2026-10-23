# minidm

Gerenciador de downloads em Python, escrito como exercício de engenharia —
inspirado nos conceitos observados em gerenciadores comerciais (fila de
downloads, retomada, segmentação multi-thread, integração com navegador),
porém com código próprio e sem qualquer mecanismo de licenciamento.

## Recursos (v2)

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

## Instalação

Requer Python 3.11+ e `requests` + `rich` (a interface usa `tkinter`, que já
vem com o Python):

```bat
python -m pip install requests rich
```

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

`browser_ext/` tem uma extensão MV3 de exemplo. Em `chrome://extensions`,
ative "Modo de desenvolvedor" → "Carregar descompactada" → aponte para a
pasta `browser_ext`. Com o `minidm daemon`/`serve` rodando, botão direito no
link → **Enviar para o minidm**.

## API local (127.0.0.1:9742)

```
GET  /status                  -> tarefas + config resumida
GET  /config                  -> configuração efetiva
POST /add      {url, filename?, dir?, mirrors?, sha256?, proxy?, cookie?, start?}
POST /start    {id|ids|all}
POST /pause    {id}
POST /remove   {id, delete?}
```

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
  cli.py         argparse + comandos (gui, add, daemon, tui, verify, …)
  config.py      AppConfig (arquivo + flags)
  meta.py        Task/Segment + persistência
  remote.py      sondagem do servidor
  downloader.py  engine: segmentos, retry, mirrors, throttle, verificação
  scheduler.py   pool global: N tarefas, semáforo de conexões, bucket
  throttle.py    token bucket
  verify.py      SHA-256, checksums remotos, Authenticode
  clipboard.py   observador Win32 (ctypes)
  server.py      API HTTP local
  dashboard.py   TUI (rich)
  daemon.py      scheduler + clipboard + API
  browser_ext/   extensão MV3 de exemplo
tests/           18 testes (engine, retry, mirrors, scheduler, throttle)
```

## Testes

```bat
python -m unittest discover -s tests -v
```

## Limitações conhecidas

- Sem suporte a `sftp`/`ftp`, sem segmentação por `multipart/form-data`.
- Fila em JSON único (bom para uso local; sem trava entre processos).
- A extensão do navegador é um exemplo mínimo — não há auto-instalação.
- `verify` usa `Get-AuthenticodeSignature` (só Windows).
