"""Interface de linha de comando do minidm (v2)."""

from __future__ import annotations

import argparse
import sys

from minidm import __version__
from minidm.config import AppConfig
from minidm.dashboard import human
from minidm.meta import Task, TaskState
from minidm.remote import filename_from
from minidm.store import TaskStore


# ---------------------------------------------------------------- utilitários
def _cfg(args) -> AppConfig:
    cfg = AppConfig.load(args.data_dir)
    return cfg.apply_flags(args)


def _print_task_line(t: Task) -> None:
    sig = f"  sig={t.signature_status}" if t.signature_status else ""
    chk = {True: "  checksum:ok", False: "  checksum:FALHOU"}.get(t.checksum_ok, "")
    print(f"[{t.id}] {t.state:12} {t.progress*100:5.1f}%  "
          f"{human(t.received):>12}/{human(t.total_size):<12} {t.filename}{chk}{sig}")
    if t.error:
        print(f"         erro: {t.error}")


# ------------------------------------------------------------------- comandos
def cmd_add(args) -> int:
    cfg = _cfg(args)
    store = TaskStore(cfg.data_dir)
    created = []
    for url in args.urls:
        task = Task(url=url, dest_dir=args.dir or cfg.data_dir,
                    filename=filename_from(url), state=TaskState.QUEUED,
                    mirrors=list(args.mirror or []),
                    proxy=args.proxy or "",
                    cookie=args.cookie or "",
                    user_agent=args.user_agent or "",
                    expected_sha256=args.sha256)
        store.add(task)
        created.append(task)
        print(f"[{task.id}] {task.filename}  <- {url}"
              + (f"  (+{len(task.mirrors)} mirror(s))" if task.mirrors else ""))
    if args.start:
        return _run_inline(store, cfg, [t.id for t in created])
    print("Use `python -m minidm start --all` ou `python -m minidm daemon`.")
    return 0


def cmd_list(args) -> int:
    cfg = _cfg(args)
    store = TaskStore(cfg.data_dir)
    tasks = store.all()
    if not tasks:
        print("Fila vazia.")
        return 0
    for t in tasks:
        _print_task_line(t)
    return 0


def cmd_start(args) -> int:
    cfg = _cfg(args)
    store = TaskStore(cfg.data_dir)
    if args.all:
        ids = [t.id for t in store.all()
               if t.state in (TaskState.QUEUED, TaskState.PAUSED, TaskState.ERROR)]
    else:
        ids = args.ids
        if not ids:
            print("Informe o id ou use --all.", file=sys.stderr)
            return 2
    return _run_inline(store, cfg, ids)


def _run_inline(store: TaskStore, cfg: AppConfig, ids: list[str]) -> int:
    """Roda o scheduler aqui mesmo até a fila selecionada esvaziar."""
    from minidm.scheduler import Scheduler

    sched = Scheduler(store, cfg)
    sched.start()
    n = sched.submit(ids)
    if n == 0:
        print("Nada para iniciar.")
        sched.shutdown()
        return 0
    try:
        if sys.stdout.isatty():
            from minidm.dashboard import live_dashboard

            live_dashboard(sched, throttle_bps=cfg.throttle_bps)
        else:
            sched.wait_idle()
    except KeyboardInterrupt:
        print("\nparando — progresso salvo")
    finally:
        sched.shutdown()

    exit_code = 0
    for t in store.all():
        if t.id in ids:
            _print_task_line(t)
            if t.state == TaskState.ERROR:
                exit_code = 1
    return exit_code


def cmd_pause(args) -> int:
    cfg = _cfg(args)
    store = TaskStore(cfg.data_dir)
    ok = store.get(args.id) is not None
    if not ok:
        print("Tarefa não encontrada.", file=sys.stderr)
        return 2
    # pausa local: marca o estado; se houver daemon ativo ele para no próximo
    # checkpoint via /pause — aqui garantimos o caso "não está rodando"
    t = store.get(args.id)
    if t.state in (TaskState.QUEUED, TaskState.ERROR):
        t.state = TaskState.PAUSED
        store.save()
    _try_api_pause(args.id)
    _print_task_line(t)
    return 0


