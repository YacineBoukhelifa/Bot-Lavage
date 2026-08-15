"""Logique metier: rattachement horaire, calcul des deltas, synthese de fin
de poste, formatage des messages.

Ce module est pur (pas d'acces reseau, pas de generation de fichier/image) —
la persistance (SQLite) est injectee via l'objet `conn` (sqlite3.Connection),
et les couches graphique/Excel/Google consomment les structures qu'il
calcule (voir `compute_poste_stats`) sans jamais reinterroger la logique
metier elles-memes.
"""
import json
import math
import re
from datetime import datetime, timedelta

from . import config

ETAT_EXPIRATION_MINUTES = 20

LINE_CODE_RE = re.compile(r"^([A-Za-z0-9]+)\s+(-?\d+(?:[.,]\d+)?)$")
PROD_TOKEN_RE = re.compile(r"^([A-Za-z0-9]+)=(-?\d+(?:[.,]\d+)?)$")


def _resolve_code(code_raw):
    return config.CODES_ACCEPTES.get(code_raw.upper())


# ---------------------------------------------------------------------------
# Parsing des messages entrants
# ---------------------------------------------------------------------------

def parse_checkin(text):
    """Parse un message en lignes 'CODE valeur' (format libre v1, toujours
    accepte en prive, et reutilise pour les commandes a une seule ligne
    /a, /s, /l3 une fois le nom de commande retire par l'appelant).

    Retourne (reconnues: {code_interne: float}, non_reconnues: [code brut],
              malformees: [ligne brute]).
    """
    reconnues = {}
    non_reconnues = []
    malformees = []

    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = LINE_CODE_RE.match(line)
        if not m:
            malformees.append(raw_line)
            continue
        code_raw, value_raw = m.group(1), m.group(2)
        code = _resolve_code(code_raw)
        value = float(value_raw.replace(",", "."))
        if code:
            reconnues[code] = value
        else:
            non_reconnues.append(code_raw)

    return reconnues, non_reconnues, malformees


def parse_prod(text):
    """Parse la syntaxe multi-lignes '/prod a=800 s=660 l3=90' (separateurs
    espace, virgule ou point-virgule). Meme contrat de retour que
    `parse_checkin`."""
    reconnues = {}
    non_reconnues = []
    malformees = []

    normalise = text.strip().replace(",", " ").replace(";", " ")
    for token in normalise.split():
        m = PROD_TOKEN_RE.match(token)
        if not m:
            malformees.append(token)
            continue
        code_raw, value_raw = m.group(1), m.group(2)
        code = _resolve_code(code_raw)
        value = float(value_raw.replace(",", "."))
        if code:
            reconnues[code] = value
        else:
            non_reconnues.append(code_raw)

    return reconnues, non_reconnues, malformees


# ---------------------------------------------------------------------------
# Grilles horaires (postes) et rattachement a un point de controle
# ---------------------------------------------------------------------------

def _poste_config(poste):
    return config.POSTES[poste]


def _points(poste):
    return _poste_config(poste)["points"]


def _to_minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _point_index(poste, heure):
    for i, p in enumerate(_points(poste)):
        if p["heure"] == heure:
            return i
    raise ValueError(f"heure inconnue pour le poste {poste} : {heure}")


def point_coef(poste, heure):
    return _points(poste)[_point_index(poste, heure)]["coef"]


def objectif_point(poste, code, heure):
    """Objectif arrondi a l'entier inferieur pour ce point de controle
    (spec v2 §3.4) : floor(objectif_horaire_ligne * coef_du_point)."""
    objectif_horaire = config.LIGNES[code]["objectif_horaire"]
    coef = point_coef(poste, heure)
    return math.floor(objectif_horaire * coef)


def heure_debut_periode(poste, heure):
    idx = _point_index(poste, heure)
    if idx == 0:
        return _poste_config(poste)["debut"]
    return _points(poste)[idx - 1]["heure"]


def is_final_point(poste, heure):
    return bool(_points(poste)[_point_index(poste, heure)].get("final"))


def final_heure(poste):
    for p in _points(poste):
        if p.get("final"):
            return p["heure"]
    return None


def snap_to_checkpoint(dt, poste, checkpoints=None, tolerance_minutes=None):
    """Rattache un horodatage au point de controle prevu le plus proche pour
    le poste donne.

    Retourne {"matched": "11:00"} ou {"matched": None, "avant": .., "apres": ..}.
    """
    poste_cfg = _poste_config(poste)
    checkpoints = checkpoints or [p["heure"] for p in poste_cfg["points"]]
    tolerance_minutes = (
        poste_cfg["tolerance_minutes"] if tolerance_minutes is None else tolerance_minutes
    )

    now_min = dt.hour * 60 + dt.minute
    best_cp = None
    best_dist = None
    for cp in checkpoints:
        dist = abs(_to_minutes(cp) - now_min)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_cp = cp

    if best_dist is not None and best_dist <= tolerance_minutes:
        return {"matched": best_cp}

    avant = None
    apres = None
    for cp in checkpoints:
        cp_min = _to_minutes(cp)
        if cp_min <= now_min:
            avant = cp
        elif apres is None:
            apres = cp
    return {"matched": None, "avant": avant, "apres": apres}


