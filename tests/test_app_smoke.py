import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

import pytest  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from bot import app as app_module

    monkeypatch.setattr(app_module.config, "DB_PATH", str(tmp_path / "smoke.sqlite3"))
    monkeypatch.setattr(app_module.config, "GROUPE_AUTORISE", 0)
    monkeypatch.setattr(app_module.config, "AUTORISATIONS", {"saisie": [], "lecture": "*"})
    monkeypatch.setattr(app_module.config, "EXCEL_LOCAL_DIR", str(tmp_path / "exports"))
    app_module.db.init_db()

    sent = []

    def fake_send_message(chat_id, text, reply_markup=None, parse_mode=None):
        sent.append({
            "kind": "text", "chat_id": chat_id, "text": text,
            "reply_markup": reply_markup, "parse_mode": parse_mode,
        })

    def fake_send_photo(chat_id, photo_bytes, caption=None, reply_markup=None, parse_mode=None):
        sent.append({
            "kind": "photo", "chat_id": chat_id, "text": caption, "caption": caption,
            "size": len(photo_bytes), "parse_mode": parse_mode,
        })

    def fake_send_document(chat_id, file_path, caption=None):
        sent.append({"kind": "document", "chat_id": chat_id, "text": caption, "caption": caption, "path": file_path})

    def fake_answer_cbq(callback_query_id, text=None):
        sent.append({"kind": "answer_cbq", "id": callback_query_id})

    monkeypatch.setattr(app_module.telegram_client, "send_message", fake_send_message)
    monkeypatch.setattr(app_module.telegram_client, "send_photo", fake_send_photo)
    monkeypatch.setattr(app_module.telegram_client, "send_document", fake_send_document)
    monkeypatch.setattr(app_module.telegram_client, "answer_callback_query", fake_answer_cbq)

    test_client = app_module.app.test_client()
    test_client.sent = sent
    test_client.app_module = app_module
    return test_client


# Date volontairement tres eloignee de la date reelle : le webhook declenche
# aussi une verification opportuniste de cloture basee sur l'horloge REELLE
# pour les callbacks (spec v2 §5.1, cle sur `datetime.now()` puisqu'un
# callback_query Telegram ne porte pas d'horodatage "actuel" exploitable).
# Une date de test proche d'aujourd'hui ferait declencher cette verification
# de facon non deterministe selon l'heure a laquelle la suite tourne.
TEST_DATE = "2031-03-17"


def _post_message(client, text, hh, mm, chat_id=100, chat_type="private", user_id=1, first_name="Yacine",
                   date=TEST_DATE):
    config = client.app_module.config
    tz = ZoneInfo(config.TIMEZONE)
    y, m, d = (int(x) for x in date.split("-"))
    ts = int(datetime(y, m, d, hh, mm, tzinfo=tz).timestamp())
    payload = {
        "message": {
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "first_name": first_name},
            "text": text,
            "date": ts,
        }
    }
    return client.post(f"/webhook/{config.WEBHOOK_SECRET}", json=payload)


def _post_callback(client, data, chat_id, callback_id="cbq1"):
    config = client.app_module.config
    return client.post(
        f"/webhook/{config.WEBHOOK_SECRET}",
        json={"callback_query": {"id": callback_id, "data": data, "message": {"chat": {"id": chat_id}}}},
    )


def _texts(sent):
    return [m["text"] for m in sent if m["kind"] == "text"]


# --- Format libre en prive (compatibilite v1) ---------------------------------

def test_private_free_format_checkin(client):
    _post_message(client, "/start_day", 8, 0)
    r = _post_message(client, "A 800\nS 660", 9, 0)
    assert r.status_code == 200
    assert "RAPPORT HORAIRE" in client.sent[-1]["text"]


# --- Commandes de saisie -------------------------------------------------------

def test_prod_multi_command(client):
    _post_message(client, "/start_day", 8, 0)
    _post_message(client, "/prod a=133 s=160 l3=80", 9, 0)
    assert "Ligne Auto" in client.sent[-1]["text"]
    assert "Ligne 03" in client.sent[-1]["text"]


def test_single_line_command_with_bot_suffix(client):
    _post_message(client, "/start_day", 8, 0)
    r = _post_message(client, "/a@my_bot 250", 9, 0)
    assert r.status_code == 200
    assert "Production : 250 pcs" in client.sent[-1]["text"]


def test_keyboard_button_is_a_real_command(client):
    from bot import keyboards

    _post_message(client, "/start_day", 8, 0)
    assert "démarré" in client.sent[-1]["text"].lower()
    _post_message(client, "/prod a=100", 9, 0)
    _post_message(client, keyboards.BTN_RECAP, 9, 5)
    assert "RECAP" in client.sent[-1]["text"]


# --- Anomalie via bouton inline -------------------------------------------------