def _try_api_pause(task_id: str, port: int = 9742) -> bool:
    """Se houver daemon rodando, avisa via API."""
    try:
        import json
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/pause",
            data=json.dumps({"id": task_id}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


def cmd_remove(args) -> int:
    cfg = _cfg(args)
    store = TaskStore(cfg.data_dir)
    ok = store.remove(args.id, delete_files=args.delete)
    if not ok:
        print("Tarefa não encontrada.", file=sys.stderr)
        return 2
    print("Removida." + (" (arquivos apagados)" if args.delete else ""))
    return 0


def cmd_hash(args) -> int:
    from minidm.verify import sha256_file

    print(sha256_file(args.file))
    return 0


def cmd_verify(args) -> int:
    """Revalida um arquivo já baixado: hash, checksum do servidor, assinatura."""
    from minidm import verify as v

    cfg = _cfg(args)
    path = args.file
    url = args.url
    remote_sha = remote_md = None
    if url and cfg.remote_checksums:
        print(f"procurando checksums publicados ao lado de {url} …")
        remote_sha, remote_md = v.fetch_remote_checksums(url, cfg.user_agent)
    print(f"verificando {path} …")
    report = v.verify_file(path, expected_sha256=args.sha256,
                           remote_sha256=remote_sha, remote_md5=remote_md,
                           check_signature=cfg.verify_signatures)
    print(f"  sha256 local     : {report['sha256']}")
    _flag("sha256 esperado  ", report["expected_sha256_ok"])
    _flag("sha256 do servidor", report["remote_sha256_ok"])
    _flag("md5 do servidor  ", report["remote_md5_ok"])
    if report["signature_status"]:
        print(f"  assinatura       : {report['signature_status']}")
        if report["signature_signer"]:
            print(f"  assinante        : {report['signature_signer']}")
    bad = [report[k] for k in ("expected_sha256_ok", "remote_sha256_ok",
                               "remote_md5_ok") if report[k] is False]
    return 1 if bad else 0


def _flag(label: str, value) -> None:
    if value is True:
        print(f"  {label}: OK")
    elif value is False:
        print(f"  {label}: DIVERGE")
    else:
        print(f"  {label}: (sem referência)")


def cmd_watch(args) -> int:
    from minidm import clipboard

    cfg = _cfg(args)
    store = TaskStore(cfg.data_dir)
    print(f"Observando a área de transferência ({cfg.clipboard_interval:.0f}s). Ctrl+C encerra.")

    def on_url(url: str):
        task = Task(url=url, dest_dir=args.dir or cfg.data_dir,
                    filename=filename_from(url), state=TaskState.QUEUED)
        store.add(task)
        print(f"[{task.id}] capturado: {url}")

    try:
        clipboard.watch(on_url, interval=cfg.clipboard_interval)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_serve(args) -> int:
    cfg = _cfg(args)
    from minidm.daemon import run_daemon

    return run_daemon(cfg, watch_clipboard=False, dashboard=args.dashboard)


def cmd_gui(args) -> int:
    # a GUI carrega a própria configuração ao construir o app
    from minidm import gui

    print("Abrindo a interface gráfica…")
    return gui.main()


def cmd_daemon(args) -> int:
    cfg = _cfg(args)
    from minidm.daemon import run_daemon

    return run_daemon(cfg, watch_clipboard=not args.no_clipboard,
                      dashboard=args.dashboard)


def cmd_config(args) -> int:
    cfg = _cfg(args)
    if args.action == "show":
        import json
        from dataclasses import asdict

        d = asdict(cfg)
        d.pop("data_dir", None)
        print(json.dumps(d, indent=2))
        return 0
    # set
    if not hasattr(cfg, args.key):
        print(f"chave desconhecida: {args.key}", file=sys.stderr)
        return 2
    current = getattr(cfg, args.key)
    value = args.value
    if isinstance(current, bool):
        value = value.lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int):
        value = int(value)
    elif isinstance(current, float):
        value = float(value)
    setattr(cfg, args.key, value)
    cfg.save()
    print(f"{args.key} = {value!r}  (salvo em {cfg.config_path})")
    return 0