# ---------------------------------------------------------------------------
# Etat des postes (ouverture / cloture / passage de minuit)
# ---------------------------------------------------------------------------

def get_poste(conn, date, poste):
    return conn.execute(
        "SELECT * FROM postes WHERE date = ? AND poste = ?", (date, poste)
    ).fetchone()


def get_poste_state(conn):
    return conn.execute("SELECT * FROM poste_state WHERE id = 1").fetchone()


def start_day(conn, date, dt=None):
    """Ouvre le poste 1 pour `date`, remet ses cumuls a 0. Si un poste 2
    d'une date de production anterieure est reste ouvert (oubli de cloture
    la veille), il est cloture automatiquement au prealable (spec v2 §4.3)
    et sa synthese est retournee pour etre postee dans le groupe."""
    dt = dt or datetime.now()
    poste2_cloture_auto = None
    state = get_poste_state(conn)
    if state["poste2_ouvert"] and state["poste2_date"] != date:
        poste2_cloture_auto = close_poste(conn, state["poste2_date"], 2, dt)

    conn.execute("DELETE FROM checkpoints WHERE date = ? AND poste = 1", (date,))
    conn.execute("DELETE FROM pending_anomalies WHERE date = ? AND poste = 1", (date,))
    conn.execute(
        "INSERT INTO postes (date, poste, statut, ouvert_a) VALUES (?, 1, 'actif', ?) "
        "ON CONFLICT(date, poste) DO UPDATE SET statut='actif', ouvert_a=excluded.ouvert_a",
        (date, dt.isoformat()),
    )
    conn.commit()
    return {"poste2_cloture_auto": poste2_cloture_auto}


def open_poste2(conn, date, dt):
    """Active le poste 2 pour la journee en cours (spec v2 §4.1). Valable un
    seul jour ; n'est jamais reconduit automatiquement au lendemain."""
    conn.execute("DELETE FROM checkpoints WHERE date = ? AND poste = 2", (date,))
    conn.execute("DELETE FROM pending_anomalies WHERE date = ? AND poste = 2", (date,))
    conn.execute(
        "INSERT INTO postes (date, poste, statut, ouvert_a) VALUES (?, 2, 'actif', ?) "
        "ON CONFLICT(date, poste) DO UPDATE SET statut='actif', ouvert_a=excluded.ouvert_a",
        (date, dt.isoformat()),
    )
    conn.execute(
        "UPDATE poste_state SET poste2_ouvert = 1, poste2_date = ?, poste2_ouvert_a = ? WHERE id = 1",
        (date, dt.isoformat()),
    )
    conn.commit()
    return {"date": date, "poste": 2}


def resolve_production_context(conn, dt):
    """Determine (date de production, poste) pour un check-in sans poste
    explicite. Si le poste 2 est ouvert, tout s'y rattache (y compris apres
    minuit — spec v2 §4.3). Sinon, poste 1 pour la date du jour, sauf en
    dehors de sa fenetre (+ tolerance) ou la saisie est refusee tant que
    /poste2 n'a pas ete lance (spec v2 §4.1)."""
    state = get_poste_state(conn)
    if state["poste2_ouvert"]:
        return {"date": state["poste2_date"], "poste": 2}

    poste1_cfg = config.POSTE_1
    fin_min = _to_minutes(poste1_cfg["fin"]) + poste1_cfg["tolerance_minutes"]
    now_min = dt.hour * 60 + dt.minute
    if now_min > fin_min:
        return {"date": None, "poste": None, "poste2_ferme": True}
    return {"date": dt.strftime("%Y-%m-%d"), "poste": 1}


def _mark_synthese_envoyee(conn, date, poste, dt):
    conn.execute(
        "INSERT OR REPLACE INTO syntheses_envoyees (date, poste, envoyee_a) VALUES (?, ?, ?)",
        (date, poste, dt.isoformat()),
    )


def synthese_deja_envoyee(conn, date, poste):
    row = conn.execute(
        "SELECT 1 FROM syntheses_envoyees WHERE date = ? AND poste = ?", (date, poste)
    ).fetchone()
    return row is not None


