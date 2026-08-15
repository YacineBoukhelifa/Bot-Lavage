import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config, db, logic  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite3"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    db.init_db()
    connection = db.get_connection()
    yield connection
    connection.close()


def dt(h, m, date="2026-08-14"):
    y, mo, d = (int(x) for x in date.split("-"))
    return datetime(y, mo, d, h, m)


# --- Codes acceptes / alias Ligne 03 (spec v2 §2) ----------------------------

@pytest.mark.parametrize("alias", ["skd", "SKD", "l3", "L3", "03", "3"])
def test_ligne_03_aliases_resolve_to_skd(alias):
    reconnues, non_reconnues, _ = logic.parse_checkin(f"{alias} 90")
    assert reconnues == {"SKD": 90.0}
    assert non_reconnues == []


def test_ligne_03_nom_affiche_et_objectif():
    assert config.LIGNES["SKD"]["nom_affiche"] == "Ligne 03"
    assert config.LIGNES["SKD"]["objectif_horaire"] == 80


# --- Coefficients d'objectif par point + arrondi plancher (spec v2 §3.4) ----

def test_objectif_point_normal():
    assert logic.objectif_point(1, "A", "09:00") == 133
    assert logic.objectif_point(1, "S", "09:00") == 160
    assert logic.objectif_point(1, "SKD", "09:00") == 80


def test_objectif_point_pause_dejeuner_floor():
    assert logic.objectif_point(1, "A", "13:00") == 66  # 133*0.5=66.5 -> 66
    assert logic.objectif_point(1, "S", "13:00") == 80
    assert logic.objectif_point(1, "SKD", "13:00") == 40


def test_objectif_point_fin_de_poste_floor():
    assert logic.objectif_point(1, "A", "16:30") == 53  # 133*0.4=53.2 -> 53
    assert logic.objectif_point(1, "S", "16:30") == 64
    assert logic.objectif_point(1, "SKD", "16:30") == 32


def test_objectif_point_poste2_pause_repas():
    assert logic.objectif_point(2, "A", "19:30") == 66
    assert logic.objectif_point(2, "S", "19:30") == 80
    assert logic.objectif_point(2, "SKD", "19:30") == 40


# --- Time snapping par poste --------------------------------------------------

def test_snap_poste1_exact_and_tolerance():
    assert logic.snap_to_checkpoint(dt(11, 0), 1)["matched"] == "11:00"
    assert logic.snap_to_checkpoint(dt(10, 58), 1)["matched"] == "11:00"
    # 16:00 et 16:30 espaces de 30 min, tolerance +-15 -> pas d'ambiguite
    # tant qu'on ne tombe pas exactement sur la frontiere a 16:15.
    assert logic.snap_to_checkpoint(dt(16, 10), 1)["matched"] == "16:00"
    assert logic.snap_to_checkpoint(dt(16, 20), 1)["matched"] == "16:30"


def test_snap_poste1_outside_tolerance():
    result = logic.snap_to_checkpoint(dt(11, 20), 1)
    assert result["matched"] is None
    assert result["avant"] == "11:00"
    assert result["apres"] == "12:00"


def test_snap_poste2_uses_its_own_grid():
    assert logic.snap_to_checkpoint(dt(19, 32), 2)["matched"] == "19:30"
    assert logic.snap_to_checkpoint(dt(0, 30), 2)["matched"] == "00:30"


# --- Parsing -------------------------------------------------------------------

def test_parse_checkin_mixed():
    reconnues, non_reconnues, malformees = logic.parse_checkin("A 800\nS 660\nl3 90\nX 5\nfoo")
    assert reconnues == {"A": 800.0, "S": 660.0, "SKD": 90.0}
    assert non_reconnues == ["X"]
    assert malformees == ["foo"]


def test_parse_prod_space_comma_semicolon():
    r1, nr1, m1 = logic.parse_prod("a=800 s=660 l3=90")
    r2, nr2, m2 = logic.parse_prod("a=800, s=660; l3=90")
    assert r1 == r2 == {"A": 800.0, "S": 660.0, "SKD": 90.0}
    assert nr1 == nr2 == []
    assert m1 == m2 == []


