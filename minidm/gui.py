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
from minidm.dashboard import eta, eta_seconds, human
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

STATE_TAG = {
    TaskState.DOWNLOADING: "down",
    TaskState.PAUSED: "paused",
    TaskState.DONE: "done",
    TaskState.ERROR: "error",
}


def progress_bar(progress: float, width: int = 20) -> str:
    """Barra em blocos Unicode (tkinter é UTF-16: renderiza em qualquer SO).

    Bloco cheio = feito; vazio = falta. Corta em [0, 1] p/ não estourar
    com valores estranhos (ytdlp reporta ~100% antes do mux).
    """
    p = 0.0 if progress is None or progress != progress else progress
    p = max(0.0, min(1.0, p))
    filled = int(round(p * width))
    return "█" * filled + "░" * (width - filled)


# chave de ordenação por coluna (clique no cabeçalho) — pura, p/ testar
SORT_KEYS = {
    "id": lambda r: r["id"],
    "arquivo": lambda r: (r.get("filename") or "").lower(),
    "estado": lambda r: str(r["state"]),
    "badge": lambda r: kind_badge(r).lower(),
    "barra": lambda r: r.get("progress") or 0.0,
    "pct": lambda r: r.get("progress") or 0.0,
    "recebido": lambda r: r.get("received") or 0,
    "vel": lambda r: r.get("speed") or 0.0,
    "eta": lambda r: eta_seconds(r.get("received") or 0, r.get("total"),
                                 r.get("speed") or 0.0),
    "segs": lambda r: r.get("segments_done") or 0,
    "perfil": lambda r: (r.get("profile") or "").lower(),
}


