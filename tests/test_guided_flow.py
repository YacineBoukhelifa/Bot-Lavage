"""Tests du flux par boutons (spec bot v2, Tache B) : saisie guidee
(ForceReply + carte de confirmation), menu inline, machine a etats."""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

import pytest  # noqa: E402

TEST_DATE = "2031-03-17"  # cf. test_app_smoke.py : eloigne de la date reelle
CHAT_ID = -500


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from bot import app as app_module

    monkeypatch.setattr(app_module.config, "DB_PATH", str(tmp_path / "guided.sqlite3"))
    monkeypatch.setattr(app_module.config, "GROUPE_AUTORISE", 0)
    monkeypatch.setattr(app_module.config, "AUTORISATIONS", {"saisie": [], "lecture": "*"})
    monkeypatch.setattr(app_module.config, "EXCEL_LOCAL_DIR", str(tmp_path / "exports"))
    app_module.db.init_db()

    sent = []
    next_id = [1]

    def fake_send_message(chat_id, text, reply_markup=None, parse_mode=None):
        mid = next_id[0]
        next_id[0] += 1
        sent.append({
            "kind": "send", "mid": mid, "chat_id": chat_id, "text": text,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
        })
        return {"ok": True, "result": {"message_id": mid}}

    def fake_send_photo(chat_id, photo_bytes, caption=None, reply_markup=None, parse_mode=None):
        mid = next_id[0]
        next_id[0] += 1
        sent.append({"kind": "photo", "mid": mid, "chat_id": chat_id, "text": caption, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": mid}}

    def fake_send_document(chat_id, file_path, caption=None):
        mid = next_id[0]
        next_id[0] += 1
        sent.append({"kind": "document", "mid": mid, "chat_id": chat_id, "text": caption, "path": file_path})
        return {"ok": True, "result": {"message_id": mid}}

    def fake_edit_text(chat_id, message_id, text, reply_markup=None, parse_mode=None):
        sent.append({
            "kind": "edit", "mid": message_id, "chat_id": chat_id, "text": text,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
        })
        return {"ok": True, "result": {"message_id": message_id}}

    def fake_answer_cbq(cbq_id, text=None):
        sent.append({"kind": "answer_cbq", "id": cbq_id})

    monkeypatch.setattr(app_module.telegram_client, "send_message", fake_send_message)
    monkeypatch.setattr(app_module.telegram_client, "send_photo", fake_send_photo)
    monkeypatch.setattr(app_module.telegram_client, "send_document", fake_send_document)
    monkeypatch.setattr(app_module.telegram_client, "edit_message_text", fake_edit_text)
    monkeypatch.setattr(app_module.telegram_client, "answer_callback_query", fake_answer_cbq)

    test_client = app_module.app.test_client()
    test_client.sent = sent
    test_client.app_module = app_module
    return test_client


def _post_message(client, text, hh, mm, chat_id=CHAT_ID, chat_type="group", user_id=11,
                   first_name="Ali", reply_to_mid=None, date=TEST_DATE):
    config = client.app_module.config
    tz = ZoneInfo(config.TIMEZONE)
    y, m, d = (int(x) for x in date.split("-"))
    ts = int(datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())
    message = {
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "first_name": first_name},
        "text": text, "date": ts,
    }
    if reply_to_mid is not None:
        message["reply_to_message"] = {"message_id": reply_to_mid, "from": {"is_bot": True}}
    return client.post(f"/webhook/{config.WEBHOOK_SECRET}", json={"message": message})


def _post_callback(client, data, message_id, chat_id=CHAT_ID, user_id=11, first_name="Ali"):
    config = client.app_module.config
    return client.post(
        f"/webhook/{config.WEBHOOK_SECRET}",
        json={"callback_query": {
            "id": "cb", "data": data, "from": {"id": user_id, "first_name": first_name},
            "message": {"chat": {"id": chat_id, "type": "group"}, "message_id": message_id},
        }},
    )


def _last(sent):
    """Dernier item avec du contenu — `answer_callback_query` n'en porte pas
    et arrive toujours en dernier quand un callback n'envoie plus rien."""
    for e in reversed(sent):
        if e["kind"] != "answer_cbq":
            return e
    return None


def _seed_poste_actif(date, poste=1):
    """Ouvre un poste directement en base, synthese deja marquee "envoyee".

    La verification opportuniste de cloture (spec v2 §5.1) tourne a la fin
    de CHAQUE webhook (message ou callback) sur l'horloge REELLE — pas
    seulement au moment du demarrage. Si l'heure reelle a laquelle la suite
    de tests s'execute est deja passee 16:30, un poste actif serait
    referme des le premier webhook suivant, quel qu'il soit. Marquer la
    synthese comme deja envoyee neutralise ce declenchement pour les tests
    qui portent sur la saisie/correction, pas sur la cloture elle-meme."""
    from bot import db as db_module

    conn = db_module.get_connection()
    conn.execute(
        "INSERT INTO postes (date, poste, statut) VALUES (?, ?, 'actif') "
        "ON CONFLICT(date, poste) DO UPDATE SET statut='actif'",
        (date, poste),
    )
    conn.execute(
        "INSERT OR REPLACE INTO syntheses_envoyees (date, poste, envoyee_a) VALUES (?, ?, ?)",
        (date, poste, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def _start_day(client, hh, mm, objectifs="133 160 80", **kwargs):
    """/start_day demande desormais les objectifs horaires du jour
    (ForceReply) avant de demarrer reellement le shift — ce helper enchaine
    les deux messages comme le ferait un utilisateur reel."""
    _post_message(client, "/start_day", hh, mm, **kwargs)
    return _post_message(client, objectifs, hh, mm, **kwargs)


def _open_poste2(client, hh, mm, objectifs="133 160 80", **kwargs):
    """Meme principe que `_start_day`, pour /poste2 (/shift2)."""
    _post_message(client, "/poste2", hh, mm, **kwargs)
    return _post_message(client, objectifs, hh, mm, **kwargs)


def _seed_attente_cumuls(client, heure, message_id, poste=1, date=TEST_DATE, user_id="11", dt=None):
    """Place directement un etat ATTENTE_CUMULS, pour tester la suite du
    flux (carte, validation, correction) sans dependre du snap horaire de
    `/saisir` sur une heure precise."""
    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    dt = dt or datetime.now(ZoneInfo(client.app_module.config.TIMEZONE))
    logic_module.set_interaction_state(
        conn, CHAT_ID, user_id, "ATTENTE_CUMULS", {"poste": poste, "date": date, "heure": heure}, dt, message_id,
    )
    conn.close()


# --- B2 : saisie guidee, chemin nominal ----------------------------------------

def test_saisie_guidee_happy_path(client):
    _start_day(client, 8, 0)
    r = _post_message(client, "/saisir", 9, 0)
    assert r.status_code == 200
    prompt = _last(client.sent)
    assert prompt["reply_markup"] == {"force_reply": True, "selective": True}
    assert "09:00" in prompt["text"]

    _post_message(client, "133 160 80", 9, 1, reply_to_mid=prompt["mid"])
    card = _last(client.sent)
    assert card["kind"] == "edit" and card["mid"] == prompt["mid"]
    assert "à valider" in card["text"]
    assert card["reply_markup"] == client.app_module.keyboards.GUIDE_CONFIRM_KEYBOARD

    client.sent.clear()
    _post_callback(client, "guide_valider", message_id=card["mid"])
    # La carte est reduite a une courte confirmation (boutons retires)...
    edited = [e for e in client.sent if e["kind"] == "edit"]
    assert edited and edited[-1]["mid"] == card["mid"]
    assert edited[-1]["reply_markup"] is None
    # ...et le rapport complet part comme NOUVEAU message, avec le clavier
    # persistant reattache (masque par le ForceReply du debut du flux).
    result = _last(client.sent)
    assert result["kind"] == "send"
    assert result["reply_markup"] == client.app_module.keyboards.MAIN_KEYBOARD
    assert result["parse_mode"] == "HTML"
    assert "Production : 133 pcs" in result["text"]


def test_saisie_guidee_ligne_a_larret(client):
    _start_day(client, 8, 0)
    _post_message(client, "/saisir", 9, 0)
    prompt = _last(client.sent)
    _post_message(client, "133 - 80", 9, 1, reply_to_mid=prompt["mid"])
    card = _last(client.sent)
    assert "Ligne Semi-auto" not in card["text"]
    assert "Ligne Auto" in card["text"] and "Ligne 03" in card["text"]


def test_saisie_guidee_format_invalide_garde_letat(client):
    _start_day(client, 8, 0)
    _post_message(client, "/saisir", 9, 0)
    prompt = _last(client.sent)
    r = _post_message(client, "pas trois nombres ici", 9, 1, reply_to_mid=prompt["mid"])
    assert "invalide" in _last(client.sent)["text"].lower()
    # l'etat n'a pas avance : une reponse valide juste apres doit encore marcher
    _post_message(client, "100 100 50", 9, 2, reply_to_mid=prompt["mid"])
    assert "à valider" in _last(client.sent)["text"]


def test_guide_corriger_reboucle_sur_forcereply(client):
    # `guide_corriger` est un callback -> horloge REELLE (webhook()), donc ce
    # test reste sur l'horloge reelle de bout en bout plutot que TEST_DATE,
    # pour que l'etat qu'il ecrit ne paraisse pas expire a la prochaine
    # lecture (qui viendrait, elle, d'un message TEST_DATE tres eloigne).
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    n = datetime.now(tz)
    today = n.strftime("%Y-%m-%d")

    _seed_poste_actif(today)
    _seed_attente_cumuls(client, "09:00", message_id=42, date=today, dt=n)

    _post_message(client, "133 160 80", n.hour, n.minute, reply_to_mid=42, date=today)
    card = _last(client.sent)

    _post_callback(client, "guide_corriger", message_id=card["mid"])
    new_prompt = _last(client.sent)
    assert new_prompt["kind"] == "send"  # editer un message en ForceReply est impossible -> nouveau message
    assert new_prompt["reply_markup"] == {"force_reply": True, "selective": True}

    _post_message(client, "200 200 100", n.hour, n.minute, reply_to_mid=new_prompt["mid"], date=today)
    corrected_card = _last(client.sent)
    assert "200" in corrected_card["text"]

    _post_callback(client, "guide_valider", message_id=corrected_card["mid"])
    assert "Production : 200 pcs" in _last(client.sent)["text"]


def test_saisie_guidee_anomalie_garde_boutons_confirmation(client):
    _start_day(client, 8, 0)
    _post_message(client, "/saisir", 9, 0)
    p1 = _last(client.sent)
    _post_message(client, "500 500 500", 9, 1, reply_to_mid=p1["mid"])
    c1 = _last(client.sent)
    _post_callback(client, "guide_valider", message_id=c1["mid"])

    _post_message(client, "/saisir", 10, 0)
    p2 = _last(client.sent)
    _post_message(client, "480 500 500", 10, 1, reply_to_mid=p2["mid"])  # baisse pour A -> anomalie
    c2 = _last(client.sent)
    _post_callback(client, "guide_valider", message_id=c2["mid"])
    anomaly_card = _last(client.sent)
    assert "ANOMALIE" in anomaly_card["text"]
    assert anomaly_card["reply_markup"] == client.app_module.keyboards.ANOMALY_KEYBOARD


# --- Isolation par utilisateur (B4.6) et autorisation sur callback (B4.7) -----

def test_isolation_par_utilisateur_en_groupe(client):
    _start_day(client, 8, 0)

    _post_message(client, "/saisir", 10, 0, user_id=11, first_name="Ali")
    prompt_ali = _last(client.sent)
    _post_message(client, "/saisir", 10, 1, user_id=22, first_name="Sara")
    prompt_sara = _last(client.sent)
    assert prompt_ali["mid"] != prompt_sara["mid"]

    _post_message(client, "266 320 160", 10, 2, user_id=22, reply_to_mid=prompt_sara["mid"])
    card_sara = _last(client.sent)
    assert card_sara["mid"] == prompt_sara["mid"]

    _post_message(client, "133 160 80", 10, 3, user_id=11, reply_to_mid=prompt_ali["mid"])
    card_ali = _last(client.sent)
    assert card_ali["mid"] == prompt_ali["mid"]
    assert "133" in card_ali["text"]  # pas ecrase par la saisie de Sara


def test_callback_rejoue_sur_carte_dautrui_est_rejete(client):
    _start_day(client, 8, 0)
    _post_message(client, "/saisir", 9, 0, user_id=11)
    prompt_ali = _last(client.sent)
    _post_message(client, "133 160 80", 9, 1, user_id=11, reply_to_mid=prompt_ali["mid"])
    card_ali = _last(client.sent)

    client.sent.clear()
    _post_callback(client, "guide_valider", message_id=card_ali["mid"], user_id=22, first_name="Sara")
    result = _last(client.sent)
    assert "expirée" in result["text"].lower()
    # rien n'a ete valide : la carte d'Ali garde ses boutons de confirmation
    assert result["kind"] != "edit" or result["mid"] != card_ali["mid"]


def test_saisir_refuse_si_non_autorise_en_groupe(client, monkeypatch):
    monkeypatch.setattr(client.app_module.config, "AUTORISATIONS", {"saisie": [42], "lecture": "*"})
    _start_day(client, 8, 0, user_id=42)
    r = _post_message(client, "/saisir", 9, 0, user_id=999)
    assert "non autorisé" in _last(client.sent)["text"].lower()


# --- Expiration a 20 minutes (B4.5) --------------------------------------------

def test_etat_expire_est_ignore(client):
    _start_day(client, 8, 0)
    _post_message(client, "/saisir", 9, 0)
    prompt = _last(client.sent)

    from bot import logic as logic_module
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    past = datetime(2031, 3, 17, 8, 30, tzinfo=tz)  # 30 min avant l'envoi du prompt (9:00) -> deja vieux
    _seed_attente_cumuls(client, "09:00", prompt["mid"], dt=past)

    client.sent.clear()
    _post_message(client, "133 160 80", 9, 5, reply_to_mid=prompt["mid"])
    # etat expire -> traite comme texte libre -> ignore en groupe (privacy mode)
    assert client.sent == []


# --- B3 : menu -------------------------------------------------------------------

def test_menu_demarrage_avec_confirmation_puis_annulation(client):
    _post_message(client, "/menu", 8, 0)
    menu = _last(client.sent)
    assert menu["reply_markup"] == client.app_module.keyboards.MENU_KEYBOARD

    _post_callback(client, "menu_start_day", message_id=menu["mid"])
    confirm = _last(client.sent)
    assert "Démarrer une nouvelle journée" in confirm["text"]
    assert confirm["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "confirm_action:start_day"

    _post_callback(client, "cancel_action", message_id=menu["mid"])
    cancelled = _last(client.sent)
    assert "Annulé" in cancelled["text"]
    assert cancelled["reply_markup"] is None


def test_menu_demarrage_confirme(client):
    # `confirm_action:start_day` est un callback -> horloge REELLE
    # (webhook()), donc la reponse aux objectifs doit rester sur l'horloge
    # reelle elle aussi (meme remarque que test_guide_corriger_reboucle_
    # sur_forcereply) pour que l'etat qu'elle lit ne paraisse pas expire.
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    n = datetime.now(tz)
    today = n.strftime("%Y-%m-%d")

    _post_message(client, "/menu", n.hour, n.minute, date=today)
    menu = _last(client.sent)
    _post_callback(client, "menu_start_day", message_id=menu["mid"])

    client.sent.clear()
    _post_callback(client, "confirm_action:start_day", message_id=menu["mid"])
    entries = client.sent
    edited = [e for e in entries if e["kind"] == "edit"]
    sent_msgs = [e for e in entries if e["kind"] == "send"]
    assert any("Terminé" in e["text"] for e in edited)
    prompt = _last(sent_msgs)
    assert prompt["reply_markup"] == {"force_reply": True, "selective": True}

    client.sent.clear()
    _post_message(client, "133 160 80", n.hour, n.minute, reply_to_mid=prompt["mid"], date=today)
    # Pas forcement le tout dernier message : si l'heure reelle depasse deja
    # la fin du poste 1, la verification opportuniste de cloture (spec v2
    # §5.1, meme webhook) ajoute une synthese juste derriere.
    assert any("démarré" in (e.get("text") or "").lower() for e in client.sent)


def test_menu_corriger_flow_complet(client):
    # Le menu (callback) opere sur l'horloge REELLE -> ce test reste sur
    # l'horloge reelle de bout en bout (voir test_guide_corriger_reboucle_
    # sur_forcereply pour la meme remarque). Poste ouvert directement en
    # base (pas via /menu -> demarrer) pour ne pas declencher la cloture
    # opportuniste si l'heure reelle est deja passee 16:30 (spec v2 §5.1) —
    # ce test porte sur la correction, pas sur le demarrage.
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    n = datetime.now(tz)
    today = n.strftime("%Y-%m-%d")
    _seed_poste_actif(today)

    _post_message(client, "/menu", n.hour, n.minute, date=today)
    menu = _last(client.sent)

    from bot import db as db_module, logic as logic_module
    conn = db_module.get_connection()
    logic_module.set_interaction_state(
        conn, CHAT_ID, "11", "ATTENTE_CUMULS", {"poste": 1, "date": today, "heure": "09:00"}, n, 9999,
    )
    conn.close()

    client.sent.clear()
    _post_message(client, "100 100 50", n.hour, n.minute, reply_to_mid=9999, date=today)
    card = _last(client.sent)
    _post_callback(client, "guide_valider", message_id=card["mid"])

    client.sent.clear()
    _post_callback(client, "menu_corriger", message_id=menu["mid"])
    heures_card = _last(client.sent)
    assert heures_card["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "corr_heure|09:00"

    client.sent.clear()
    _post_callback(client, "corr_heure|09:00", message_id=menu["mid"])
    lignes_card = _last(client.sent)
    assert lignes_card["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "corr_ligne|09:00|A"

    client.sent.clear()
    _post_callback(client, "corr_ligne|09:00|A", message_id=menu["mid"])
    correction_prompt = _last(client.sent)
    assert correction_prompt["kind"] == "send"

    client.sent.clear()
    _post_message(client, "777", n.hour, n.minute, reply_to_mid=correction_prompt["mid"], date=today)
    assert "Production : 777 pcs" in _last(client.sent)["text"]

    conn = db_module.get_connection()
    row = conn.execute(
        "SELECT cumul_envoye FROM checkpoints WHERE date=? AND poste=1 AND code='A' AND heure='09:00'",
        (today,),
    ).fetchone()
    conn.close()
    assert row["cumul_envoye"] == 777.0


# --- Objectifs journaliers configurables (debut de shift) ----------------------

def test_saisie_guidee_utilise_objectifs_jour_custom(client):
    _start_day(client, 8, 0, objectifs="150 170 90")

    _post_message(client, "/saisir", 9, 0)
    prompt = _last(client.sent)
    _post_message(client, "150 170 90", 9, 1, reply_to_mid=prompt["mid"])
    card = _last(client.sent)

    client.sent.clear()
    _post_callback(client, "guide_valider", message_id=card["mid"])
    result = _last(client.sent)
    assert "Objectif : 150 pcs" in result["text"]
    assert "Objectif : 170 pcs" in result["text"]
    assert "Objectif : 90 pcs" in result["text"]


# --- Pause dejeuner dynamique par ligne (poste 1, point 12:00) -----------------

def test_saisir_declenche_la_question_pause_au_point_12h(client):
    """/saisir au point 12:00, sans decision de pause enregistree, pose la
    premiere question (ligne Auto) au lieu du ForceReply numerique habituel."""
    _seed_poste_actif(TEST_DATE)

    r = _post_message(client, "/saisir", 12, 0)
    assert r.status_code == 200
    q1 = _last(client.sent)
    assert "Ligne Auto" in q1["text"]
    assert q1["reply_markup"] == client.app_module.keyboards.pause_dejeuner_keyboard()


def test_pause_dejeuner_questions_puis_saisie_normale(client):
    # Les callbacks pause_dej operent sur l'horloge REELLE (webhook()), donc
    # ce test reste sur l'horloge reelle de bout en bout (meme remarque que
    # test_guide_corriger_reboucle_sur_forcereply) : l'etat est injecte
    # directement (comme _seed_attente_cumuls) plutot que declenche par un
    # /saisir simule a une heure fixe, pour ne pas dependre de l'heure reelle
    # a laquelle la suite de tests s'execute.
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    n = datetime.now(tz)
    today = n.strftime("%Y-%m-%d")
    _seed_poste_actif(today)

    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    logic_module.set_interaction_state(
        conn, CHAT_ID, "11", "ATTENTE_PAUSE_DEJEUNER",
        {"date": today, "poste": 1, "codes_restants": ["A", "S", "SKD"]}, n, 1,
    )
    conn.close()

    _post_callback(client, "pause_dej|oui", message_id=1)
    q2 = _last(client.sent)
    assert "Ligne Semi-auto" in q2["text"]

    _post_callback(client, "pause_dej|non", message_id=q2["mid"])
    q3 = _last(client.sent)
    assert "Ligne 03" in q3["text"]

    _post_callback(client, "pause_dej|non", message_id=q3["mid"])
    prompt = _last(client.sent)
    assert prompt["kind"] == "send"
    assert prompt["reply_markup"] == {"force_reply": True, "selective": True}
    assert "12:00" in prompt["text"]

    conn = db_module.get_connection()
    assert logic_module.get_pause_dejeuner(conn, today, "A") == "12:00"
    assert logic_module.get_pause_dejeuner(conn, today, "S") == "13:00"
    assert logic_module.get_pause_dejeuner(conn, today, "SKD") == "13:00"
    conn.close()

    _post_message(client, "66 160 80", n.hour, n.minute, reply_to_mid=prompt["mid"], date=today)
    card = _last(client.sent)
    _post_callback(client, "guide_valider", message_id=card["mid"])

    conn = db_module.get_connection()
    row = conn.execute(
        "SELECT coef, objectif FROM checkpoints WHERE date=? AND poste=1 AND code='A' AND heure='12:00'",
        (today,),
    ).fetchone()
    conn.close()
    assert row["coef"] == 0.5
    assert row["objectif"] == 66  # 133*0.5 -> floor


def test_pause_dejeuner_non_declenchee_deux_fois(client):
    """Une fois les 3 decisions prises, un /saisir ulterieur au point 12:00
    (ex. apres une correction) ne repose plus les questions."""
    _seed_poste_actif(TEST_DATE)

    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    for code in ("A", "S", "SKD"):
        logic_module.set_pause_dejeuner(conn, TEST_DATE, code, "13:00")
    conn.close()

    r = _post_message(client, "/saisir", 12, 0)
    assert r.status_code == 200
    prompt = _last(client.sent)
    assert prompt["reply_markup"] == {"force_reply": True, "selective": True}
    assert "12:00" in prompt["text"]


def test_pause_dejeuner_ignore_lignes_deja_decidees(client):
    """Si une ligne a deja une decision (ex. reponse partielle interrompue),
    la sequence de questions saute directement a la suivante."""
    _seed_poste_actif(TEST_DATE)

    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    logic_module.set_pause_dejeuner(conn, TEST_DATE, "A", "12:00")
    conn.close()

    r = _post_message(client, "/saisir", 12, 0)
    assert r.status_code == 200
    q1 = _last(client.sent)
    assert "Ligne Semi-auto" in q1["text"]
    assert "Ligne Auto" not in q1["text"]


def test_pause_dejeuner_callback_sur_carte_perimee_est_rejete(client):
    """Un clic sur une carte de question qui ne correspond plus a l'etat
    enregistre (ex. rejouee, ou etat deja avance) est rejete — rien n'est
    enregistre en base."""
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    n = datetime.now(tz)
    today = n.strftime("%Y-%m-%d")
    _seed_poste_actif(today)

    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    logic_module.set_interaction_state(
        conn, CHAT_ID, "11", "ATTENTE_PAUSE_DEJEUNER",
        {"date": today, "poste": 1, "codes_restants": ["A", "S", "SKD"]}, n, 1,
    )
    conn.close()

    _post_callback(client, "pause_dej|oui", message_id=999)  # mid ≠ carte enregistree (1)
    assert "expirée" in _last(client.sent)["text"].lower()

    conn = db_module.get_connection()
    assert logic_module.get_pause_dejeuner(conn, today, "A") == "13:00"  # rien n'a ete enregistre
    conn.close()


def test_pause_dejeuner_tout_non_garde_defaut_pour_toutes_les_lignes(client):
    """Repondre Non aux 3 questions doit reproduire exactement le comportement
    historique : pause a 13:00 (coef 0.5) pour toutes les lignes, 12:00 a
    coef normal (1.0) pour toutes."""
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    n = datetime.now(tz)
    today = n.strftime("%Y-%m-%d")
    _seed_poste_actif(today)

    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    logic_module.set_interaction_state(
        conn, CHAT_ID, "11", "ATTENTE_PAUSE_DEJEUNER",
        {"date": today, "poste": 1, "codes_restants": ["A", "S", "SKD"]}, n, 1,
    )
    conn.close()

    _post_callback(client, "pause_dej|non", message_id=1)
    q2 = _last(client.sent)
    _post_callback(client, "pause_dej|non", message_id=q2["mid"])
    q3 = _last(client.sent)
    _post_callback(client, "pause_dej|non", message_id=q3["mid"])
    prompt12 = _last(client.sent)

    _post_message(client, "133 160 80", n.hour, n.minute, reply_to_mid=prompt12["mid"], date=today)
    card12 = _last(client.sent)
    _post_callback(client, "guide_valider", message_id=card12["mid"])

    # Point 13:00 injecte directement (comme _seed_attente_cumuls) plutot que
    # via /saisir : snap_to_checkpoint se base sur l'heure du MESSAGE, qui ne
    # peut pas etre fixee a "13:00" tout en restant sur l'horloge reelle
    # necessaire aux callbacks pause_dej precedents (meme remarque que
    # test_guide_corriger_reboucle_sur_forcereply).
    _seed_attente_cumuls(client, "13:00", message_id=555, date=today, dt=n)
    _post_message(client, "199 240 120", n.hour, n.minute, reply_to_mid=555, date=today)
    card13 = _last(client.sent)
    _post_callback(client, "guide_valider", message_id=card13["mid"])

    conn = db_module.get_connection()
    row12 = conn.execute(
        "SELECT coef FROM checkpoints WHERE date=? AND poste=1 AND code='A' AND heure='12:00'", (today,),
    ).fetchone()
    row13 = conn.execute(
        "SELECT coef FROM checkpoints WHERE date=? AND poste=1 AND code='A' AND heure='13:00'", (today,),
    ).fetchone()
    conn.close()
    assert row12["coef"] == 1.0
    assert row13["coef"] == 0.5


def test_pause_dejeuner_non_declenchee_en_poste2(client):
    """Le mecanisme est scope au poste 1 uniquement (mot de l'utilisateur :
    "in the first shift") — le poste 2 garde sa pause fixe a 19:30."""
    _start_day(client, 8, 0)
    _post_message(client, "/fin", 16, 25)  # cloture propre du poste 1 avant le poste 2
    _open_poste2(client, 16, 35)

    client.sent.clear()
    r = _post_message(client, "/saisir", 19, 30)
    assert r.status_code == 200
    prompt = _last(client.sent)
    assert prompt["reply_markup"] == {"force_reply": True, "selective": True}
    assert "19:30" in prompt["text"]


def test_commande_legacy_ne_declenche_jamais_la_question_pause(client):
    """Les chemins historiques (/a, /prod, saisie libre, /corriger) ne
    posent jamais la question — ils heritent juste de la decision deja
    prise ou du defaut (spec : portee volontairement limitee a /saisir)."""
    _seed_poste_actif(TEST_DATE)

    r = _post_message(client, "/a 133", 12, 0)
    assert r.status_code == 200
    result = _last(client.sent)
    assert "Production : 133 pcs" in result["text"]

    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    state = logic_module.get_interaction_state(conn, CHAT_ID, "11", datetime.now(tz))
    conn.close()
    assert state is None  # aucune question pause_dej n'a ete amorcee


# --- Regression : cloture au point final + clavier persistant reattache ------

def test_saisie_guidee_point_final_cloture_et_restaure_clavier(client):
    """Reproduit le probleme rapporte en production : au dernier point
    (16:30), valider la saisie guidee doit a la fois cloturer le poste
    (synthese + graphique envoyes) ET reattacher le clavier persistant —
    pas rester bloque apres le ForceReply qui l'avait masque en debut de
    flux (bug constate le 15/08/2026, cf. memoire du projet)."""
    _start_day(client, 8, 0)

    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    for code in ("A", "S", "SKD"):
        logic_module.set_pause_dejeuner(conn, TEST_DATE, code, "13:00")
    conn.close()

    checkpoints = [
        ("09:00", "133 160 80"), ("10:00", "266 320 160"), ("11:00", "399 480 240"),
        ("12:00", "532 640 320"), ("13:00", "598 720 360"), ("14:00", "731 880 440"),
        ("15:00", "864 1040 520"), ("16:00", "997 1200 600"),
    ]
    for heure, valeurs in checkpoints:
        h, m = (int(x) for x in heure.split(":"))
        _post_message(client, "/saisir", h, m)
        prompt = _last(client.sent)
        _post_message(client, valeurs, h, m, reply_to_mid=prompt["mid"])
        card = _last(client.sent)
        client.sent.clear()
        _post_callback(client, "guide_valider", message_id=card["mid"])
        client.sent.clear()

    # Point final 16:30 : declenche la cloture automatique (spec v2 §5, point
    # final complet).
    _post_message(client, "/saisir", 16, 30)
    prompt = _last(client.sent)
    _post_message(client, "1180 1240 632", 16, 30, reply_to_mid=prompt["mid"])
    card = _last(client.sent)

    client.sent.clear()
    _post_callback(client, "guide_valider", message_id=card["mid"])

    kinds = [m["kind"] for m in client.sent]
    assert "photo" in kinds  # le graphique de la synthese est parti automatiquement
    assert any("SYNTH" in (m.get("text") or "").upper() for m in client.sent)
    # Le clavier persistant doit etre reattache sur au moins un des messages
    # de cloture — un edit (comme "✅ Enregistré.") ne peut jamais le faire,
    # seul un nouveau sendMessage/sendPhoto portant reply_markup le peut.
    restored = [
        m for m in client.sent
        if m["kind"] in ("send", "photo") and m.get("reply_markup") == client.app_module.keyboards.MAIN_KEYBOARD
    ]
    assert restored, "le clavier persistant MAIN_KEYBOARD n'a pas ete reattache apres la cloture automatique"


# --- Renommage Shift : /shift2 est un alias plein de /poste2 ------------------

def test_shift2_est_alias_de_poste2(client):
    _start_day(client, 8, 0)

    r = _post_message(client, "/shift2", 16, 35)
    assert r.status_code == 200
    prompt = _last(client.sent)
    assert prompt["reply_markup"] == {"force_reply": True, "selective": True}
    assert "Objectifs horaires" in prompt["text"]

    _post_message(client, "140 150 70", 16, 35, reply_to_mid=prompt["mid"])
    result = _last(client.sent)
    assert "shift 2" in result["text"].lower()
    assert "activé" in result["text"].lower()

    from bot import db as db_module, logic as logic_module

    conn = db_module.get_connection()
    assert logic_module.get_objectif_horaire(conn, TEST_DATE, 2, "A") == 140
    conn.close()


# --- Objectifs journaliers : cas limites ----------------------------------------

def test_objectifs_jour_format_invalide_garde_letat(client):
    _post_message(client, "/start_day", 8, 0)
    prompt = _last(client.sent)
    _post_message(client, "pas trois nombres", 8, 1, reply_to_mid=prompt["mid"])
    assert "invalide" in _last(client.sent)["text"].lower()
    # l'etat n'a pas avance : une reponse valide juste apres doit encore marcher
    _post_message(client, "133 160 80", 8, 2, reply_to_mid=prompt["mid"])
    assert "démarré" in _last(client.sent)["text"].lower()


def test_objectifs_jour_tiret_garde_defaut_pour_une_ligne(client):
    _start_day(client, 8, 0, objectifs="150 - 90")

    _post_message(client, "/saisir", 9, 0)
    prompt = _last(client.sent)
    _post_message(client, "150 160 90", 9, 1, reply_to_mid=prompt["mid"])
    card = _last(client.sent)

    client.sent.clear()
    _post_callback(client, "guide_valider", message_id=card["mid"])
    result = _last(client.sent)
    assert "Objectif : 150 pcs" in result["text"]
    assert "Objectif : 160 pcs" in result["text"]  # S : "-" -> defaut config (160), pas custom
    assert "Objectif : 90 pcs" in result["text"]


def test_objectifs_jour_independants_entre_poste1_et_poste2(client):
    _start_day(client, 8, 0, objectifs="150 170 90")
    _post_message(client, "/fin", 16, 25)  # cloture propre du poste 1 avant le poste 2
    _open_poste2(client, 16, 35, objectifs="140 150 70")

    client.sent.clear()
    _post_message(client, "/saisir", 17, 30)  # premier point du poste 2
    prompt = _last(client.sent)
    _post_message(client, "140 150 70", 17, 30, reply_to_mid=prompt["mid"])
    card = _last(client.sent)

    client.sent.clear()
    _post_callback(client, "guide_valider", message_id=card["mid"])
    result = _last(client.sent)
    assert "Objectif : 140 pcs" in result["text"]  # objectif du poste 2, pas 150 (poste 1)


def test_objectifs_jour_etat_expire_est_ignore(client):
    _post_message(client, "/start_day", 8, 0)
    prompt = _last(client.sent)

    from bot import db as db_module, logic as logic_module

    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    past = datetime(2031, 3, 17, 7, 30, tzinfo=tz)  # 30 min avant l'envoi du prompt -> deja vieux
    conn = db_module.get_connection()
    logic_module.set_interaction_state(
        conn, CHAT_ID, "11", "ATTENTE_OBJECTIFS_JOUR", {"action": "start_day"}, past, prompt["mid"],
    )
    conn.close()

    client.sent.clear()
    _post_message(client, "133 160 80", 8, 5, reply_to_mid=prompt["mid"])
    # etat expire -> traite comme texte libre -> ignore en groupe (privacy mode)
    assert client.sent == []
