"""Configuração do minidm: padrões + `~/.minidm/config.json` + flags da CLI."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, fields

DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".minidm")


@dataclass
class AppConfig:
    # concorrência
    max_concurrent: int = 3        # downloads simultâneos
    max_connections: int = 16      # segmentos/aberturas HTTP simultâneas no total
    segments: int = 0              # 0 = automático por tamanho
    # resiliência
    retries: int = 5               # tentativas por segmento
    backoff_base: float = 1.0      # segundos; dobra a cada falha
    backoff_max: float = 60.0
    # banda
    throttle_bps: int = 0          # 0 = sem limite (bytes/segundo, global)
    # streams (HLS/DASH)
    concurrent_fragments: int = 0  # 0 = usar max_connections; >0 = fragmentos
                                   # simultâneos por stream (como --concurrent-fragments)
    # HLS AO VIVO (playlist sem #EXT-X-ENDLIST): o segmento chega junto com
    # o tempo, então a engine rebusca a playlist até o ENDLIST aparecer.
    live_poll_seconds: float = 0.0   # 0 = automático (target_duration/2,
                                     # entre 1 e 10s)
    live_max_seconds: float = 14400.0  # duração máxima do gravo (0 = sem
                                       # limite) — evita tarefa infinita
    live_playlist_retries: int = 20  # falhas seguidas na playlist antes de
                                     # abortar (segmento perdido é tolerado)
    # FTP/SFTP (credenciais padrão; `user:pass@` no URL tem precedência)
    ftp_user: str = ""               # vazio = anônimo
    ftp_password: str = ""
    sftp_user: str = ""              # vazio = agent/chaves locais do SO
    sftp_password: str = ""
    sftp_key: str = ""               # caminho da chave privada (~ expandido)
    sftp_known_hosts: str = ""       # vazio = ~/.ssh/known_hosts
    sftp_accept_host_key: bool = True  # 1ª conexão aceita e salva a chave;
                                       # False = estrita (chave nova = erro)
    # yt-dlp (CLI externa, opcional)
    use_ytdlp_downloader: bool = False  # delegar download ao yt-dlp quando
                                        # disponível (perfis podem forçar)
    download_archive: bool = False      # não rebaixar URLs já concluídas
    # cookies do NAVEGADOR do usuário (conceito --cookies-from-browser do
    # yt-dlp): "chrome"/"edge"/"firefox"/"brave"/"opera" = usar a sessão do
    # usuário p/ sites que exigem login. Vazio = desligado. Só a sessão do
    # próprio usuário — nada de contorno de DRM.
    cookies_from_browser: str = ""
    # playlists: nº máx. de itens expandidos por URL de playlist
    playlist_limit: int = 50
    # SponsorBlock: "" = desligado; "all" ou lista ("sponsor,selfpromo,…")
    # remove trechos de patrocínio no pós-processamento (yt-dlp + ffmpeg)
    sponsorblock: str = ""
    # rede
    user_agent: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "minidm/2.0")
    proxy: str = ""                # ex.: http://host:8080 ou socks5://host:1080
    cookie: str = ""               # header Cookie completo
    # integração
    api_port: int = 9742
    # API local ligada ao abrir a GUI (extensão do navegador funciona sem
    # precisar marcar nada). `daemon`/`serve` sempre ligam; aqui vale p/ a
    # interface gráfica. A checkbox da toolbar altera e persiste este campo.
    api_enabled: bool = True
    clipboard_interval: float = 2.0
    # interface
    theme: str = "light"             # "light" | "dark" (botão da toolbar)
    notify_sound: bool = True        # "ding" ao concluir (Windows)
    onboarding_done: bool = False    # tela de boas-vindas já mostrada
    # fechar a janela Some para a bandeja (tray) em vez de sair — como o
    # IDM. Sair de verdade: menu Arquivo → Sair ou o "Sair" da bandeja.
    # Em SO sem bandeja (Linux/macOS) o campo é ignorado e o fechar sai
    # normal.
    close_to_tray: bool = True
    # verificação
    verify_signatures: bool = True     # Authenticode em .exe/.dll/.msi
    remote_checksums: bool = True      # busca <arquivo>.sha256 / .md5
    # caminhos
    data_dir: str = DEFAULT_DATA_DIR
    # pasta base para as CATEGORIAS estilo IDM (Vídeos, Músicas…).
    # vazio = "~/Downloads" (convenção do IDM). Dentro dela criamos uma
    # subpasta por categoria.
    download_base: str = ""
    # Android: mover o arquivo concluído p/ a pasta pública Download/
    # do sistema (MediaStore em API 29+; WRITE_EXTERNAL_STORAGE em
    # 24–28) — sem isso os arquivos ficam no armazenamento privado do
    # app, invisíveis para o sistema. No desktop não tem efeito.
    android_public_downloads: bool = True

    @property
    def config_path(self) -> str:
        return os.path.join(self.data_dir, "config.json")

    @classmethod
    def load(cls, data_dir: str = DEFAULT_DATA_DIR) -> "AppConfig":
        cfg = cls(data_dir=data_dir)
        try:
            with open(cfg.config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return cfg
        known = {f.name for f in fields(cls)}
        for k, v in raw.items():
            if k in known and k != "data_dir":
                setattr(cfg, k, v)
        return cfg

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        d = asdict(self)
        d.pop("data_dir", None)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.config_path)

    def apply_flags(self, args) -> "AppConfig":
        """Sobrepõe configuração com flags explícitas da CLI (se presentes)."""
        for name in ("segments", "proxy", "cookie", "user_agent", "throttle",
                     "retries"):
            v = getattr(args, name, None)
            if v is None:
                continue
            attr = {"throttle": "throttle_bps"}.get(name, name)
            setattr(self, attr, v)
        return self
