"""Dashboard TUI (rich): todas as tarefas, barras, velocidade e ETA ao vivo."""

from __future__ import annotations

import time

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from minidm.meta import TaskState

STATE_STYLE = {
    TaskState.QUEUED: "dim",
    TaskState.DOWNLOADING: "cyan",
    TaskState.PAUSED: "yellow",
    TaskState.DONE: "green",
    TaskState.ERROR: "red",
}


def human(n: int | None) -> str:
    if n is None:
        return "?"
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.1f} {unit}" if unit != "B" else f"{int(x)} B"
        x /= 1024
    return f"{x:.1f} TB"


def eta(received: int, total: int | None, speed: float) -> str:
    if not total or speed <= 0:
        return "--:--"
    secs = (total - received) / speed
    if secs > 3600 * 24:
        return ">1d"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def render(rows: list[dict], throttle_bps: int = 0) -> Panel:
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("ID", style="dim", width=8)
    table.add_column("ARQUIVO", max_width=44, no_wrap=True)
    table.add_column("ESTADO", width=12)
    table.add_column("PROGRESSO", width=24)
    table.add_column("%", justify="right", width=6)
    table.add_column("RECEBIDO", justify="right", width=11)
    table.add_column("VEL", justify="right", width=11)
    table.add_column("ETA", justify="right", width=8)
    table.add_column("SEGS", justify="center", width=7)

    total_speed = 0.0
    for r in rows:
        # HLS ao vivo: total desconhecido — não mostra % nem ETA enganoso
        is_live = bool(r.get("live")) and \
            r["state"] == TaskState.DOWNLOADING
        pct = "  --" if is_live else f"{r['progress']*100:5.1f}"
        filled = int(r["progress"] * 22)
        # ASCII puro: renderiza em qualquer console (cp1252, UTF-8, pipe)
        bar = "ao vivo (sem fim)" if is_live else \
            "#" * filled + "-" * (22 - filled)
        style = STATE_STYLE.get(r["state"], "")
        speed = r.get("speed", 0.0) or 0.0
        total_speed += speed
        extra = ""
        if r["state"] == TaskState.ERROR and r.get("error"):
            extra = f"\n[red]   > {r['error'][:100]}[/]"
        if r.get("signature_status"):
            mark = {"Valid": "green", "NotSigned": "yellow"}.get(
                r["signature_status"], "red")
            extra += f"\n[dim]   > assinatura: [{mark}]{r['signature_status']}[/] "
            signer = (r.get("signature_signer") or "")[:60]
            if signer:
                extra += f"{signer}[/]"
        segs = (f"{r['segments_done']}/{r['segments_total']}"
                if r["segments_total"]
                else (str(r.get("segments_done") or "")
                      if r.get("live") else ""))
        table.add_row(
            r["id"],
            r["filename"],
            f"[{style}]{r['state']}[/]",
            f"[{style}]{bar}[/]",
            pct,
            human(r["received"]),
            f"{human(int(speed))}/s" if speed else "",
            eta(r["received"], r["total"], speed),
            segs,
        )
        if extra:
            table.add_row("", Text.from_markup(extra), "", "", "", "", "", "", "")

    header = Text()
    header.append(f" minidm  •  {len(rows)} tarefa(s)", style="bold")
    header.append(f"  •  total {human(int(total_speed))}/s", style="cyan")
    if throttle_bps:
        header.append(f"  •  limite {human(throttle_bps)}/s", style="yellow")
    return Panel(Group(header, table), title="minidm", border_style="blue")


def live_dashboard(scheduler, throttle_bps: int = 0,
                   refresh: float = 0.5) -> None:
    """Bloca exibindo o dashboard até Ctrl+C."""
    with Live(render(scheduler.snapshot(), throttle_bps),
              refresh_per_second=1 / refresh, transient=False) as live:
        try:
            while True:
                live.update(render(scheduler.snapshot(), throttle_bps))
                time.sleep(refresh)
        except KeyboardInterrupt:
            pass