def sort_rows(rows: list[dict], col: str, asc: bool = True) -> list[dict]:
    """Ordena as linhas do snapshot pela coluna `col`.

    ETA é o único que não pode ter `None` na comparação (tarefa sem
    estimativa) — ele vai pro fim em qualquer sentido da ordenação.
    """
    key = SORT_KEYS.get(col)
    if key is None:
        return rows
    if col == "eta":
        # sem estimativa (parado/sem tamanho) -> sempre no fim
        real = [r for r in rows if key(r) is not None]
        pending = [r for r in rows if key(r) is None]
        real.sort(key=key, reverse=not asc)
        return real + pending
    return sorted(rows, key=key, reverse=not asc)


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
        self._filter_category = ""      # "" = Todas (sidebar)
        self._rows_by_id: dict[str, dict] = {}   # cache p/ seleção pós-refresh
        self._speed_smooth: dict[str, float] = {}  # p/ ETA não dançar

        self.sched = Scheduler(self.store, self.config_app,
                               on_event=self._on_event)
        self.sched.start()

        self._build_styles()
        self._build_sidebar()
        self._build_toolbar()
        self._build_table()
        self._build_statusbar()
        self._build_menu()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # API local ligada por padrão (config `api_enabled`): a extensão do
        # navegador funciona logo ao abrir, sem clicar em nada.
        if self.var_api.get():
            try:
                self._start_api()
            except OSError as e:
                # outro minidm/daemon já serve a porta — GUI segue normal
                self.var_api.set(False)
                self._status(f"API já ativa em outro processo (porta "
                             f"{self.config_app.api_port}): {e}")
        self._update_toolbar_state()

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
        self.btn_start = ttk.Button(bar, text="Iniciar", command=self._do_start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_pause = ttk.Button(bar, text="Pausar", command=self._do_pause)
        self.btn_pause.pack(side="left", padx=4)
        self.btn_remove = ttk.Button(bar, text="Remover",
                                     command=self._do_remove)
        self.btn_remove.pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=10)

        self.btn_preview = ttk.Button(bar, text="▶ Visualizar",
                                      command=self._dlg_preview)
        self.btn_preview.pack(side="left", padx=(0, 4))
        self.btn_details = ttk.Button(bar, text="Propriedades",
                                      command=self._dlg_details)
        self.btn_details.pack(side="left", padx=4)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                   padx=10)

        self.var_clip = tk.BooleanVar(value=self._clipboard_on)
        ttk.Checkbutton(bar, text="Área de transferência",
                        variable=self.var_clip,
                        command=self._toggle_clipboard).pack(side="left", padx=4)

        # API do navegador: LIGADA por padrão (config `api_enabled`) — a
        # extensão captura vídeos assim que a GUI abre, sem marcar nada.
        self.var_api = tk.BooleanVar(value=bool(self.config_app.api_enabled))
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

        cols = ("id", "arquivo", "estado", "badge", "barra", "pct", "recebido",
                "vel", "eta", "segs", "perfil")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 selectmode="extended")
        heads = {
            "id": ("ID", 80, "center"),
            "arquivo": ("Arquivo", 300, "w"),
            "estado": ("Estado", 96, "center"),
            "badge": ("Tipo", 84, "center"),
            "barra": ("Progresso", 200, "w"),
            "pct": ("%", 52, "e"),
            "recebido": ("Recebido", 92, "e"),
            "vel": ("Velocidade", 96, "e"),
            "eta": ("ETA", 72, "e"),
            "segs": ("Segs", 56, "center"),
            "perfil": ("Perfil", 84, "center"),
        }
        for c, (txt, w, anchor) in heads.items():
            # ordenação por clique no cabeçalho (estilo IDM/Explorer)
            self.tree.heading(c, text=txt, command=lambda col=c:
                              self._sort_by(col))
            self.tree.column(c, width=w, anchor=anchor,
                             stretch=(c == "arquivo"))
        # linhas listradas + estados: a UI "anda" sem depender do texto
        self.tree.tag_configure("done", foreground=STATE_COLOR[TaskState.DONE])
        self.tree.tag_configure("error", foreground=STATE_COLOR[TaskState.ERROR])
        self.tree.tag_configure("down", foreground=STATE_COLOR[TaskState.DOWNLOADING])
        self.tree.tag_configure("paused", foreground=STATE_COLOR[TaskState.PAUSED])
        self.tree.tag_configure("odd", background="#f2f5f9")
        self.tree.tag_configure("even", background="#ffffff")
        self._sort_col = ""          # "" = ordem natural (fila)
        self._sort_asc = True

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self._dlg_details)
        self.tree.bind("<Delete>", lambda e: self._do_remove())
        self.tree.bind("<<TreeviewSelect>>", lambda e:
                       self._update_toolbar_state())

    def _build_statusbar(self):
        self.var_status = tk.StringVar(value="pronto")
        bar = ttk.Frame(self, padding=(8, 4, 8, 8))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.var_status,
                  style="Status.TLabel").pack(side="left")

        # progresso AGREGADO de todas as tarefas com tamanho conhecido —
        # barra viva, tipo IDM (algo sempre se mexe na janela)
        self.pbar = ttk.Progressbar(bar, length=240, mode="determinate",
                                    maximum=100.0)
        self.pbar.pack(side="right", padx=(10, 0))
        # tempo restante total (soma do que falta / soma das velocidades)
        self.var_eta_total = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.var_eta_total, style="Status.TLabel",
                  foreground="#c07a00").pack(side="right", padx=(0, 10))
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

    def _build_sidebar(self):
        """Barra lateral de categorias estilo IDM (Todas + categorias + contagem)."""
        from minidm.categories import default_categories

        side = ttk.Frame(self, padding=(8, 8, 0, 8))
        side.pack(side="left", fill="y")
        self._side = side

        ttk.Label(side, text="Categorias",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 6))

        self._cat_items: dict[str, str] = {}   # nome -> iid no Listbox
        self._cat_counts: dict[str, int] = {}

        # lista de categorias: primeiro "Todas", depois as do IDM
        names = ["Todas"] + [c.name for c in default_categories()]
        self._cat_list = tk.Listbox(side, exportselection=False, height=len(names),
                                    activestyle="dotbox", font=("Segoe UI", 10),
                                    relief="flat", bd=0, highlightthickness=0)
        self._cat_list.pack(fill="x")
        for n in names:
            self._cat_list.insert("end", f"{n}")
        self._cat_list.selection_set(0)
        self._cat_list.bind("<<ListboxSelect>>", self._on_category)

        # rótulo de contagem p/ a categoria selecionada
        self._var_cat_info = tk.StringVar(value="")
        ttk.Label(side, textvariable=self._var_cat_info, foreground="#777",
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 0))

    def _on_category(self, _event=None):
        sel = self._cat_list.curselection()
        idx = sel[0] if sel else 0
        from minidm.categories import default_categories

        names = ["Todas"] + [c.name for c in default_categories()]
        name = names[idx]
        self._filter_category = "" if name == "Todas" else name
        self._refresh()

    def _update_sidebar(self, rows: list[dict]):
        """Reconta por categoria e destaca a selecionada."""
        from minidm.categories import default_categories

        counts: dict[str, int] = {}
        for r in rows:
            key = r.get("effective_category") or ""
            counts[key] = counts.get(key, 0) + 1
        self._cat_counts = counts
        names = ["Todas"] + [c.name for c in default_categories()]
        self._cat_list.delete(0, "end")
        for n in names:
            key = "" if n == "Todas" else n
            c = counts.get(key, 0) if n != "Todas" else len(rows)
            self._cat_list.insert("end", f"{n}  ({c})")
        # re-seleciona a atual
        cur = "Todas" if not self._filter_category else self._filter_category
        idx = names.index(cur) if cur in names else 0
        self._cat_list.selection_clear(0, "end")
        self._cat_list.selection_set(idx)
        sel = self._filter_category
        c = counts.get(sel, 0) if sel else len(rows)
        self._var_cat_info.set(f"{c} tarefa(s) nesta categoria")

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

    def _refresh(self, reschedule: bool = True):
        rows = self.sched.snapshot()
        self._update_sidebar(rows)

        # filtro da sidebar (categoria estilo IDM) — usa a categoria efetiva
        # (explícita ou deduzida pela extensão), p/ agrupar tarefas antigas
        if self._filter_category:
            rows = [r for r in rows
                    if (r.get("effective_category") or "") == self._filter_category]

        # ordenação por clique no cabeçalho ("" = ordem natural da fila)
        if self._sort_col:
            rows = sort_rows(rows, self._sort_col, self._sort_asc)

        self._rows_by_id = {r["id"]: r for r in rows}
        selected = set(self.tree.selection())
        existing = set(self.tree.get_children())
        seen = set()

        total_speed = 0.0
        active = 0
        for idx, r in enumerate(rows):
            seen.add(r["id"])
            # velocidade suavizada (média 50/50): o ETA não fica pulando
            # a cada amostra de 0,5 s
            raw_speed = r.get("speed") or 0.0
            prev_speed = self._speed_smooth.get(r["id"], 0.0)
            speed = raw_speed if not prev_speed else (prev_speed + raw_speed) / 2
            self._speed_smooth[r["id"]] = speed
            total_speed += speed
            if r["state"] == TaskState.DOWNLOADING:
                active += 1
            # HLS ao vivo: total ainda não existe — mostra "ao vivo"
            # em vez de uma barra mentirosa em 0%
            live = bool(r.get("live")) and \
                r["state"] == TaskState.DOWNLOADING
            if live:
                bar = "ao vivo (sem fim)"
                pct = "--"
            else:
                bar = progress_bar(r["progress"])
                pct = f"{r['progress']*100:.1f}"
            state = STATE_PT.get(r["state"], r["state"])
            # badge de tipo/qualidade (estilo IDM): HLS/DASH/áudio + qualidade
            badge = self._kind_badge(r)
            vals = (
                r["id"],
                r["filename"],
                state,
                badge,
                bar,
                pct,
                f"{human(r['received'])} / {human(r['total'])}",
                f"{human(int(speed))}/s" if speed else "",
                eta(r["received"], r["total"], speed),
                f"{r['segments_done']}/{r['segments_total']}"
                if r["segments_total"] else "",
                r.get("profile") or "",
            )
            # listras zebradas + cor do estado (a linha "anda" visualmente)
            tags = (("odd" if idx % 2 else "even"),)
            st_tag = STATE_TAG.get(r["state"])
            if st_tag:
                tags += (st_tag,)
            if r["id"] in existing:
                self.tree.move(r["id"], "", "end")
                self.tree.item(r["id"], values=vals, tags=tags)
            else:
                self.tree.insert("", "end", iid=r["id"], values=vals, tags=tags)

        for iid in existing - seen:
            self.tree.delete(iid)
        if selected & seen:
            self.tree.selection_set(list(selected & seen))
        # esquece velocidades de tarefas que saíram da lista/filtro
        if set(self._speed_smooth) - seen:
            for k in list(self._speed_smooth):
                if k not in seen:
                    del self._speed_smooth[k]

        # --------- progresso AGREGADO + tempo restante total (barra inferior)
        known = [(r["received"], r["total"]) for r in rows if r.get("total")]
        total_known = sum(t for _, t in known)
        recv_known = sum(min(rv, t) for rv, t in known)
        if total_known:
            self.pbar["value"] = 100.0 * recv_known / total_known
            eta_tot = eta_seconds(recv_known, total_known, total_speed)
            self.var_eta_total.set(
                f"restante {eta(recv_known, total_known, total_speed)}"
                if eta_tot is not None else "restante --:--")
        else:
            self.pbar["value"] = 0.0
            self.var_eta_total.set("")

        cfg = self.config_app
        lim = f" · limite {human(cfg.throttle_bps)}/s" if cfg.throttle_bps else ""
        done_all = sum(1 for r in rows if r["state"] == TaskState.DONE)
        self.var_speed.set(
            f"{active} ativo(s) · {done_all}/{len(rows)} concluídos"
            f" · {human(recv_known)}/{human(total_known)}"
            f" · {cfg.max_concurrent} simultâneos"
            f" · {cfg.max_connections} conexões{lim}"
            f" · total {human(int(total_speed))}/s")

        if reschedule:
            self.after(REFRESH_MS, self._refresh)

    def _status(self, msg: str):
        self.var_status.set(msg)

    def _kind_badge(self, r: dict) -> str:
        return kind_badge(r)

    # ------------------------------------------------- ordenação/toolbar
    def _sort_by(self, col: str):
        """Clique no cabeçalho: ordena; clique de novo inverte."""
        if self._sort_col == col:
            if self._sort_asc:            # 3º clique volta à ordem natural
                self._sort_asc = False
            else:
                self._sort_col = ""
                self._sort_asc = True
        else:
            self._sort_col = col
            self._sort_asc = True
        self._refresh_now()

    def _refresh_now(self):
        """Um ciclo de refresh imediato (a ordenação responde ao clique
        sem esperar o tick periódico de 500 ms). Não reagenda — a cadeia
        periódica continua viva sozinha."""
        self._refresh(reschedule=False)

    def _update_toolbar_state(self):
        """Botões que exigem seleção ficam desligados sem seleção."""
        has = bool(self.tree.selection())
        for btn in (self.btn_pause, self.btn_remove, self.btn_details,
                    self.btn_preview):
            btn.configure(state="normal" if has else "disabled")

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
            category=spec.get("category", ""),
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
            DetailsDialog(self, t, row=self._rows_by_id.get(ids[0]))

    def _dlg_preview(self, _event=None):
        """Abre o visualizador com a URL da tarefa selecionada (ou pede uma)."""
        ids = self._selected_ids()
        url = ""
        if ids:
            t = self.store.get(ids[0])
            if t:
                url = t.url

        def _add(u, q, res):
            self._enqueue_media(u, q, res)

        dlg = MediaPreviewDialog(self, url, self.config_app, on_add=_add)
        self.wait_window(dlg)

    def _enqueue_media(self, url: str, q: dict, res: dict):
        """Cria uma tarefa a partir de uma escolha do visualizador."""
        task = Task(
            url=q.get("url") or url,
            dest_dir=self._last_dir,
            filename=res.get("filename") or filename_from(q.get("url") or url),
            kind="hls" if "hls" in (q.get("kind", ""), res.get("kind", "")) else
                 ("dash" if "dash" in (q.get("kind", ""),
                                       res.get("kind", "")) else "file"),
            referer=url,
            page_title=res.get("page_title", ""),
            quality_label=q.get("label", ""),
            user_agent=self.config_app.user_agent,
        )
        auto_delegate(task)
        self.store.add(task)
        self.sched.submit([task.id])
        self._status(f"[{task.id}] {task.filename} — {q.get('label', '')} na fila.")

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
        """Checkbox da toolbar: liga/desliga a API e persiste a escolha
        em `api_enabled` (na próxima abertura já vem assim)."""
        try:
            if self.var_api.get():
                self._start_api()
            else:
                self._stop_api()
                self._status("API do navegador desligada.")
        except OSError as e:
            # porta ocupada (outro minidm/daemon já serve a API): avisa e
            # desmarca o checkbox em vez de quebrar a GUI
            self.var_api.set(False)
            self._status(f"API não iniciada (porta "
                         f"{self.config_app.api_port} em uso): {e}")
        self.config_app.api_enabled = bool(self.var_api.get())
        try:
            self.config_app.save()
        except OSError:
            pass

    def _start_api(self):
        if self._api_server:
            return
        from minidm.server import App, make_handler
        from http.server import ThreadingHTTPServer

        app = App(self.store, self.sched, self.config_app)
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", self.config_app.api_port), make_handler(app))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self._api_server = httpd
        self._status(f"API do navegador ativa em "
                     f"http://127.0.0.1:{self.config_app.api_port} — "
                     f"a extensão já pode enviar links")

    def _stop_api(self):
        if self._api_server:
            self._api_server.shutdown()
            self._api_server.server_close()
            self._api_server = None

    def _on_close(self):
        self._clipboard_stop.set()
        self._stop_api()
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
class _CfgForDialog:
    """Mini-adaptador: `category_folder` espera um objeto com `download_base`."""

    def __init__(self, download_base: str):
        self.download_base = download_base


