# Análise reversa estática — Internet Download Manager 6.42.63.3

> **Escopo deste documento:** análise estática e arquitetura, feita com o
> MCP **REA** (`rea-agents@6.7.0`) usando o **Ghidra 12.1.4** como provider.
> Nada aqui foi usado para criar, aprimorar ou validar bypass de licenciamento.
> A finalidade é embasamento técnico: entender a arquitetura de um
> gerenciador de downloads maduro — e é exatamente disso que o `minidm`
> (este projeto) se inspira, com código próprio e legítimo.
>
> **Legenda:** **[O]** observado no binário · **[I]** inferência ·
> **[D]** desconhecido / não verificado.

---

## 1. Identificação do alvo

| Campo | Valor |
|---|---|
| Arquivo | `IDMan.exe` (PE32 x86, application) |
| Versão | 6.42.63.3 |
| Tamanho | 6.054.768 bytes |
| SHA-256 | `27ca6f93b7c9fa368012f66ed5a9aa3916cb523a5562ec3d8ee289029858cf63` |
| Image base | `0x400000` |
| PDB referenciado | `IDMan.pdb` (GUID `c8a69b1c-9f16-488b-9013-5740907ff52c`) — **não carregado**, daí os nomes `FUN_*` |
| Provider | Ghidra 12.1.4 via REA |

Evidence base da visão geral: `ev_d0acd4a094becd07ed55d45941b9df87924cf56e7c275c2ea3cd2f7a89bb3ae1`

### Segmentos e escala

- `.text`  `0x401000–0x67b600`
- `.rdata` `0x67c000–0x757400`
- `.data`  `0x758000–0x76b55c`
- `.rsrc`  `0x76c000–0x999800` (~2,2 MB — diálogos, ícones, traduções, HTML)
- **18.828 procedimentos** (~14.200 internos), **7.007 strings**
- Toolchain: **MSVC + ATL** (`ATL::CSimpleStringT`), MFC/Win32 dialogs,
  CRT com cookie de segurança (GS) e SEH C++ (`Catch@...`)

---

## 2. Arquitetura em camadas do pacote

**[O]** Componentes observados no diretório de instalação:

```
┌─────────────────────────────────────────────────────────────┐
│ IDMan.exe        UI + engine de downloads + fila            │
│ idmBroker.exe    broker de processos/IPC                    │
│ IEMonitor.exe    monitor de navegação (captura links)       │
│ IDMNetMon.dll    monitor de rede                            │
│ idmtdi64.sys     driver TDI  (interceptação de conexões)    │
│ idmwfp64.sys     driver WFP  (Windows Filtering Platform)   │
│ idmmzcc.crx      extensão Chrome/Edge                       │
│ idmmz.xpi        extensão Firefox                           │
└─────────────────────────────────────────────────────────────┘
```

- **[O]** Drivers coordenados por `SYSTEM\CurrentControlSet\Services\IDMTDI`
  e `...\IDMWFP` (`0x6804f8`, `0x680524`), dispositivos `\\.\IDMTDI` /
  `\\.\IDMWFP` (`0x680550`, `0x68055c`), com rotinas `Start`/`Delete`
  (`FUN_0040f9e0`, `FUN_004161a0`).
- **[O]** Integração com navegadores via `%s\components12\idmmzcc.dll`,
  `%s\chrome\idmmzcc.jar`, `%s\components2\idmcchandler2.dll`
  (`0x681650–0x6816ac`).
- **[O]** Instalação de LSP/Winsock provider: `WSCInstallProvider`,
  `WSCWriteProviderOrder`, GUIDs próprias (`FUN_0040e4d0`).
- **[O]** IPC por eventos nomeados: `Local\IDMEventMonitor` (`0x688858`).
- **[I]** Fluxo: kernel (TDI/WFP) → monitor (`IEMonitor`/`IDMNetMon`) →
  broker (`idmBroker`) → aplicação (`IDMan`), com IPC por eventos.

> **Lição para o `minidm`:** a separação "captura de link" / "motor de
> download" / "persistência" é a mesma que adotamos com `clipboard.py` +
  `server.py` / `downloader.py` / `store.py`.

