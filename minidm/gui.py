"""Interface gráfica do minidm (tkinter — padrão do Python, sem dependências).

Roda o scheduler **no mesmo processo** (funciona com duplo clique, sem
terminal), com clipboard e API local opcionais.

Regras de threading:
* só a thread principal toca no tkinter;
* workers do scheduler publicam eventos numa fila (`queue.Queue`);
* a UI drena a fila e atualiza a árvore num `after()` periódico.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from minidm import __version__, clipboard as clipboard_mod
from minidm.config import AppConfig
from minidm.dashboard import eta, human
from minidm.meta import Task, TaskState
from minidm.remote import auto_delegate, filename_from
from minidm.scheduler import Scheduler
from minidm.store import TaskStore

REFRESH_MS = 500

STATE_COLOR = {
    TaskState.QUEUED: "#666666",
    TaskState.DOWNLOADING: "#0a7bd8",
    TaskState.PAUSED: "#c07a00",
    TaskState.DONE: "#1a8a3c",
    TaskState.ERROR: "#c02020",
}

STATE_PT = {
    TaskState.QUEUED: "na fila",
    TaskState.DOWNLOADING: "baixando",
    TaskState.PAUSED: "pausado",
    TaskState.DONE: "concluído",
    TaskState.ERROR: "erro",
}


class MinidmApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"minidm {__version__} — gerenciador de downloads")
        self.geometry("1080x620")
        self.minsize(860, 440)

        self.config_app = AppConfig.load()
        self.store = TaskStore(self.config_app.data_dir)
        self.events: queue.Queue = queue.Queue()
        self._last_dir = self.config_app.data_dir
        self._api_server = None
        self._clipboard_stop = threading.Event()
        self._clipboard_on = False

        self.sched = Scheduler(self.store, self.config_app,
                               on_event=self._on_event)
        self.sched.start()

        self._build_styles()
        self._build_toolbar()
        self._build_table()
        self._build_statusbar()
        self._build_menu()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._tick()
        self._refresh()

    # ------------------------------------------------------------------ UI
    def _build_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6))
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Status.TLabel", font=("Segoe UI", 9))

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 8, 8, 6))
        bar.pack(fill="x")

        ttk.Button(bar, text="+ Adicionar URL", command=self._dlg_add
                   ).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="Iniciar", command=self._do_start
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="Pausar", command=self._do_pause
                   ).pack(side="left", padx=4)
        ttk.Button(bar, text="Remover", command=self._do_remove
                   ).pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=10)

        self.var_clip = tk.BooleanVar(value=self._clipboard_on)
        ttk.Checkbutton(bar, text="Área de transferência",
                        variable=self.var_clip,
                        command=self._toggle_clipboard).pack(side="left", padx=4)

        self.var_api = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="API do navegador",
                        variable=self.var_api,
                        command=self._toggle_api).pack(side="left", padx=4)

        ttk.Button(bar, text="Configurações…", command=self._dlg_settings
                   ).pack(side="right")
        ttk.Button(bar, text="Abrir pasta", command=self._open_folder
                   ).pack(side="right", padx=6)

    def _build_table(self):
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8)

        cols = ("id", "arquivo", "estado", "barra", "pct", "recebido",
                "vel", "eta", "segs")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 selectmode="extended")
        heads = {
            "id": ("ID", 80, "center"),
            "arquivo": ("Arquivo", 320, "w"),
            "estado": ("Estado", 100, "center"),
            "barra": ("Progresso", 230, "w"),
            "pct": ("%", 56, "e"),
            "recebido": ("Recebido", 96, "e"),
            "vel": ("Velocidade", 100, "e"),
            "eta": ("ETA", 76, "e"),
            "segs": ("Segs", 60, "center"),
        }
        for c, (txt, w, anchor) in heads.items():
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anchor,
                             stretch=(c == "arquivo"))
        self.tree.tag_configure("done", foreground=STATE_COLOR[TaskState.DONE])
        self.tree.tag_configure("error", foreground=STATE_COLOR[TaskState.ERROR])

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self._dlg_details)
        self.tree.bind("<Delete>", lambda e: self._do_remove())

    def _build_statusbar(self):
        self.var_status = tk.StringVar(value="pronto")
        bar = ttk.Frame(self, padding=(8, 4, 8, 8))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.var_status,
                  style="Status.TLabel").pack(side="left")
        self.var_speed = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_speed, style="Status.TLabel",
                  foreground="#0a7bd8").pack(side="right")

    def _build_menu(self):
        menubar = tk.Menu(self)
        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="Adicionar URL…", command=self._dlg_add,
                           accelerator="Ctrl+N")
        m_file.add_separator()
        m_file.add_command(label="Sair", command=self._on_close)
        menubar.add_cascade(label="Arquivo", menu=m_file)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="Sobre", command=self._dlg_about)
        menubar.add_cascade(label="Ajuda", menu=m_help)
        self.config(menu=menubar)
        self.bind("<Control-n>", lambda e: self._dlg_add())

    # ------------------------------------------------------- loop principal
    def _tick(self):
        """Drena eventos vindos dos workers (thread-safe)."""
        try:
            while True:
                ev, task = self.events.get_nowait()
                if ev == "done":
                    self._status(f"Concluído: {task.filename}  "
                                 f"sha256={task.sha256}")
                elif ev == "error":
                    self._status(f"Erro: {task.filename} — {task.error}")
        except queue.Empty:
            pass
        self.after(REFRESH_MS, self._tick)

    def _on_event(self, event, task):
        """Chamado por threads do scheduler — só enfileira."""
        self.events.put((event, task))

    def _refresh(self):
        rows = self.sched.snapshot()
        selected = set(self.tree.selection())
        existing = set(self.tree.get_children())
        seen = set()

        total_speed = 0.0
        active = 0
        for r in rows:
            seen.add(r["id"])
            total_speed += r.get("speed") or 0.0
            if r["state"] == TaskState.DOWNLOADING:
                active += 1
            filled = int(r["progress"] * 18)
            bar = "#" * filled + "-" * (18 - filled)
            pct = f"{r['progress']*100:.1f}"
            state = STATE_PT.get(r["state"], r["state"])
            vals = (
                r["id"],
                r["filename"],
                state,
                bar,
                pct,
                f"{human(r['received'])} / {human(r['total'])}",
                f"{human(int(r.get('speed') or 0))}/s" if r.get("speed") else "",
                eta(r["received"], r["total"], r.get("speed") or 0.0),
                f"{r['segments_done']}/{r['segments_total']}"
                if r["segments_total"] else "",
            )
            tags = ()
            if r["state"] == TaskState.DONE:
                tags = ("done",)
            elif r["state"] == TaskState.ERROR:
                tags = ("error",)
            if r["id"] in existing:
                self.tree.item(r["id"], values=vals, tags=tags)
            else:
                self.tree.insert("", "end", iid=r["id"], values=vals, tags=tags)

        for iid in existing - seen:
            self.tree.delete(iid)
        if selected & seen:
            self.tree.selection_set(list(selected & seen))

        cfg = self.config_app
        lim = f" · limite {human(cfg.throttle_bps)}/s" if cfg.throttle_bps else ""
        self.var_speed.set(
            f"{active} ativo(s) · {cfg.max_concurrent} simultâneos"
            f" · {cfg.max_connections} conexões{lim}"
            f" · total {human(int(total_speed))}/s")

        self.after(REFRESH_MS, self._refresh)

    def _status(self, msg: str):
        self.var_status.set(msg)

    # ------------------------------------------------------------- ações
    def _selected_ids(self) -> list[str]:
        return list(self.tree.selection())

    def _do_start(self):
        ids = self._selected_ids()
        n = self.sched.submit(ids if ids else None)
        self._status(f"{n} tarefa(s) na fila." if ids
                     else f"{n} tarefa(s) selecionada(s) para iniciar.")

    def _do_pause(self):
        ids = self._selected_ids()
        if not ids:
            self._status("Selecione uma tarefa para pausar.")
            return
        ok = sum(1 for i in ids if self.sched.pause(i))
        self._status(f"{ok} tarefa(s) pausada(s) — progresso salvo.")

    def _do_remove(self):
        ids = self._selected_ids()
        if not ids:
            return
        if not messagebox.askyesno(
                "Remover", f"Remover {len(ids)} tarefa(s) da fila?\n"
                "(os arquivos parciais .part serão mantidos — pode retomar "
                "depois re-adicionando a URL)", parent=self):
            return

        def _worker():
            for i in ids:
                self.sched.remove(i)
            self.events.put(("info", None))

        threading.Thread(target=_worker, daemon=True).start()
        self._status("Removendo…")

    def _open_folder(self):
        ids = self._selected_ids()
        path = self._last_dir
        if ids:
            t = self.store.get(ids[0])
            if t:
                path = t.dest_dir
        if os.path.isdir(path):
            os.startfile(path)  # noqa: S606 — intencional no Windows

    # ---------------------------------------------------------- diálogos
    def _dlg_add(self):
        dlg = AddDialog(self, self._last_dir)
        self.wait_window(dlg)
        if not dlg.result:
            return
        spec = dlg.result
        task = Task(
            url=spec["url"],
            dest_dir=spec["dir"],
            filename=spec["filename"],
            mirrors=spec["mirrors"],
            expected_sha256=spec["sha256"] or None,
            proxy=spec["proxy"],
            cookie=spec["cookie"],
        )
        auto_delegate(task)     # link de página (YouTube…) -> yt-dlp
        self.store.add(task)
        self._last_dir = spec["dir"]
        self._status(f"[{task.id}] {task.filename} adicionado.")
        if spec["start"]:
            self.sched.submit([task.id])

    def _dlg_settings(self):
        dlg = SettingsDialog(self, self.config_app)
        self.wait_window(dlg)
        if not dlg.saved:
            return
        self.config_app = dlg.cfg
        self.sched.config = dlg.cfg
        if dlg.cfg.throttle_bps > 0:
            from minidm.throttle import TokenBucket
            self.sched.bucket = TokenBucket(dlg.cfg.throttle_bps)
        else:
            self.sched.bucket = None
        self._status("Configurações salvas.")

    def _dlg_details(self, _event=None):
        ids = self._selected_ids()
        if not ids:
            return
        t = self.store.get(ids[0])
        if t:
            DetailsDialog(self, t)

    def _dlg_about(self):
        messagebox.showinfo(
            "Sobre",
            f"minidm {__version__}\n\n"
            "Gerenciador de downloads didático, com código próprio.\n"
            "Inspirado nos conceitos de segmentação, retomada e fila —\n"
            "documentados na análise de arquitetura em docs/analise-idm.md.",
            parent=self)

    # ------------------------------------------------------------ extras
    def _toggle_clipboard(self):
        if self.var_clip.get():
            self._clipboard_stop.clear()
            self._clipboard_on = True

            def _watch():
                def on_url(url: str):
                    task = Task(url=url, dest_dir=self._last_dir,
                                filename=filename_from(url))
                    auto_delegate(task)   # link de página -> yt-dlp
                    self.store.add(task)
                    self.sched.submit([task.id])
                    self.events.put(("info", None))

                clipboard_mod.watch(on_url,
                                    interval=self.config_app.clipboard_interval,
                                    stop=self._clipboard_stop)

            threading.Thread(target=_watch, daemon=True).start()
            self._status("Observando a área de transferência…")
        else:
            self._clipboard_stop.set()
            self._clipboard_on = False
            self._status("Área de transferência desligada.")

    def _toggle_api(self):
        if self.var_api.get():
            from minidm.server import App, make_handler
            from http.server import ThreadingHTTPServer

            app = App(self.store, self.sched, self.config_app)
            httpd = ThreadingHTTPServer(
                ("127.0.0.1", self.config_app.api_port), make_handler(app))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            self._api_server = httpd
            self._status(f"API do navegador ativa em "
                         f"http://127.0.0.1:{self.config_app.api_port}")
        else:
            if self._api_server:
                self._api_server.shutdown()
                self._api_server.server_close()
                self._api_server = None
            self._status("API do navegador desligada.")

    def _on_close(self):
        self._clipboard_stop.set()
        if self._api_server:
            self._api_server.shutdown()
            self._api_server.server_close()
        busy = any(r["state"] == TaskState.DOWNLOADING
                   for r in self.sched.snapshot())
        if busy and not messagebox.askyesno(
                "Sair", "Há downloads em andamento.\n"
                "O progresso será salvo e você pode retomar depois.\n\n"
                "Sair mesmo assim?", parent=self):
            return
        self.sched.shutdown(wait=False)
        self.destroy()


# --------------------------------------------------------------- AddDialog
class AddDialog(tk.Toplevel):
    def __init__(self, parent, last_dir: str):
        super().__init__(parent)
        self.title("Adicionar URL")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        self.var_url = tk.StringVar()
        self.var_dir = tk.StringVar(value=last_dir)
        self.var_name = tk.StringVar()
        self.var_mirrors = tk.StringVar()
        self.var_sha = tk.StringVar()
        self.var_segments = tk.IntVar(value=0)
        self.var_start = tk.BooleanVar(value=True)

        def row(label, widget, r):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w",
                                             pady=(0, 8))
            widget.grid(row=r, column=1, sticky="ew", pady=(0, 8))

        e = ttk.Entry(body, textvariable=self.var_url, width=64)
        row("URL:", e, 0)
        e.focus_set()

        dirframe = ttk.Frame(body)
        ttk.Entry(dirframe, textvariable=self.var_dir).pack(
            side="left", fill="x", expand=True)
        ttk.Button(dirframe, text="…", width=3,
                   command=self._pick_dir).pack(side="left", padx=(6, 0))
        row("Pasta de destino:", dirframe, 1)
        row("Nome do arquivo:", ttk.Entry(body, textvariable=self.var_name,
                                          width=64), 2)
        row("Mirrors (um por linha):",
            ttk.Entry(body, textvariable=self.var_mirrors, width=64), 3)
        row("SHA-256 esperado (opcional):",
            ttk.Entry(body, textvariable=self.var_sha, width=64), 4)

        segframe = ttk.Frame(body)
        ttk.Label(segframe, text="Segmentos:").pack(side="left")
        ttk.Spinbox(segframe, from_=0, to=64, textvariable=self.var_segments,
                    width=5).pack(side="left", padx=(6, 0))
        ttk.Label(segframe, text="(0 = automático)").pack(side="left",
                                                          padx=(6, 0))
        ttk.Checkbutton(segframe, text="Começar agora",
                        variable=self.var_start).pack(side="left",
                                                      padx=(20, 0))
        row("", segframe, 5)

        btns = ttk.Frame(body)
        btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="Cancelar",
                   command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Adicionar",
                   command=self._ok).pack(side="right")

        body.columnconfigure(1, weight=1)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())

    def _pick_dir(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.var_dir.get())
        if d:
            self.var_dir.set(d)

    def _ok(self):
        url = self.var_url.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showerror("URL inválida",
                                 "Informe uma URL http:// ou https://",
                                 parent=self)
            return
        d = self.var_dir.get().strip() or self.var_dir.get()
        name = self.var_name.get().strip() or filename_from(url)
        mirrors = [m.strip() for m in
                   self.var_mirrors.get().splitlines() if m.strip()]
        sha = self.var_sha.get().strip().lower()
        if sha and (len(sha) != 64 or any(c not in "0123456789abcdef"
                                          for c in sha)):
            messagebox.showerror("SHA-256 inválido",
                                 "Deve ter 64 caracteres hexadecimais.",
                                 parent=self)
            return
        self.result = {
            "url": url,
            "dir": d,
            "filename": name,
            "mirrors": mirrors,
            "sha256": sha,
            "segments": self.var_segments.get(),
            "start": self.var_start.get(),
            "proxy": "",
            "cookie": "",
        }
        self.destroy()


# ------------------------------------------------------------ SettingsDialog
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg: AppConfig):
        super().__init__(parent)
        self.title("Configurações")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.saved = False
        self.cfg = cfg

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        self.vars = {}

        def num(label, key, r, hint=""):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w",
                                             pady=(0, 8))
            v = tk.StringVar(value=str(getattr(cfg, key)))
            ttk.Entry(body, textvariable=v, width=12).grid(
                row=r, column=1, sticky="w", pady=(0, 8), padx=(8, 0))
            if hint:
                ttk.Label(body, text=hint, foreground="#777").grid(
                    row=r, column=2, sticky="w", padx=(8, 0))
            self.vars[key] = (v, int)

        def txt(label, key, r, hint=""):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w",
                                             pady=(0, 8))
            v = tk.StringVar(value=str(getattr(cfg, key)))
            ttk.Entry(body, textvariable=v, width=40).grid(
                row=r, column=1, columnspan=2, sticky="ew", pady=(0, 8))
            if hint:
                ttk.Label(body, text=hint, foreground="#777").grid(
                    row=r, column=3, sticky="w", padx=(8, 0))
            self.vars[key] = (v, str)

        ttk.Label(body, text="Concorrência", font=("Segoe UI", 10, "bold")
                  ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        num("Downloads simultâneos:", "max_concurrent", 1)
        num("Conexões HTTP máx.:", "max_connections", 2,
            "compartilhadas entre todos")
        num("Segmentos por download:", "segments", 3, "0 = automático")

        ttk.Label(body, text="Resiliência e banda",
                  font=("Segoe UI", 10, "bold")
                  ).grid(row=4, column=0, sticky="w", pady=(10, 6))
        num("Tentativas por segmento:", "retries", 5)
        num("Limite de banda (bytes/s):", "throttle_bps", 6, "0 = sem limite")

        ttk.Label(body, text="Rede", font=("Segoe UI", 10, "bold")
                  ).grid(row=7, column=0, sticky="w", pady=(10, 6))
        txt("Proxy:", "proxy", 8, "http://host:port ou socks5://host:port")
        txt("Cookie:", "cookie", 9, "header Cookie completo")
        txt("User-Agent:", "user_agent", 10)

        ttk.Label(body, text="Verificação", font=("Segoe UI", 10, "bold")
                  ).grid(row=11, column=0, sticky="w", pady=(10, 6))
        self.var_sig = tk.BooleanVar(value=cfg.verify_signatures)
        ttk.Checkbutton(body, text="Verificar assinatura Authenticode "
                                   "(.exe/.dll/.msi)",
                        variable=self.var_sig).grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.var_chk = tk.BooleanVar(value=cfg.remote_checksums)
        ttk.Checkbutton(body, text="Buscar checksums publicados "
                                   "(<arquivo>.sha256 / .md5)",
                        variable=self.var_chk).grid(
            row=13, column=0, columnspan=3, sticky="w", pady=(0, 8))

        btns = ttk.Frame(body)
        btns.grid(row=14, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancelar",
                   command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="Salvar", command=self._save).pack(side="right")

        body.columnconfigure(1, weight=1)
        self.bind("<Escape>", lambda e: self.destroy())

    def _save(self):
        try:
            for key, (var, typ) in self.vars.items():
                raw = var.get().strip()
                setattr(self.cfg, key, typ(raw))
        except ValueError:
            messagebox.showerror("Valor inválido",
                                 "Verifique os campos numéricos.", parent=self)
            return
        self.cfg.verify_signatures = self.var_sig.get()
        self.cfg.remote_checksums = self.var_chk.get()
        self.cfg.save()
        self.saved = True
        self.destroy()


# ------------------------------------------------------------- DetailsDialog
class DetailsDialog(tk.Toplevel):
    def __init__(self, parent, task: Task):
        super().__init__(parent)
        self.title(f"Detalhes — {task.filename}")
        self.geometry("620x460")
        self.transient(parent)

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        info = ttk.LabelFrame(body, text="Tarefa", padding=10)
        info.pack(fill="x")
        lines = [
            ("ID", task.id),
            ("URL", task.url),
            ("Mirrors", "\n".join(task.mirrors) or "—"),
            ("Destino", task.final_path),
            ("Estado", STATE_PT.get(task.state, task.state)),
            ("Tamanho", human(task.total_size)),
            ("Recebido", f"{human(task.received)} "
                         f"({task.progress*100:.1f}%)"),
            ("Segmentos", f"{sum(1 for s in task.segments if s.done)} / "
                          f"{len(task.segments)}"),
            ("SHA-256", task.sha256 or "—"),
            ("Checksum remoto",
             {True: "OK", False: "DIVERGE",
              None: "—"}.get(task.checksum_ok, "—")),
            ("Assinatura", f"{task.signature_status or '—'}"
                           + (f"\n{task.signature_signer}"
                              if task.signature_signer else "")),
            ("Erro", task.error or "—"),
        ]
        for i, (k, v) in enumerate(lines):
            ttk.Label(info, text=f"{k}:", foreground="#555").grid(
                row=i, column=0, sticky="nw", pady=(0, 4))
            lbl = ttk.Label(info, text=str(v), wraplength=460)
            lbl.grid(row=i, column=1, sticky="w", pady=(0, 4), padx=(10, 0))

        if task.segments:
            segf = ttk.LabelFrame(body, text="Segmentos", padding=10)
            segf.pack(fill="both", expand=True, pady=(10, 0))
            txt = tk.Text(segf, height=8, font=("Consolas", 9))
            txt.pack(fill="both", expand=True)
            for i, s in enumerate(task.segments):
                pct = s.received / s.total * 100 if s.total else 100
                txt.insert(
                    "end",
                    f"seg {i:02d}  {s.start:>10}-{s.end:<10}  "
                    f"{human(s.received):>10}/{human(s.total):<10}  "
                    f"{pct:5.1f}%  {'OK' if s.done else ''}\n")
            txt.configure(state="disabled")

        ttk.Button(body, text="Fechar", command=self.destroy).pack(
            anchor="e", pady=(10, 0))


def main() -> int:
    app = MinidmApp()
    app.mainloop()
    return 0