class AddDialog(tk.Toplevel):
    def __init__(self, parent, last_dir: str):
        super().__init__(parent)
        self.title("Adicionar URL")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result = None
        self._last_dir = last_dir
        self._base = resolve_base(AppConfig.load())   # pasta base das categorias

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        self.var_url = tk.StringVar()
        from minidm.categories import default_categories, category_folder, \
            ensure_folder, resolve_base

        self._cfg_base = resolve_base(AppConfig.load())
        self._cats = default_categories()
        self.var_cat = tk.StringVar(value=self._cats[0].name)   # Vídeos
        default_dir = ensure_folder(
            category_folder(self._cats[0], _CfgForDialog(self._cfg_base)))
        self.var_dir = tk.StringVar(value=default_dir)
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

        # categoria estilo IDM: define a pasta automaticamente
        catframe = ttk.Frame(body)
        self.var_cat_cb = ttk.Combobox(
            catframe, state="readonly", width=22,
            values=[c.name for c in self._cats],
            textvariable=self.var_cat)
        self.var_cat_cb.pack(side="left")
        self.var_cat_cb.bind("<<ComboboxSelected>>", self._on_category)
        ttk.Label(catframe, text="(define a pasta de destino)"
                  ).pack(side="left", padx=(8, 0))
        row("Categoria:", catframe, 1)

        dirframe = ttk.Frame(body)
        ttk.Entry(dirframe, textvariable=self.var_dir).pack(
            side="left", fill="x", expand=True)
        ttk.Button(dirframe, text="…", width=3,
                   command=self._pick_dir).pack(side="left", padx=(6, 0))
        row("Pasta de destino:", dirframe, 2)
        row("Nome do arquivo:", ttk.Entry(body, textvariable=self.var_name,
                                          width=64), 3)
        row("Mirrors (um por linha):",
            ttk.Entry(body, textvariable=self.var_mirrors, width=64), 4)
        row("SHA-256 esperado (opcional):",
            ttk.Entry(body, textvariable=self.var_sha, width=64), 5)

        segframe = ttk.Frame(body)
        ttk.Label(segframe, text="Segmentos:").pack(side="left")
        ttk.Spinbox(segframe, from_=0, to=64, textvariable=self.var_segments,
                    width=5).pack(side="left", padx=(6, 0))
        ttk.Label(segframe, text="(0 = automático)").pack(side="left",
                                                          padx=(6, 0))
        ttk.Checkbutton(segframe, text="Começar agora",
                        variable=self.var_start).pack(side="left",
                                                      padx=(20, 0))
        row("", segframe, 6)

        btns = ttk.Frame(body)
        btns.grid(row=7, column=0, columnspan=2, sticky="e", pady=(8, 0))
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

    def _on_category(self, _event=None):
        """Trocar a categoria move a pasta de destino (estilo IDM)."""
        from minidm.categories import category_folder, ensure_folder, \
            get_category

        cat = get_category(self.var_cat.get())
        if cat:
            self.var_dir.set(ensure_folder(
                category_folder(cat, _CfgForDialog(self._cfg_base))))

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
            "category": self.var_cat.get().strip(),
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
    """Propriedades ricas de uma tarefa (estilo IDM).

    Aceita um dict do `snapshot()` (rico) e/ou a `Task` (para os segmentos).
    Campos ausentes são simplesmente omitidos.
    """

    def __init__(self, parent, task: Task, row: dict | None = None):
        super().__init__(parent)
        row = row or {}
        title = row.get("page_title") or task.page_title or task.filename
        self.title(f"Propriedades — {title}")
        self.geometry("660x520")
        self.transient(parent)

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        info = ttk.LabelFrame(body, text="Tarefa", padding=10)
        info.pack(fill="x")

        kind = row.get("kind", task.kind)
        kind_pt = {"hls": "vídeo (HLS)", "dash": "vídeo (DASH)",
                   "file": "arquivo"}.get(kind, kind)
        lines = [
            ("ID", task.id),
            ("Página", row.get("page_title") or task.page_title or "—"),
            ("Categoria", row.get("category", task.category) or "—"),
            ("Qualidade", row.get("quality_label") or task.quality_label or "—"),
            ("Perfil", row.get("profile") or task.profile or "—"),
            ("Tipo", kind_pt),
            ("URL", task.url),
            ("Origem", row.get("origin_url") or task.origin_url or "—"),
            ("Mirrors", "\n".join(task.mirrors) or "—"),
            ("Destino", task.final_path),
            ("Estado", STATE_PT.get(task.state, task.state)),
            ("Tamanho", human(task.total_size)),
            ("Recebido", f"{human(task.received)} "
                         f"({task.progress*100:.1f}%)"),
            ("Velocidade", f"{human(int(row.get('speed') or 0))}/s"
             if row.get("speed") else "—"),
            ("Tempo restante",
             eta(task.received, task.total_size, row.get("speed") or 0.0)
             if row else "—"),
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
        r = 0
        for k, v in lines:
            if v in (None, "", "—") and k in ("Página", "Categoria",
                                              "Qualidade", "Perfil", "Origem"):
                continue      # não polui com campos vazios
            ttk.Label(info, text=f"{k}:", foreground="#555").grid(
                row=r, column=0, sticky="nw", pady=(0, 4))
            lbl = ttk.Label(info, text=str(v), wraplength=500)
            lbl.grid(row=r, column=1, sticky="w", pady=(0, 4), padx=(10, 0))
            r += 1
        info.columnconfigure(1, weight=1)

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


def kind_badge(r: dict) -> str:
    """Rótulo curto de tipo/qualidade (estilo IDM): 1080p, HLS, DASH, mp3…"""
    q = (r.get("quality_label") or "").strip()
    kind = (r.get("kind") or "file").lower()
    prefix = {"hls": "HLS", "dash": "DASH"}.get(kind, "")
    if q:
        return f"{prefix} {q}".strip()
    if kind in ("hls", "dash"):
        return prefix
    # arquivo direto: acha o tipo pela extensão
    name = (r.get("filename") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return ext.upper() if ext else "arq"


def _best_playable(res: dict) -> tuple[str, dict] | None:
    """Escolhe a melhor qualidade reproduzível para o preview.

    Prefere a de maior resolução/banda. Para HLS/DASH devolve o próprio
    manifesto (o media source do navegador/ffmpeg resolve); para arquivo
    direto, o URL. Ignora faixas de áudio puro se houver vídeo.
    """
    quals = res.get("qualities") or []
    if not quals:
        return None

    def score(q):
        r = q.get("resolution") or ""
        if isinstance(r, str) and "x" in r:
            try:
                w, h = r.split("x", 1)
                px = int(w) * int(h)
            except ValueError:
                px = 0
        elif isinstance(r, (list, tuple)) and len(r) == 2:
            px = r[0] * r[1]
        else:
            px = q.get("height") or 0
        b = q.get("bandwidth") or 0
        pen = -1 if q.get("audio_only") else 0
        return (pen, px, b)

    q = max(quals, key=score)
    return q.get("url", ""), q


class MediaPreviewDialog(tk.Toplevel):
    """Visualizador de mídia embutido (estilo IDM "Play").

    Mostra um player, lista as qualidades e deixa escolher. Conteúdo com
    DRM (Widevine/FairPlay/PlayReady) é apenas detectado e recusado — não
    é possível baixar (DMCA §1201 / Lei 9.610).
    """

    def __init__(self, parent, url: str, config: AppConfig,
                 on_add=None):
        super().__init__(parent)
        self.title("Visualizador de mídia")
        self.geometry("720x560")
        self.transient(parent)
        self._url = url
        self._cfg = config
        self._on_add = on_add
        self._res = None

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        # ---- barra de URL + analisar
        top = ttk.Frame(body)
        top.pack(fill="x")
        self.var_url = tk.StringVar(value=url)
        ttk.Entry(top, textvariable=self.var_url).pack(
            side="left", fill="x", expand=True)
        self.btn_analyze = ttk.Button(top, text="Analisar",
                                      command=self._analyze)
        self.btn_analyze.pack(side="left", padx=(6, 0))

        # ---- área do player (container p/ o tkvideo/ffmpeg)
        self.player_host = tk.Frame(body, bg="#000", height=300)
        self.player_host.pack(fill="both", expand=False, pady=(10, 6))
        self.player_host.pack_propagate(False)
        self._player = None

        # ---- informativo (duração, tipo, criptografia)
        self.var_info = tk.StringVar(value="Clique em Analisar p/ carregar.")
        ttk.Label(body, textvariable=self.var_info, foreground="#555"
                  ).pack(anchor="w")

        # ---- lista de qualidades
        qf = ttk.LabelFrame(body, text="Qualidades", padding=6)
        qf.pack(fill="both", expand=True, pady=(8, 0))
        self.q_list = tk.Listbox(qf, exportselection=False, height=6,
                                 font=("Segoe UI", 10))
        self.q_list.pack(side="left", fill="both", expand=True)
        qs = ttk.Scrollbar(qf, orient="vertical", command=self.q_list.yview)
        self.q_list.configure(yscrollcommand=qs.set)
        qs.pack(side="right", fill="y")

        # ---- botões
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Fechar", command=self.destroy).pack(
            side="right", padx=(6, 0))
        self.btn_add = ttk.Button(btns, text="Baixar…", state="disabled",
                                  command=self._add)
        self.btn_add.pack(side="right")

        self.bind("<Escape>", lambda e: self.destroy())

    # ------------------------------------------------------------------ ações
    def _analyze(self):
        url = self.var_url.get().strip()
        if not url:
            return
        self.btn_analyze.configure(state="disabled")
        self.var_info.set("analisando…")
        self.update_idletasks()

        def _worker():
            from minidm.media import analyze

            try:
                res = analyze(url, user_agent=self._cfg.user_agent,
                              cookie=self._cfg.cookie, proxy=self._cfg.proxy,
                              use_ytdlp=True)
            except Exception as e:      # rede/parser: mostra o erro
                res = {"ok": False, "error": str(e)}
            self.after(0, lambda: self._finish_analyze(res))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_analyze(self, res: dict):
        self.btn_analyze.configure(state="normal")
        self._res = res
        if not res.get("ok"):
            if res.get("drm"):
                self.var_info.set(
                    f"DRM ({res['drm']}): conteúdo protegido — "
                    "não é possível baixar.")
                self.btn_add.configure(state="disabled")
            else:
                self.var_info.set(f"falha: {res.get('error')}")
            self._clear_quality()
            return

        dur = res.get("duration")
        kind = res.get("kind", "file")
        enc = "  ·  criptografado (AES-128)" if res.get("encrypted") else ""
        live = "  ·  AO VIVO" if res.get("live") else ""
        dur_s = f"{int(dur//60)}:{int(dur%60):02d}" if dur else "—"
        self.var_info.set(f"tipo {kind}  ·  duração {dur_s}{enc}{live}")

        # preenche a lista de qualidades
        self._clear_quality()
        for q in res.get("qualities") or []:
            lbl = q.get("label") or "—"
            bw = (f"  {q['bandwidth']/1e6:.1f} Mbps"
                  if q.get("bandwidth") else "")
            self.q_list.insert("end", f"{lbl}{bw}")
        if res.get("qualities"):
            self.q_list.selection_set(0)
        self.btn_add.configure(state="normal")
        self._start_preview(res)

    def _clear_quality(self):
        self.q_list.delete(0, "end")

    def _start_preview(self, res: dict):
        """Tenta reproduzir a melhor qualidade; se não der, só lista."""
        best = _best_playable(res)
        if not best:
            return
        play_url, q = best
        try:
            from minidm.player import play_in_frame

            self._player = play_in_frame(self.player_host, play_url,
                                         referer=self._url,
                                         user_agent=self._cfg.user_agent,
                                         proxy=self._cfg.proxy)
        except Exception as e:      # player indisponível: segue sem preview
            self._show_placeholder(f"preview indisponível ({e})")

    def _show_placeholder(self, msg: str):
        for w in self.player_host.winfo_children():
            w.destroy()
        ttk.Label(self.player_host, text=msg, foreground="#aaa",
                  background="#000", wraplength=600).pack(
            expand=True)

    def _add(self):
        if not (self._res and self._res.get("ok")):
            return
        idx = self.q_list.curselection()
        idx = idx[0] if idx else 0
        quals = self._res.get("qualities") or []
        if not (0 <= idx < len(quals)):
            return
        if self._on_add:
            self._on_add(self.var_url.get().strip(), quals[idx], self._res)
        self.destroy()


def main() -> int:
    app = MinidmApp()
    app.mainloop()
    return 0