---

## 3. Persistência e configuração

- **[O]** Chave principal: **`HKCU|HKLM\SOFTWARE\Internet Download Manager`**
  (`0x67de60`), acessada por 9 funções. `DAT_00765c48` guarda o `HKEY`
  aberto globalmente.
- **[O]** Valores identificados: `MData`, `Version`, `Version32`, `nExpDt`,
  `nLst`, `bshexmsg`, `ptrk_scdt`, `AdvIntDriverEnabled2`, `lastintres`, e
  dois nomes dinâmicos apontados por `DAT_00767560`/`DAT_00767564`
  (**[I]** quase certamente "FName"/e-mail usados no diálogo About).
- **[O]** Auto-inicialização via `...\CurrentVersion\Run`.
- **[O]** Espelho de dados em
  `HKCU\Software\Classes\CLSID\{E6871B76-C3C8-44DD-B947-ABFFE144860D}`.

> **Lição para o `minidm`:** estado fora do binário (aqui registro, no nosso
> caso `~/.minidm/*.json`) é o que permite retomar e inspecionar. A ideia de
> "espelho"/fallback também aparece no nosso `.meta.json` ao lado do `.part`.

---

## 4. Ciclo de licença/trial (análise arquitetural)

> Registrado apenas para documentar o desenho de estado — **não** para
> reprodução.

### 4.1 Manutenção do estado de trial — `FUN_004664d0` (2.061 B)

Evidence `ev_d1c9b9b993720ff8c1bb823eeb3a09cb09a146a2daca3823e939f3eb2b901b04`

- **[O]** Lê `MData` (blob), `Version`/`Version32`; sem `MData`, tenta o
  espelho na CLSID.
- **[O]** Blob decodificado por `FUN_004479d0` com chave global `DAT_00766a2c`
  (**[I]** XOR/obfuscação simples, chave estática).
- **[O]** Conversão de tempos com divisor **`0x15180` = 86.400 s = 1 dia**.
- **[O]** Vencido → apaga `MData` (em ambas as chaves).
- **[O]** 0–5 dias antes do fim → grava `bshexmsg=1` e posta mensagem `0x146c`
  com os dias restantes.
- **[O]** Deriva/grava `nExpDt` (REG_DWORD) quando ausente.

### 4.2 Aplicação da resposta do servidor — `FUN_0051fc50` (4.238 B)

Evidence `ev_135a54d83da0b6dbcbf91e9f0f157f8480ecb2f62e9c476b43a6382aa722e1cd`

- **[O]** Monta host `"%s.%s%s%s"` com `register` + domínio; envia
  `IDM-DataType-a1:%s`, paths `/data/395012712/`, `answer`.
- **[O]** Resposta: termina em `L` → vitalícia (apaga `nExpDt`, grava
  **`nLst=1`**); senão `sscanf("%lu")` → **`nExpDt`** e apaga `nLst`.
- **[O]** Campo seguinte grava `ptrk_scdt = agora + offset`
  (**[I]** marca de verificação/renovação periódica).

### 4.3 Diálogo "Sobre" — `FUN_00489f50` (1.300 B)

Evidence `ev_918d72a78b8cb5c3ccc34024b1241048ee1128433c22be0d3d31ae2e2a29c3f0`

- **[O]** Lê os dois valores dinâmicos, exibe "This product is licensed to:";
  `nLst==1` → "This is a lifetime license"; senão formata `nExpDt` com `%b/%d/%Y`.
- **[O]** Strings-âncora no `.rdata`: `CAboutDlg::OnInitDialog()` (`0x67de14`),
  `nLst` (`0x67de40`), `nExpDt` (`0x67de54`).

### 4.4 Dispatcher de mensagens — `FUN_004ec350` (14.171 B)

Evidence `ev_671f721f882a12f00e0c3f209f051983fbb134fdb8313da44d8664e2a1a64580`

- **[O]** Referencia **todas** as strings de erro de licença (879 strings no
  total; é um dispatcher de UI, **não** a lógica de decisão).

