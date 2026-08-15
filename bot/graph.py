"""Génération du graphique de taux de réalisation (spec v2 §6.2).

Consomme directement la structure retournée par
`logic.compute_poste_stats` / `logic.compute_consolidated_stats` — ce module
ne touche jamais la base de données.
"""
import io

import matplotlib

matplotlib.use("Agg")  # pas d'interface graphique — obligatoire sur serveur
import matplotlib.pyplot as plt
import numpy as np

from . import config

LINE_STYLES = {"solid": "-", "dashed": "--", "dotted": ":"}


def generate_realisation_chart(stats):
    """Retourne les octets PNG (1200x700) du graphique 'taux de réalisation
    (%) par point de controle', une courbe par ligne active, points inactifs
    representes par un trou (NaN) et non un zero ni un segment relie."""
    fig_w, fig_h = config.GRAPH_SIZE_PX
    fig, ax = plt.subplots(
        figsize=(fig_w / config.GRAPH_DPI, fig_h / config.GRAPH_DPI), dpi=config.GRAPH_DPI
    )

    points_axe = stats["points_axe"]
    x = list(range(len(points_axe)))

    for ligne in stats["lignes"]:
        code = ligne["code"]
        y = [
            p["realisation_pct"] if p["realisation_pct"] is not None else np.nan
            for p in ligne["points"]
        ]
        ax.plot(
            x, y,
            label=ligne["nom"],
            color=config.GRAPH_COLORS.get(code, "#999999"),
            linestyle=LINE_STYLES.get(config.GRAPH_DASHES.get(code, "solid"), "-"),
            linewidth=2,
            marker="o",
            markersize=8,
            markeredgewidth=2,
            markeredgecolor="white",
            solid_capstyle="round",
            solid_joinstyle="round",
        )

    ax.axhline(100, color="#999999", linestyle=":", linewidth=1.5, zorder=0)

    ax.set_xticks(x)
    ax.set_xticklabels(points_axe, rotation=45, ha="right")
    ax.set_ylim(0, 160)
    ax.set_ylabel("Taux de réalisation (%)")
    ax.set_title(f"Taux de réalisation — BU Lavage — {stats['libelle']} — {_fmt_date(stats['date'])}")
    ax.legend(loc="upper right")
    ax.grid(True, axis="y", alpha=0.3)

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _fmt_date(date):
    y, m, d = date.split("-")
    return f"{d}/{m}/{y}"
