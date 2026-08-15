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
    _post_message(client, "/start_day", 8, 0)
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
    _post_message(client, "/start_day", 8, 0)
    _post_message(client, "/saisir", 9, 0)
    prompt = _last(client.sent)
    _post_message(client, "133 - 80", 9, 1, reply_to_mid=prompt["mid"])
    card = _last(client.sent)
    assert "Ligne Semi-auto" not in card["text"]
    assert "Ligne Auto" in card["text"] and "Ligne 03" in card["text"]


def test_saisie_guidee_format_invalide_garde_letat(client):
    _post_message(client, "/start_day", 8, 0)
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

    _post_message(client, "/start_day", n.hour, n.minute, date=today)
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
    _post_message(client, "/start_day", 8, 0)
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
    _post_message(client, "/start_day", 8, 0)

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
    _post_message(client, "/start_day", 8, 0)
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
    _post_message(client, "/start_day", 8, 0, user_id=42)
    r = _post_message(client, "/saisir", 9, 0, user_id=999)
    assert "non autorisé" in _last(client.sent)["text"].lower()


# --- Expiration a 20 minutes (B4.5) --------------------------------------------

def test_etat_expire_est_ignore(client):
    _post_message(client, "/start_day", 8, 0)
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
    _post_message(client, "/menu", 8, 0)
    menu = _last(client.sent)
    _post_callback(client, "menu_start_day", message_id=menu["mid"])

    client.sent.clear()
    _post_callback(client, "confirm_action:start_day", message_id=menu["mid"])
    entries = client.sent
    edited = [e for e in entries if e["kind"] == "edit"]
    sent_msgs = [e for e in entries if e["kind"] == "send"]
    assert any("Terminé" in e["text"] for e in edited)
    assert any("démarré" in e["text"].lower() for e in sent_msgs)


def test_menu_corriger_flow_complet(client):
    # Le menu (callback) opere sur l'horloge REELLE -> ce test reste sur
    # l'horloge reelle de bout en bout (voir test_guide_corriger_reboucle_
    # sur_forcereply pour la meme remarque).
    tz = ZoneInfo(client.app_module.config.TIMEZONE)
    n = datetime.now(tz)
    today = n.strftime("%Y-%m-%d")

    _post_message(client, "/menu", n.hour, n.minute, date=today)
    menu = _last(client.sent)
    _post_callback(client, "menu_start_day", message_id=menu["mid"])
    _post_callback(client, "confirm_action:start_day", message_id=menu["mid"])

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