def close_poste(conn, date, poste, dt):
    """Cloture un poste (peu importe le declencheur : point final complet,
    heure de fin atteinte, ou /fin) et retourne sa synthese calculee. Op
    idempotente : si deja cloturee, recalcule quand meme la synthese (pour
    /fin renvoye deux fois) sans dupliquer l'enregistrement."""
    stats = compute_poste_stats(conn, date, poste)
    conn.execute(
        "INSERT INTO postes (date, poste, statut) VALUES (?, ?, 'cloture') "
        "ON CONFLICT(date, poste) DO UPDATE SET statut='cloture'",
        (date, poste),
    )
    if poste == 2:
        state = get_poste_state(conn)
        if state["poste2_ouvert"] and state["poste2_date"] == date:
            conn.execute(
                "UPDATE poste_state SET poste2_ouvert = 0, poste2_date = NULL, poste2_ouvert_a = NULL WHERE id = 1"
            )
    _mark_synthese_envoyee(conn, date, poste, dt)
    conn.commit()
    return stats


def is_poste_pret_a_cloturer(conn, date, poste):
    """Vrai si le point final vient d'etre enregistre pour toutes les lignes
    actives de ce poste (declenchement immediat, spec v2 §5.1)."""
    heure_finale = final_heure(poste)
    if heure_finale is None:
        return False
    codes_actifs = _codes_actifs(conn, date, poste)
    if not codes_actifs:
        return False
    for code in codes_actifs:
        row = conn.execute(
            "SELECT 1 FROM checkpoints WHERE date=? AND poste=? AND code=? AND heure=?",
            (date, poste, code, heure_finale),
        ).fetchone()
        if row is None:
            return False
    return True


def postes_a_cloturer_automatiquement(conn, dt):
    """Liste des (date, poste) dont l'heure de fin est depassee, actifs, et
    dont la synthese n'a pas encore ete envoyee — verifie a chaque webhook
    en l'absence de cron externe fiable (voir DEPLOY.md)."""
    resultats = []

    date1 = dt.strftime("%Y-%m-%d")
    poste1 = get_poste(conn, date1, 1)
    fin1_min = _to_minutes(config.POSTE_1["fin"])
    now_min = dt.hour * 60 + dt.minute
    if (
        poste1 is not None
        and poste1["statut"] == "actif"
        and now_min >= fin1_min
        and not synthese_deja_envoyee(conn, date1, 1)
    ):
        resultats.append((date1, 1))

    state = get_poste_state(conn)
    if state["poste2_ouvert"]:
        date2 = state["poste2_date"]
        poste2 = get_poste(conn, date2, 2)
        fin2_min = _to_minutes(config.POSTE_2["fin"])
        depasse = dt.strftime("%Y-%m-%d") > date2 and now_min >= fin2_min
        if (
            poste2 is not None
            and poste2["statut"] == "actif"
            and depasse
            and not synthese_deja_envoyee(conn, date2, 2)
        ):
            resultats.append((date2, 2))

    return resultats


# ---------------------------------------------------------------------------
# Cumuls et enregistrement des points de controle
# ---------------------------------------------------------------------------

def get_cumul_precedent(conn, date, poste, code, heure):
    """Dernier cumul enregistre pour (ligne, date, poste) avant `heure` (0 si
    aucun). La portee stricte a (date, poste) est ce qui fait repartir le
    compteur MES a 0 a l'ouverture du poste 2, sans anomalie (spec v2 §4.4)."""
    idx = _point_index(poste, heure)
    points_avant = [p["heure"] for p in _points(poste)[:idx]]
    if not points_avant:
        return 0.0
    placeholders = ",".join("?" for _ in points_avant)
    rows = conn.execute(
        f"SELECT cumul_envoye, heure FROM checkpoints "
        f"WHERE date = ? AND poste = ? AND code = ? AND heure IN ({placeholders}) "
        f"ORDER BY heure DESC",
        (date, poste, code, *points_avant),
    ).fetchall()
    if not rows:
        return 0.0
    rows_sorted = sorted(rows, key=lambda r: _point_index(poste, r["heure"]))
    return rows_sorted[-1]["cumul_envoye"]


def get_dernier_cumul(conn, date, poste, code):
    rows = conn.execute(
        "SELECT cumul_envoye, heure FROM checkpoints WHERE date = ? AND poste = ? AND code = ?",
        (date, poste, code),
    ).fetchall()
    if not rows:
        return None
    rows_sorted = sorted(rows, key=lambda r: _point_index(poste, r["heure"]))
    return rows_sorted[-1]["cumul_envoye"]


def line_has_any_checkpoint(conn, date, poste, code):
    row = conn.execute(
        "SELECT 1 FROM checkpoints WHERE date = ? AND poste = ? AND code = ? LIMIT 1",
        (date, poste, code),
    ).fetchone()
    return row is not None


