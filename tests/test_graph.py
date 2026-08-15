import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config, db, graph, logic  # noqa: E402

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.sqlite3"))
    db.init_db()
    connection = db.get_connection()
    yield connection
    connection.close()


def dt(h, m, date="2026-08-14"):
    y, mo, d = (int(x) for x in date.split("-"))
    return datetime(y, mo, d, h, m)


def test_chart_is_valid_png_with_all_lines(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "c", date, 1, dt(9, 0), "A 133\nS 160\nL3 80")
    logic.process_checkin(conn, "c", date, 1, dt(10, 0), "A 266\nS 320\nL3 160")

    stats = logic.compute_poste_stats(conn, date, 1)
    png = graph.generate_realisation_chart(stats)
    assert png.startswith(PNG_MAGIC)
    assert len(png) > 1000


def test_chart_handles_partially_active_line(conn):
    """Une ligne qui ne demarre qu'a 11:00 doit produire un graphique valide
    (les points 09:00/10:00 sont des trous NaN, pas relies au reste)."""
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "c", date, 1, dt(9, 0), "A 133")
    logic.process_checkin(conn, "c", date, 1, dt(11, 0), "A 399\nS 160")

    stats = logic.compute_poste_stats(conn, date, 1)
    ligne_s = next(l for l in stats["lignes"] if l["code"] == "S")
    valeurs = [p["realisation_pct"] for p in ligne_s["points"]]
    assert valeurs[0] is None  # 09:00 : S pas encore demarree -> trou
    assert valeurs[1] is None  # 10:00 : idem
    assert valeurs[2] is not None  # 11:00 : premiere valeur de S

    png = graph.generate_realisation_chart(stats)
    assert png.startswith(PNG_MAGIC)


def test_chart_single_line_only(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "c", date, 1, dt(9, 0), "A 133")
    stats = logic.compute_poste_stats(conn, date, 1)
    png = graph.generate_realisation_chart(stats)
    assert png.startswith(PNG_MAGIC)
