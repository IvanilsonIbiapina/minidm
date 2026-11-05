"""Paletas de tema da GUI (claro/escuro) — um único lugar com as cores.

`gui.py` lê o `Palette` escolhido e aplica via `ttk.Style` + tags do
Treeview + widgets `tk` puros (Listbox/fundo de janela). Trocar o tema em
tempo de execução é só reaplicar (`MinidmApp._apply_theme`).

`resolve("auto")` hoje cai no claro (deteccao de tema do SO pode entrar
depois); nome desconhecido também cai no claro — a GUI nunca fica sem
paleta.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    window: str          # fundo da janela
    frame: str           # toolbar/sidebar/barra de status
    surface: str         # painéis/dialogs
    text: str            # texto principal
    text_dim: str        # texto secundário/rótulos
    accent: str          # azul de progresso/links
    accent_dark: str     # azul para texto sobre claro
    success: str
    warn: str
    error: str
    row_even: str        # listra par do Treeview
    row_odd: str         # listra ímpar do Treeview
    heading_bg: str
    heading_fg: str
    entry_bg: str
    entry_fg: str
    list_bg: str
    list_fg: str
    select_bg: str
    progress_trough: str
    border: str


LIGHT = Palette(
    name="light",
    window="#f0f2f5", frame="#e8eaee", surface="#ffffff",
    text="#1b1d21", text_dim="#5f6470",
    accent="#0a7bd8", accent_dark="#0a6bb5",
    success="#1a8a3c", warn="#c07a00", error="#c02020",
    row_even="#ffffff", row_odd="#f2f5f9",
    heading_bg="#e8eaee", heading_fg="#3a3d44",
    entry_bg="#ffffff", entry_fg="#1b1d21",
    list_bg="#f7f8fa", list_fg="#1b1d21", select_bg="#cfe4f7",
    progress_trough="#d7dbe2", border="#c9ced6",
)

DARK = Palette(
    name="dark",
    window="#1e2024", frame="#26292e", surface="#2c3036",
    text="#e7e9ee", text_dim="#9aa0ab",
    accent="#3d9be9", accent_dark="#5cb0f5",
    success="#3fbe6a", warn="#e0a030", error="#e05555",
    row_even="#2a2d33", row_odd="#242730",
    heading_bg="#26292e", heading_fg="#c3c8d2",
    entry_bg="#1a1c20", entry_fg="#e7e9ee",
    list_bg="#24272c", list_fg="#e7e9ee", select_bg="#33465c",
    progress_trough="#3a3e46", border="#3a3e46",
)

THEMES = {"light": LIGHT, "dark": DARK}


def resolve(name: str) -> Palette:
    """Paleta pelo nome ("light"/"dark"/"auto"/desconhecido -> claro)."""
    return THEMES.get((name or "").strip().lower(), LIGHT)


def next_theme(name: str) -> str:
    """Alterna o nome do tema (usado pelo botão da toolbar)."""
    return "dark" if resolve(name).name == "light" else "light"