def _codes_actifs(conn, date, poste):
    return [c for c in config.ORDRE_AFFICHAGE if line_has_any_checkpoint(conn, date, poste, c)]


def save_checkpoint(
    conn, date, poste, code, heure, cumul_nouveau, horodatage_reel, cumul_precedent,
    saisi_par_id=None, saisi_par_nom=None,
):
    coef = point_coef(poste, heure)
    objectif = objectif_point(poste, code, heure)
    production = cumul_nouveau - cumul_precedent
    ecart = production - objectif
    ecart_pct = (ecart / objectif * 100) if objectif else None
    conn.execute(
        "INSERT INTO checkpoints "
        "(date, poste, code, heure, cumul_envoye, production_heure, coef, objectif, ecart, ecart_pct, "
        " horodatage_reel, saisi_par_id, saisi_par_nom) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(date, poste, code, heure) DO UPDATE SET "
        "cumul_envoye=excluded.cumul_envoye, production_heure=excluded.production_heure, "
        "coef=excluded.coef, objectif=excluded.objectif, ecart=excluded.ecart, ecart_pct=excluded.ecart_pct, "
        "horodatage_reel=excluded.horodatage_reel, saisi_par_id=excluded.saisi_par_id, "
        "saisi_par_nom=excluded.saisi_par_nom",
        (
            date, poste, code, heure, cumul_nouveau, production, coef, objectif, ecart, ecart_pct,
            horodatage_reel, saisi_par_id, saisi_par_nom,
        ),
    )
    conn.commit()
    return {
        "code": code,
        "nom": config.LIGNES[code]["nom_affiche"],
        "production": production,
        "objectif": objectif,
        "ecart": ecart,
        "ecart_pct": ecart_pct,
        "cumul": cumul_nouveau,
    }


# ---------------------------------------------------------------------------
# Anomalies (cumul < cumul precedent)
# ---------------------------------------------------------------------------

def queue_anomaly(
    conn, chat_id, date, poste, heure, code, cumul_precedent, cumul_nouveau, horodatage_reel,
    saisi_par_id=None, saisi_par_nom=None,
):
    conn.execute(
        "INSERT INTO pending_anomalies "
        "(chat_id, date, poste, heure, code, cumul_precedent, cumul_nouveau, horodatage_reel, "
        " saisi_par_id, saisi_par_nom) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, date, poste, heure, code, cumul_precedent, cumul_nouveau, horodatage_reel,
         saisi_par_id, saisi_par_nom),
    )
    conn.commit()


def get_pending_anomalies(conn, chat_id):
    return conn.execute(
        "SELECT * FROM pending_anomalies WHERE chat_id = ? ORDER BY id", (chat_id,)
    ).fetchall()