> **Lição para o `minidm`:** o IDM separa "decidir o estado" (`FUN_004664d0`)
> de "mostrar o estado" (`FUN_004ec350`). Fazemos o mesmo: `downloader.py`
> decide, `dashboard.py`/`cli.py` mostram.

---

## 5. Atualização e contato com o servidor — `FUN_0042dc60` (1.858 B)

Evidence `ev_bc9030e1bb1de4aa60225de275690990ea4f51ed5dfadb7b78809e17df4c0c3e`

- **[O]** `www.internetdownloadmanager.com` / `secure%s`; paths `update.cgi`,
  `/a/dc/`, `/data/fv/`, `/data/729418032/`; parâmetro `tcidm`; constante
  `hsIWo2k8DSl24qsx` (**[I]** nonce/identificador do canal de atualização).
- **[O]** Baixa e executa `idmupdt.exe`/`idmupdt2.exe` via `%s.del`
  (**[I]** substituição atômica); escreve em `AppCompatFlags\Layers`.

> **Lição para o `minidm`:** o `remote.py` faz a "negociação" equivalente
> (HEAD/Range, `ETag`), e o `verify.py` checa a origem do que chega — aqui
> por hash publicado e Authenticode, não por canal proprietário.

---

## 6. Desconhecidos e limitações

- **[D]** PDB não carregado → sem nomes reais de classes/métodos; o mapeamento
  é por strings + xrefs.
- **[D]** Algoritmo exato de validação local de série não isolado; as mensagens
> "counterfeit"/"blocked" só têm xref dentro do dispatcher `FUN_004ec350`,
> sugerindo verificação delegada ao servidor (`FUN_0051fc50`).
- **[D]** Conteúdo do blob `MData` e da chave `DAT_00766a2c` vistos em uso,
  não extraídos.
- **[D]** Análise 100% estática: nenhum comportamento observado em execução.
- **Limitações do REA:** funções muito grandes estouram o orçamento de
  resposta MCP (ler via `inspect_analysis_view` com `source.kind =
  "retained-evidence"` e paginação).

---

## 7. Mapa de endereços útil (referência rápida)

| Endereço | O que é |
|---|---|
| `0x40e4d0` | LSP/Winsock provider (instala GUIDs) |
| `0x40f9e0`, `0x4161a0` | serviços IDMTDI / IDMWFP (`Start`) |
| `0x42dc60` | verificação de atualização / download de patch |
| `0x44c9c0` | rotina de configurações (proxy, `Run`, `idmvers`, `MData`) |
| `0x4664d0` | **manutenção do estado de trial** |
| `0x489d80` | gatilho de "renovar" ao checar `nExpDt` |
| `0x489f50` | diálogo About/Licença |
| `0x4ec350` | dispatcher central de mensagens/UI (879 strings) |
| `0x51fc50` | registro online / aplicação da resposta do servidor |
| `0x67de54` / `0x67de60` | strings `nExpDt` / chave do registro |

---

## 8. Tradução em decisões de projeto do `minidm`

| Conceito observado no IDM | Implementação no `minidm` |
|---|---|
| Segmentação por `Range` | `downloader.py` → `plan_segments` + `_stream_segment` |
| Retomada validada (`ETag`/tamanho) | `remote.py` → `still_valid` + `.meta.json` |
| Persistência fora do binário | `store.py` (`tasks.json`) + `.meta.json` |
| Decidir vs. mostrar | `downloader`/`scheduler` vs. `dashboard`/`cli` |
| Integração com navegador | `server.py` (API local) + `browser_ext/` |
| Captura de navegação | `clipboard.py` (observador Win32) |
| Verificação de origem | `verify.py` (hash publicado + Authenticode) |
| Resiliência de rede | retry/backoff + mirrors + throttle |

---

*Documento gerado a partir da sessão REA/Ghidra. Para reproduzir: registrar o
MCP `rea` com `GHIDRA_INSTALL_DIR` apontando para o Ghidra 12.1.x, abrir o
binário com `open_binary` e usar `binary_overview`, `search_strings`,
`xrefs`, `analyze_function` e `inspect_analysis_view`.*
