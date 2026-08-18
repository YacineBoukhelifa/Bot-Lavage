"""Couche de persistance SQLite."""
import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS postes (
    date TEXT NOT NULL,
    poste INTEGER NOT NULL,
    statut TEXT NOT NULL CHECK(statut IN ('actif', 'cloture')),
    ouvert_a TEXT,
    PRIMARY KEY (date, poste)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    date TEXT NOT NULL,
    poste INTEGER NOT NULL,
    code TEXT NOT NULL,
    heure TEXT NOT NULL,
    cumul_envoye REAL NOT NULL,
    production_heure REAL NOT NULL,
    coef REAL NOT NULL,
    objectif REAL,
    ecart REAL,
    ecart_pct REAL,
    horodatage_reel TEXT NOT NULL,
    saisi_par_id TEXT,
    saisi_par_nom TEXT,
    PRIMARY KEY (date, poste, code, heure)
);

CREATE TABLE IF NOT EXISTS pending_anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    date TEXT NOT NULL,
    poste INTEGER NOT NULL,
    heure TEXT NOT NULL,
    code TEXT NOT NULL,
    cumul_precedent REAL NOT NULL,
    cumul_nouveau REAL NOT NULL,
    horodatage_reel TEXT NOT NULL,
    saisi_par_id TEXT,
    saisi_par_nom TEXT
);

-- Etat singleton du poste 2 : ouvert ou non, et sur quelle date de
-- production (necessaire pour rattacher correctement les mesures qui
-- traversent minuit — spec v2 §4.3).
CREATE TABLE IF NOT EXISTS poste_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    poste2_ouvert INTEGER NOT NULL DEFAULT 0,
    poste2_date TEXT,
    poste2_ouvert_a TEXT
);

-- Memorise les syntheses de fin de poste deja envoyees, pour eviter les
-- doublons entre declenchement immediat / opportuniste / manuel (/fin).
CREATE TABLE IF NOT EXISTS syntheses_envoyees (
    date TEXT NOT NULL,
    poste INTEGER NOT NULL,
    envoyee_a TEXT NOT NULL,
    PRIMARY KEY (date, poste)
);

-- IDs des fichiers/feuilles Google (spreadsheet, exports mensuels, backups
-- .db) pour ne jamais recreer un fichier mais toujours faire un update.
CREATE TABLE IF NOT EXISTS drive_meta (
    cle TEXT PRIMARY KEY,
    valeur TEXT
);

-- Chat_id de groupes non autorises deja avertis une fois (spec v2 §1.4 :
-- "reponse non autorise une seule fois, puis silence").
CREATE TABLE IF NOT EXISTS groupes_avertis (
    chat_id TEXT PRIMARY KEY
);

-- Machine a etats de l'interaction guidee par boutons (spec bot v2,
-- Tache B4). Une ligne par (chat_id, user_id) : deux personnes dans le
-- meme groupe ont chacune leur propre saisie en cours, sans se marcher
-- dessus (B4.6). Persistee en base (pas en memoire) pour survivre a un
-- redemarrage du process (B4.4).
CREATE TABLE IF NOT EXISTS interaction_state (
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    etat TEXT NOT NULL,
    contexte TEXT NOT NULL,
    message_id INTEGER,
    maj_a TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

-- Objectifs horaires saisis en debut de shift (par ligne), a la place des
-- constantes fixes de config.LIGNES pour cette (date, poste). Absence de
-- ligne = garder le defaut config.LIGNES.
CREATE TABLE IF NOT EXISTS objectifs_jour (
    date TEXT NOT NULL,
    poste INTEGER NOT NULL,
    code TEXT NOT NULL,
    objectif_horaire REAL NOT NULL,
    PRIMARY KEY (date, poste, code)
);

-- Decision par ligne de l'heure de pause dejeuner du poste 1 : "12:00" ou
-- "13:00". Absence de ligne = defaut historique "13:00" (coef fixe deja
-- en dur dans config.py avant cette fonctionnalite).
CREATE TABLE IF NOT EXISTS pause_dejeuner (
    date TEXT NOT NULL,
    poste INTEGER NOT NULL DEFAULT 1,
    code TEXT NOT NULL,
    heure_pause TEXT NOT NULL,
    PRIMARY KEY (date, poste, code)
);

INSERT OR IGNORE INTO poste_state (id, poste2_ouvert) VALUES (1, 0);
"""


def get_connection():
    os.makedirs(os.path.dirname(os.path.abspath(config.DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_v1_schema(conn):
    """Ecarte les anciennes tables v1 (`days`, `checkpoints` sans colonne
    `poste`) sous un nom `_v1_backup_*` au lieu de les modifier en place, pour
    ne jamais perdre de donnees silencieusement. Les tables v2 sont ensuite
    creees fraiches par SCHEMA. Idempotent."""
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }

    if "days" in tables and "postes" not in tables:
        conn.execute("ALTER TABLE days RENAME TO _v1_backup_days")

    if "checkpoints" in tables:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(checkpoints)")}
        if "poste" not in cols:
            conn.execute("ALTER TABLE checkpoints RENAME TO _v1_backup_checkpoints")

    if "pending_anomalies" in tables:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(pending_anomalies)")}
        if "poste" not in cols:
            conn.execute("ALTER TABLE pending_anomalies RENAME TO _v1_backup_pending_anomalies")

    conn.commit()


def init_db():
    conn = get_connection()
    try:
        _migrate_v1_schema(conn)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