# --------------------------------------------------------------------- main
def _add_net_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--proxy", help="proxy http://host:port ou socks5://host:port")
    p.add_argument("--cookie", help="header Cookie completo")
    p.add_argument("--user-agent", dest="user_agent", help="User-Agent")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="minidm",
                                description="Gerenciador de downloads didático (v2).")
    p.add_argument("--version", action="version", version=f"minidm {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=None,
                        help="diretório de estado (padrão: ~/.minidm)")
    common.add_argument("--throttle", type=int, default=None,
                        help="limite global de banda em bytes/segundo")
    common.add_argument("--retries", type=int, default=None,
                        help="tentativas por segmento")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="adiciona URL(s) à fila", parents=[common])
    a.add_argument("urls", nargs="+")
    a.add_argument("--dir", help="diretório de destino")
    a.add_argument("--segments", type=int, default=None,
                   help="nº de segmentos (0/omitido = automático)")
    a.add_argument("--mirror", action="append",
                   help="URL alternativa do mesmo arquivo (pode repetir)")
    a.add_argument("--sha256", help="hash esperado (valida ao concluir)")
    _add_net_flags(a)
    a.add_argument("--start", action="store_true", help="começa já baixar")
    a.set_defaults(func=cmd_add)

    a = sub.add_parser("list", help="lista a fila", parents=[common])
    a.set_defaults(func=cmd_list)

    a = sub.add_parser("start", help="processa a fila até terminar", parents=[common])
    a.add_argument("ids", nargs="*")
    a.add_argument("--all", action="store_true")
    a.set_defaults(func=cmd_start)

    a = sub.add_parser("pause", help="pausa uma tarefa", parents=[common])
    a.add_argument("id")
    a.set_defaults(func=cmd_pause)

    a = sub.add_parser("remove", help="remove tarefa da fila", parents=[common])
    a.add_argument("id")
    a.add_argument("--delete", action="store_true",
                   help="apaga também o .part/.meta parcial")
    a.set_defaults(func=cmd_remove)

    a = sub.add_parser("hash", help="SHA-256 de um arquivo", parents=[common])
    a.add_argument("file")
    a.set_defaults(func=cmd_hash)

    a = sub.add_parser("verify", help="revalida um arquivo baixado",
                       parents=[common])
    a.add_argument("file")
    a.add_argument("--url", help="URL original (busca .sha256/.md5 do servidor)")
    a.add_argument("--sha256", help="hash esperado")
    _add_net_flags(a)
    a.set_defaults(func=cmd_verify)

    a = sub.add_parser("watch", help="observa a área de transferência",
                       parents=[common])
    a.add_argument("--dir")
    a.set_defaults(func=cmd_watch)

    a = sub.add_parser("daemon", help="scheduler + clipboard + API local",
                       parents=[common])
    a.add_argument("--no-clipboard", action="store_true")
    a.add_argument("--dashboard", action="store_true",
                   help="mostra TUI no terminal")
    a.set_defaults(func=cmd_daemon)

    a = sub.add_parser("tui", help="daemon com dashboard ao vivo",
                       parents=[common])
    a.add_argument("--no-clipboard", action="store_true")
    a.set_defaults(func=lambda args: run_tui(args))

    a = sub.add_parser("serve", help="API local sem clipboard",
                       parents=[common])
    a.add_argument("--dashboard", action="store_true")
    a.set_defaults(func=cmd_serve)

    a = sub.add_parser("gui", help="interface gráfica (tkinter)",
                       parents=[common])
    a.set_defaults(func=cmd_gui)

    a = sub.add_parser("config", help="mostra/edita configuração",
                       parents=[common])
    a.add_argument("action", choices=["show", "set"])
    a.add_argument("key", nargs="?")
    a.add_argument("value", nargs="?")
    a.set_defaults(func=cmd_config)

    return p


def run_tui(args) -> int:
    cfg = _cfg(args)
    from minidm.daemon import run_daemon

    return run_daemon(cfg, watch_clipboard=not args.no_clipboard,
                      dashboard=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "config" and args.action == "set" and not (args.key and args.value):
        print("uso: minidm config set <chave> <valor>", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