def clear_pending_anomalies(conn, chat_id):
    conn.execute("DELETE FROM pending_anomalies WHERE chat_id = ?", (chat_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Traitement complet d'un check-in
# ---------------------------------------------------------------------------

def process_checkin_values(
    conn, chat_id, date, poste, dt, valeurs, non_reconnues=None, malformees=None,
    saisi_par_id=None, saisi_par_nom=None, heure=None,
):
    """Coeur du traitement d'un check-in a partir d'un dict deja parse
    {code_interne: valeur}. Utilise par `process_checkin` (format libre /
    commandes a une ligne) et directement par `/prod` (app.py).

    `heure` : si deja connue (saisie guidee bot v2 — le point a ete
    determine au moment du `/saisir`, pas a la validation, qui peut
    survenir plusieurs minutes plus tard), l'utiliser directement plutot
    que de re-snapper sur `dt` — sinon un `/saisir` a 09:00 valide a 09:04
    re-snapperait sur l'horloge de la validation, pas de la saisie."""
    non_reconnues = non_reconnues or []
    malformees = malformees or []

    poste_row = get_poste(conn, date, poste)
    if poste_row is None or poste_row["statut"] != "actif":
        return {
            "status": "error",
            "message": (
                "Aucun poste actif pour cette date. "
                "Envoyez /start_day pour demarrer le poste 1 (ou /poste2 pour le poste 2)."
            ),
        }

    if not valeurs and not non_reconnues and not malformees:
        return {
            "status": "error",
            "message": "Message non reconnu. Envoyez /help pour voir le format attendu.",
        }

    if heure is None:
        snap = snap_to_checkpoint(dt, poste)
        if snap["matched"] is None:
            avant = snap["avant"] or "aucun"
            apres = snap["apres"] or "aucun"
            return {
                "status": "error",
                "message": (
                    f"Aucun point de controle proche de cet horaire "
                    f"(recu a {dt.strftime('%H:%M')}) — dernier point : {avant}, prochain : {apres}."
                ),
            }
        heure = snap["matched"]
    horodatage_reel = dt.isoformat()

    resultats = []
    anomalies = []
    for code, cumul_nouveau in valeurs.items():
        cumul_precedent = get_cumul_precedent(conn, date, poste, code, heure)
        production = cumul_nouveau - cumul_precedent
        if production < 0:
            queue_anomaly(
                conn, chat_id, date, poste, heure, code, cumul_precedent, cumul_nouveau,
                horodatage_reel, saisi_par_id, saisi_par_nom,
            )
            anomalies.append((code, cumul_precedent, cumul_nouveau))
        else:
            resultats.append(
                save_checkpoint(
                    conn, date, poste, code, heure, cumul_nouveau, horodatage_reel,
                    cumul_precedent, saisi_par_id, saisi_par_nom,
                )
            )

    lines_out = []
    if resultats:
        lines_out.append(format_report(date, poste, heure, resultats))
    if anomalies:
        lines_out.append(format_anomaly_warning(anomalies))
    if non_reconnues:
        lines_out.append(
            "⚠️ Code(s) de ligne non reconnu(s), ignore(s) : " + ", ".join(non_reconnues)
        )
    if malformees:
        lines_out.append(
            "⚠️ Ligne(s) mal formee(s), ignoree(s) : " + " / ".join(malformees)
        )

    # Toute anomalie en attente doit faire partir le clavier de confirmation,
    # meme si d'autres lignes de la meme frappe ont ete enregistrees sans
    # probleme (le texte contient deja les deux parties) — sinon, en groupe,
    # une anomalie melangee resterait bloquee sans aucun moyen de la
    # confirmer/annuler (le texte "oui"/"non" ne passe pas le privacy mode).
    status = "anomaly" if anomalies else "report"
    pret_a_cloturer = bool(resultats) and is_poste_pret_a_cloturer(conn, date, poste)
    return {
        "status": status,
        "message": "\n\n".join(lines_out),
        "date": date,
        "poste": poste,
        "pret_a_cloturer": pret_a_cloturer,
    }


def process_checkin(conn, chat_id, date, poste, dt, text, saisi_par_id=None, saisi_par_nom=None):
    """Traite un message de check-in au format libre 'CODE valeur' (une
    ligne par code). Utilise en prive et pour les commandes /a, /s, /l3 une
    fois le nom de commande retire par l'appelant."""
    reconnues, non_reconnues, malformees = parse_checkin(text)
    return process_checkin_values(
        conn, chat_id, date, poste, dt, reconnues, non_reconnues, malformees,
        saisi_par_id, saisi_par_nom,
    )


def confirm_pending_anomalies(conn, chat_id):
    pending = get_pending_anomalies(conn, chat_id)
    if not pending:
        return {"status": "error", "message": "Aucune anomalie en attente de confirmation."}

    resultats = []
    date = pending[0]["date"]
    poste = pending[0]["poste"]
    heure = pending[0]["heure"]
    for row in pending:
        resultats.append(
            save_checkpoint(
                conn, row["date"], row["poste"], row["code"], row["heure"],
                row["cumul_nouveau"], row["horodatage_reel"], row["cumul_precedent"],
                row["saisi_par_id"], row["saisi_par_nom"],
            )
        )
    clear_pending_anomalies(conn, chat_id)
    message = "✅ Valeur(s) confirmee(s) et enregistree(s).\n\n" + format_report(date, poste, heure, resultats)
    pret_a_cloturer = is_poste_pret_a_cloturer(conn, date, poste)
    return {
        "status": "report", "message": message, "date": date, "poste": poste,
        "pret_a_cloturer": pret_a_cloturer,
    }


def cancel_pending_anomalies(conn, chat_id):
    pending = get_pending_anomalies(conn, chat_id)
    if not pending:
        return {"status": "error", "message": "Aucune anomalie en attente de confirmation."}
    clear_pending_anomalies(conn, chat_id)
    return {"status": "cancelled", "message": "❌ Valeur(s) annulee(s), rien n'a ete enregistre."}


def corriger_checkpoint(conn, date, poste, code, heure, cumul_nouveau, dt, saisi_par_id=None, saisi_par_nom=None):
    """Corrige un point deja enregistre et recalcule en cascade les points
    suivants de la meme ligne (leur production depend du cumul corrige)."""
    try:
        idx = _point_index(poste, heure)
    except ValueError:
        return {"status": "error", "message": f"Heure inconnue pour le poste {poste} : {heure}."}

    if not line_has_any_checkpoint(conn, date, poste, code):
        return {"status": "error", "message": "Aucune valeur enregistree pour cette ligne a corriger."}

    points = _points(poste)
    cumul_precedent = get_cumul_precedent(conn, date, poste, code, heure)
    resultats = []
    for i in range(idx, len(points)):
        p = points[i]
        if i == idx:
            cumul = cumul_nouveau
            horodatage = dt.isoformat()
        else:
            existing = conn.execute(
                "SELECT cumul_envoye, horodatage_reel FROM checkpoints "
                "WHERE date=? AND poste=? AND code=? AND heure=?",
                (date, poste, code, p["heure"]),
            ).fetchone()
            if existing is None:
                break
            cumul = existing["cumul_envoye"]
            horodatage = existing["horodatage_reel"]
        resultats.append(
            save_checkpoint(
                conn, date, poste, code, p["heure"], cumul, horodatage, cumul_precedent,
                saisi_par_id, saisi_par_nom,
            )
        )
        cumul_precedent = cumul

    message = "✏️ Correction enregistree.\n\n" + format_report(date, poste, heure, resultats[:1])
    return {"status": "report", "message": message, "date": date, "poste": poste}


def preview_checkin(conn, date, poste, heure, valeurs):
    """Calcule un apercu (production de l'heure, % de l'objectif) SANS rien
    ecrire en base ni pousser vers Google Sheet — pour la carte de
    confirmation de la saisie guidee (bot v2 Tache B2), affichee avant que
    l'utilisateur ne valide."""
    lignes = []
    for code in config.ORDRE_AFFICHAGE:
        if code not in valeurs:
            continue
        cumul_nouveau = valeurs[code]
        cumul_precedent = get_cumul_precedent(conn, date, poste, code, heure)
        production = cumul_nouveau - cumul_precedent
        objectif = objectif_point(poste, code, heure)
        pct = (production / objectif * 100) if objectif else None
        lignes.append({
            "code": code,
            "nom": config.LIGNES[code]["nom_affiche"],
            "cumul": cumul_nouveau,
            "production": production,
            "objectif": objectif,
            "pct": pct,
        })
    return lignes


# ---------------------------------------------------------------------------
# Machine a etats de l'interaction guidee par boutons (bot v2, Tache B4)
# ---------------------------------------------------------------------------

def get_interaction_state(conn, chat_id, user_id, now):
    """Retourne {"etat", "contexte", "message_id"} pour (chat_id, user_id),
    ou None s'il n'y en a pas ou si l'etat a expire (auto-efface dans ce
    cas — expiration paresseuse a l'acces, B4.5)."""
    row = conn.execute(
        "SELECT * FROM interaction_state WHERE chat_id = ? AND user_id = ?",
        (str(chat_id), str(user_id)),
    ).fetchone()
    if row is None:
        return None
    maj_a = datetime.fromisoformat(row["maj_a"])
    if now - maj_a > timedelta(minutes=ETAT_EXPIRATION_MINUTES):
        clear_interaction_state(conn, chat_id, user_id)
        return None
    return {
        "etat": row["etat"],
        "contexte": json.loads(row["contexte"]),
        "message_id": row["message_id"],
    }


def set_interaction_state(conn, chat_id, user_id, etat, contexte, dt, message_id=None):
    conn.execute(
        "INSERT INTO interaction_state (chat_id, user_id, etat, contexte, message_id, maj_a) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
        "etat=excluded.etat, contexte=excluded.contexte, message_id=excluded.message_id, maj_a=excluded.maj_a",
        (str(chat_id), str(user_id), etat, json.dumps(contexte), message_id, dt.isoformat()),
    )
    conn.commit()


def clear_interaction_state(conn, chat_id, user_id):
    conn.execute(
        "DELETE FROM interaction_state WHERE chat_id = ? AND user_id = ?",
        (str(chat_id), str(user_id)),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Synthese de fin de poste (spec v2 §5)
# ---------------------------------------------------------------------------

def _stats_points_for(poste_nums):
    """Concatene, dans l'ordre chronologique, les points de controle de un
    ou plusieurs postes (poste 1 puis poste 2 pour la synthese consolidee),
    chaque point porte son numero de poste d'origine."""
    out = []
    for poste in poste_nums:
        for p in _points(poste):
            out.append({**p, "poste": poste})
    return out


def compute_poste_stats(conn, date, poste):
    return _compute_stats(conn, [(date, poste)])


def compute_consolidated_stats(conn, date):
    """Synthese poste 1 + poste 2 cumules (spec v2 §4.5), a envoyer
    uniquement si le poste 2 a effectivement tourne ce jour-la."""
    return _compute_stats(conn, [(date, 1), (date, 2)])


def _compute_stats(conn, date_postes):
    """date_postes: liste de (date, poste) a agreger, dans l'ordre
    chronologique (un seul element pour une synthese de poste simple, deux
    pour la synthese consolidee poste1+poste2)."""
    points_flat = []
    for poste_date, poste in date_postes:
        for p in _points(poste):
            points_flat.append({**p, "poste": poste, "date": poste_date})

    lignes = []
    total_general = 0.0
    objectif_total = 0.0
    cumul_final_non_saisi = False

    for code in config.ORDRE_AFFICHAGE:
        points_ligne = []
        checkpoints_ligne = []
        for pt in points_flat:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE date=? AND poste=? AND code=? AND heure=?",
                (pt["date"], pt["poste"], code, pt["heure"]),
            ).fetchone()
            active = row is not None
            points_ligne.append({
                "heure": pt["heure"],
                "poste": pt["poste"],
                "active": active,
                "objectif": row["objectif"] if active else None,
                "production": row["production_heure"] if active else None,
                "realisation_pct": (
                    (row["production_heure"] / row["objectif"] * 100) if active and row["objectif"] else None
                ),
            })
            if active:
                checkpoints_ligne.append(row)

        if not checkpoints_ligne:
            continue  # ligne jamais active sur la periode -> absente de la synthese

        # Le cumul repart de 0 a chaque poste (spec v2 §4.4) : le total
        # consolide est la SOMME du dernier cumul de chaque poste, jamais le
        # dernier cumul tout court (qui ne serait que celui du poste 2).
        dernier_cumul_par_poste = {}
        for r in checkpoints_ligne:
            dernier_cumul_par_poste[r["poste"]] = r["cumul_envoye"]
        total = sum(dernier_cumul_par_poste.values())
        objectif_poste = sum(r["objectif"] or 0 for r in checkpoints_ligne)
        ecart = total - objectif_poste
        ecart_pct = (ecart / objectif_poste * 100) if objectif_poste else None
        heures_equivalentes = sum(r["coef"] for r in checkpoints_ligne)
        moyenne_horaire = (total / heures_equivalentes) if heures_equivalentes else None

        meilleure = max(checkpoints_ligne, key=lambda r: r["production_heure"])
        pire = min(checkpoints_ligne, key=lambda r: r["production_heure"])

        derniere_config_poste = date_postes[-1][1]
        heure_finale = final_heure(derniere_config_poste)
        if heure_finale and not any(
            r["heure"] == heure_finale and r["poste"] == derniere_config_poste for r in checkpoints_ligne
        ):
            cumul_final_non_saisi = True

        lignes.append({
            "code": code,
            "nom": config.LIGNES[code]["nom_affiche"],
            "total": total,
            "objectif": objectif_poste,
            "ecart": ecart,
            "ecart_pct": ecart_pct,
            "moyenne_horaire": moyenne_horaire,
            "meilleure_heure": (meilleure["heure"], meilleure["production_heure"]),
            "pire_heure": (pire["heure"], pire["production_heure"]),
            "points": points_ligne,
        })
        total_general += total
        objectif_total += objectif_poste

    taux_global_pct = (total_general / objectif_total * 100) if objectif_total else None

    dates = sorted({d for d, _ in date_postes})
    postes_nums = [p for _, p in date_postes]
    if len(date_postes) == 1:
        date, poste = date_postes[0]
        libelle = f"Poste {poste}"
        debut, fin = _poste_config(poste)["debut"], _poste_config(poste)["fin"]
    else:
        date = dates[0]
        libelle = "Journée complète (Poste 1 + 2)"
        debut = _poste_config(postes_nums[0])["debut"]
        fin = _poste_config(postes_nums[-1])["fin"]

    return {
        "date": date,
        "postes": postes_nums,
        "libelle": libelle,
        "debut": debut,
        "fin": fin,
        "lignes": lignes,
        "total_general": total_general,
        "taux_global_pct": taux_global_pct,
        "cumul_final_non_saisi": cumul_final_non_saisi,
        "points_axe": [p["heure"] for p in points_flat],
    }


# ---------------------------------------------------------------------------
# Formatage des messages
# ---------------------------------------------------------------------------

def fmt_qty(x):
    if x is None:
        return "-"
    if float(x).is_integer():
        return str(int(x))
    return f"{x:.1f}"


def fmt_signed_qty(x):
    if x is None:
        return "-"
    sign = "+" if x >= 0 else ""
    return f"{sign}{fmt_qty(x)}"


def fmt_signed_pct(x):
    if x is None:
        return None
    rounded = round(x, 1)
    if float(rounded).is_integer():
        val = f"{int(rounded)}"
    else:
        val = f"{rounded:.1f}"
    sign = "+" if rounded >= 0 else ""
    return f"{sign}{val}"


def fmt_pct(x):
    if x is None:
        return "-"
    rounded = round(x, 1)
    return f"{int(rounded)}" if float(rounded).is_integer() else f"{rounded:.1f}"


def format_report(date, poste, heure, resultats):
    date_obj = datetime.strptime(date, "%Y-%m-%d")
    lignes_par_code = {r["code"]: r for r in resultats}

    heure_debut = heure_debut_periode(poste, heure)
    header = "\n".join(
        [
            "🏭 RAPPORT HORAIRE — PRODUCTION",
            f"📅 {date_obj.strftime('%d/%m/%Y')}",
            f"⏰ {heure_debut} - {heure}",
        ]
    )
    blocs = []
    for code in config.ORDRE_AFFICHAGE:
        if code not in lignes_par_code:
            continue
        r = lignes_par_code[code]
        bloc = [f"🏗️ {r['nom']}", f"✅ Production : {fmt_qty(r['production'])} pcs"]
        if r["objectif"] is not None:
            bloc.append(f"🎯 Objectif : {fmt_qty(r['objectif'])} pcs")
            pct = fmt_signed_pct(r["ecart_pct"])
            bloc.append(f"📊 Écart : {fmt_signed_qty(r['ecart'])} pcs ({pct}%)")
        bloc.append(f"📈 Cumul jour : {fmt_qty(r['cumul'])} pcs")
        blocs.append("\n".join(bloc))

    return header + "\n\n" + "\n\n".join(blocs)


def format_anomaly_warning(anomalies):
    lines = [
        "⚠️ ANOMALIE — cumul inferieur au precedent (delta negatif) :",
    ]
    for code, precedent, nouveau in anomalies:
        nom = config.LIGNES[code]["nom_affiche"]
        lines.append(f"  • {nom} : precedent={fmt_qty(precedent)}, envoye={fmt_qty(nouveau)}")
    lines.append("Repondez au clavier ci-dessous, ou tapez \"oui\"/\"non\" en prive, pour confirmer ou annuler.")
    return "\n".join(lines)


def format_synthese(stats):
    date_obj = datetime.strptime(stats["date"], "%Y-%m-%d")
    header = [
        "📋 SYNTHÈSE DE FIN DE POSTE",
        f"📅 {date_obj.strftime('%d/%m/%Y')} — {stats['libelle']} : {stats['debut']} → {stats['fin']}",
    ]
    if stats["cumul_final_non_saisi"]:
        header.append(
            f"⚠️ Cumul final non saisi — chiffres arrêtés au dernier point reçu"
        )

    blocs = []
    for l in stats["lignes"]:
        bloc = [
            f"🏗️ {l['nom'].upper()}",
            f"📦 Production totale : {fmt_qty(l['total'])} pcs",
            f"🎯 Objectif du poste : {fmt_qty(l['objectif'])} pcs",
            f"📊 Écart : {fmt_signed_qty(l['ecart'])} pcs ({fmt_signed_pct(l['ecart_pct'])}%)",
            f"⚡ Moyenne horaire : {fmt_qty(l['moyenne_horaire'])} pcs/h",
            f"🏆 Meilleure heure : {l['meilleure_heure'][0]} ({fmt_qty(l['meilleure_heure'][1])} pcs)",
            f"🔻 Heure la plus faible : {l['pire_heure'][0]} ({fmt_qty(l['pire_heure'][1])} pcs)",
        ]
        blocs.append("\n".join(bloc))

    footer = [
        "━━━━━━━━━━━━━━━━━━",
        f"📈 TOTAL BU LAVAGE : {fmt_qty(stats['total_general'])} pcs",
        f"📊 Taux de réalisation global : {fmt_pct(stats['taux_global_pct'])}%",
    ]

    return "\n".join(header) + "\n\n" + "\n\n".join(blocs) + "\n\n" + "\n".join(footer)


def format_recap(conn, date, poste):
    poste_row = get_poste(conn, date, poste)
    if poste_row is None:
        return f"Aucun poste {poste} actif pour le {date}."

    date_obj = datetime.strptime(date, "%Y-%m-%d")
    lines = [f"📋 RECAP — {date_obj.strftime('%d/%m/%Y')} — Poste {poste} ({poste_row['statut']})", ""]
    any_line = False
    for code in config.ORDRE_AFFICHAGE:
        if not line_has_any_checkpoint(conn, date, poste, code):
            continue
        any_line = True
        cumul = get_dernier_cumul(conn, date, poste, code)
        last_row = conn.execute(
            "SELECT heure FROM checkpoints WHERE date=? AND poste=? AND code=?",
            (date, poste, code),
        ).fetchall()
        last_heure = sorted(last_row, key=lambda r: _point_index(poste, r["heure"]))[-1]["heure"]
        nom = config.LIGNES[code]["nom_affiche"]
        lines.append(f"🏗️ {nom} : cumul={fmt_qty(cumul)} pcs (dernier point : {last_heure})")
    if not any_line:
        lines.append("Aucun point de controle enregistre pour l'instant.")
    return "\n".join(lines)