def test_parse_prod_unrecognized_and_malformed():
    reconnues, non_reconnues, malformees = logic.parse_prod("a=800 zz=5 bad")
    assert reconnues == {"A": 800.0}
    assert non_reconnues == ["zz"]
    assert malformees == ["bad"]


# --- Check-in complet, poste 1 (grille + coefficients) ------------------------

def test_checkin_report_uses_point_objectif(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "chat1", date, 1, dt(12, 0), "A 532")  # 4 pts *133 = 532
    r = logic.process_checkin(conn, "chat1", date, 1, dt(13, 0), "A 598")  # +66, pause dejeuner
    assert "🎯 Objectif : 66 pcs" in r["message"]
    assert "✅ Production : 66 pcs" in r["message"]
    assert "📊 Écart : +0 pcs (+0%)" in r["message"]


def test_no_active_poste_refuses(conn):
    r = logic.process_checkin(conn, "chat1", "2026-08-14", 1, dt(11, 0), "A 100")
    assert r["status"] == "error"


def test_missing_line_not_in_report(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    r = logic.process_checkin(conn, "chat1", date, 1, dt(9, 0), "A 100")
    assert "Ligne Semi-auto" not in r["message"]
    assert "Ligne 03" not in r["message"]


def test_negative_delta_requires_confirmation(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "chat1", date, 1, dt(9, 0), "A 500")
    r = logic.process_checkin(conn, "chat1", date, 1, dt(10, 0), "A 480")
    assert r["status"] == "anomaly"
    confirm = logic.confirm_pending_anomalies(conn, "chat1")
    assert confirm["status"] == "report"
    assert "Production : -20 pcs" in confirm["message"]


def test_negative_delta_can_be_cancelled(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "chat1", date, 1, dt(9, 0), "A 500")
    logic.process_checkin(conn, "chat1", date, 1, dt(10, 0), "A 480")
    cancel = logic.cancel_pending_anomalies(conn, "chat1")
    assert cancel["status"] == "cancelled"
    r = logic.process_checkin(conn, "chat1", date, 1, dt(11, 0), "A 620")
    assert "Production : 120 pcs" in r["message"]


# --- /corriger et cascade ------------------------------------------------------

def test_corriger_recalculates_downstream(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "chat1", date, 1, dt(9, 0), "A 100")
    logic.process_checkin(conn, "chat1", date, 1, dt(10, 0), "A 250")  # production 150
    result = logic.corriger_checkpoint(conn, date, 1, "A", "09:00", 120, dt(9, 2))
    assert result["status"] == "report"
    cumul_10 = logic.get_dernier_cumul(conn, date, 1, "A")
    assert cumul_10 == 250
    row_10 = conn.execute(
        "SELECT production_heure FROM checkpoints WHERE date=? AND poste=1 AND code='A' AND heure='10:00'",
        (date,),
    ).fetchone()
    assert row_10["production_heure"] == 130  # 250 - 120, recalcule


# --- Poste 2 et passage de minuit (spec v2 §4) --------------------------------

def test_poste2_disabled_by_default_refuses_checkin(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    ctx = logic.resolve_production_context(conn, dt(18, 0))
    assert ctx.get("poste2_ferme") is True


def test_open_poste2_resets_cumul_without_anomaly(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "chat1", date, 1, dt(16, 0), "A 1100")
    logic.open_poste2(conn, date, dt(16, 35))
    ctx = logic.resolve_production_context(conn, dt(17, 30))
    assert ctx == {"date": date, "poste": 2}
    r = logic.process_checkin(conn, "chat1", date, 2, dt(17, 30), "A 145")
    assert r["status"] == "report"  # pas d'anomalie malgre la chute apparente 1100 -> 145
    assert "Production : 145 pcs" in r["message"]


def test_poste2_carries_production_date_across_midnight(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.open_poste2(conn, date, dt(16, 35))
    ctx = logic.resolve_production_context(conn, dt(0, 28, date="2026-08-15"))
    assert ctx == {"date": date, "poste": 2}


def test_start_day_next_morning_autocloses_stale_poste2(conn):
    logic.start_day(conn, "2026-08-14")
    logic.open_poste2(conn, "2026-08-14", dt(16, 35))
    logic.process_checkin(conn, "chat1", "2026-08-14", 2, dt(17, 30), "A 100")

    result = logic.start_day(conn, "2026-08-15")
    assert result["poste2_cloture_auto"] is not None
    assert result["poste2_cloture_auto"]["date"] == "2026-08-14"

    state = logic.get_poste_state(conn)
    assert state["poste2_ouvert"] == 0
    poste2_row = logic.get_poste(conn, "2026-08-14", 2)
    assert poste2_row["statut"] == "cloture"


def test_anomaly_guard_not_triggered_by_negative_input_within_same_poste(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "chat1", date, 1, dt(9, 0), "A 500")
    r = logic.process_checkin(conn, "chat1", date, 1, dt(10, 0), "A 400")
    assert r["status"] == "anomaly"  # vraie baisse a l'interieur du meme poste -> toujours signalee


# --- Synthese de fin de poste (spec v2 §5) ------------------------------------

def _remplir_poste1_complet(conn, date):
    logic.start_day(conn, date)
    valeurs = {
        "09:00": (133, 160, 80), "10:00": (266, 320, 160), "11:00": (399, 480, 240),
        "12:00": (532, 640, 320), "13:00": (598, 720, 360), "14:00": (731, 880, 440),
        "15:00": (864, 1040, 520), "16:00": (997, 1200, 600), "16:30": (1180, 1240, 632),
    }
    for heure, (a, s, skd) in valeurs.items():
        h, m = (int(x) for x in heure.split(":"))
        logic.process_checkin(conn, "chat1", date, 1, dt(h, m, date), f"A {a}\nS {s}\nL3 {skd}")


def test_synthese_de_poste_complet(conn):
    date = "2026-08-14"
    _remplir_poste1_complet(conn, date)
    stats = logic.compute_poste_stats(conn, date, 1)
    assert stats["cumul_final_non_saisi"] is False
    lignes = {l["code"]: l for l in stats["lignes"]}
    assert lignes["A"]["total"] == 1180
    assert lignes["A"]["objectif"] == 1050  # 7*133 + 66 (pause) + 53 (fin de poste)
    assert lignes["SKD"]["total"] == 632
    assert round(stats["total_general"]) == 1180 + 1240 + 632

    texte = logic.format_synthese(stats)
    assert "SYNTHÈSE DE FIN DE POSTE" in texte
    assert "Poste 1 : 08:00 → 16:30" in texte
    assert "LIGNE AUTO" in texte
    assert "Moyenne horaire" in texte


def test_synthese_signale_cumul_final_non_saisi(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "chat1", date, 1, dt(9, 0), "A 133")
    stats = logic.compute_poste_stats(conn, date, 1)
    assert stats["cumul_final_non_saisi"] is True


def test_is_poste_pret_a_cloturer_detects_all_active_lines_final(conn):
    date = "2026-08-14"
    _remplir_poste1_complet(conn, date)
    assert logic.is_poste_pret_a_cloturer(conn, date, 1) is True


def test_consolidated_stats_only_meaningful_if_poste2_ran(conn):
    date = "2026-08-14"
    _remplir_poste1_complet(conn, date)
    logic.close_poste(conn, date, 1, dt(16, 30))
    logic.open_poste2(conn, date, dt(16, 35))
    logic.process_checkin(conn, "chat1", date, 2, dt(17, 30), "A 145")
    logic.close_poste(conn, date, 2, dt(17, 35))

    consolidated = logic.compute_consolidated_stats(conn, date)
    lignes = {l["code"]: l for l in consolidated["lignes"]}
    assert lignes["A"]["total"] == 1180 + 145
    assert "Poste 1 + 2" in consolidated["libelle"]


# --- Recap ---------------------------------------------------------------------

def test_recap(conn):
    date = "2026-08-14"
    logic.start_day(conn, date)
    logic.process_checkin(conn, "chat1", date, 1, dt(9, 0), "A 133\nS 160")
    recap = logic.format_recap(conn, date, 1)
    assert "Ligne Auto" in recap
    assert "cumul=133" in recap
    assert "Ligne 03" not in recap