def test_anomaly_confirm_via_callback(client):
    _post_message(client, "/start_day", 8, 0)
    _post_message(client, "/a 500", 9, 0)
    r = _post_message(client, "/a 480", 10, 0)
    assert "ANOMALIE" in client.sent[-1]["text"]

    _post_callback(client, "confirm_anomaly", chat_id=100)
    assert "Production : -20 pcs" in client.sent[-1]["text"]


def test_report_is_sent_as_copyable_pre_block(client):
    """Plus de bouton Copier (spec Tache A) : le rapport est envoye en un
    seul message, wrappe en <pre> HTML pour etre copiable nativement."""
    _post_message(client, "/start_day", 8, 0)
    _post_message(client, "/a 250", 9, 0)
    msg = client.sent[-1]
    assert msg["parse_mode"] == "HTML"
    assert msg["text"].startswith("<pre>")
    assert msg["text"].endswith("</pre>")
    assert "Production : 250 pcs" in msg["text"]
    assert msg["reply_markup"] == client.app_module.keyboards.MAIN_KEYBOARD


# --- Groupe : autorisation et permissions --------------------------------------

def test_unauthorized_group_warned_once_then_silent(client, monkeypatch):
    monkeypatch.setattr(client.app_module.config, "GROUPE_AUTORISE", -999)
    _post_message(client, "/start_day", 8, 0, chat_id=-100111, chat_type="group")
    assert "n'est pas autorisé" in client.sent[-1]["text"].lower()

    client.sent.clear()
    _post_message(client, "/start_day", 8, 1, chat_id=-100111, chat_type="group")
    assert client.sent == []


def test_free_text_ignored_in_group(client):
    _post_message(client, "bonjour", 9, 0, chat_id=-100111, chat_type="group")
    assert client.sent == []


def test_saisie_denied_for_unlisted_user_in_group(client, monkeypatch):
    monkeypatch.setattr(client.app_module.config, "AUTORISATIONS", {"saisie": [42], "lecture": "*"})
    r = _post_message(client, "/start_day", 8, 0, chat_id=-100111, chat_type="group", user_id=7)
    assert "non autorisé à saisir" in client.sent[-1]["text"].lower()

    client.sent.clear()
    _post_message(client, "/start_day", 8, 1, chat_id=-100111, chat_type="group", user_id=42)
    assert "démarré" in client.sent[-1]["text"].lower()


def test_id_command_open_to_everyone(client):
    _post_message(client, "/id", 9, 0, chat_id=-100111, chat_type="group", user_id=55, first_name="Foo")
    text = client.sent[-1]["text"]
    assert "-100111" in text
    assert "55" in text


# --- Graph / Export -------------------------------------------------------------

def test_graph_command_sends_photo(client):
    _post_message(client, "/start_day", 8, 0)
    _post_message(client, "/prod a=133 s=160 l3=80", 9, 0)
    r = _post_message(client, "/graph", 9, 5)
    assert r.status_code == 200
    photos = [m for m in client.sent if m["kind"] == "photo"]
    assert len(photos) == 1
    assert photos[0]["size"] > 0


def test_export_command_sends_document(client):
    _post_message(client, "/start_day", 8, 0)
    _post_message(client, "/prod a=133", 9, 0)
    r = _post_message(client, "/export", 9, 5)
    assert r.status_code == 200
    docs = [m for m in client.sent if m["kind"] == "document"]
    assert len(docs) == 1
    assert os.path.exists(docs[0]["path"])


# --- Cloture immediate au point final -------------------------------------------

def test_final_point_triggers_immediate_synthese(client):
    _post_message(client, "/start_day", 8, 0)
    for heure, val in [("09:00", 133), ("10:00", 266), ("11:00", 399), ("12:00", 532),
                       ("13:00", 598), ("14:00", 731), ("15:00", 864), ("16:00", 997)]:
        h, m = (int(x) for x in heure.split(":"))
        _post_message(client, f"/a {val}", h, m)
    client.sent.clear()
    _post_message(client, "/a 1180", 16, 30)

    kinds = [m["kind"] for m in client.sent]
    assert "photo" in kinds  # la synthese avec son graphique est partie automatiquement
    assert any("SYNTHÈSE" in m["text"] for m in client.sent if m["kind"] == "photo")


# --- Poste 2 --------------------------------------------------------------------

def test_poste2_flow_via_commands(client):
    _post_message(client, "/start_day", 8, 0)
    _post_message(client, "/a 1180", 16, 30)  # cumul de fin de poste 1

    r = _post_message(client, "/a 100", 18, 0)  # poste 2 pas ouvert -> refus
    assert "poste 2 non ouvert" in client.sent[-1]["text"].lower()

    _post_message(client, "/poste2", 16, 35)
    r = _post_message(client, "/a 145", 17, 30)
    assert "Production : 145 pcs" in client.sent[-1]["text"]  # pas d'anomalie malgre la chute de cumul

    client.sent.clear()
    _post_message(client, "/fin", 17, 35)
    texts = [m["text"] for m in client.sent if m["kind"] == "text" or m["kind"] == "photo"]
    assert any("Poste 2" in t for t in texts if t)
