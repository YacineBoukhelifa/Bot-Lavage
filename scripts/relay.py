"""Relais temporaire : poll Telegram getUpdates et route chaque update vers
l'app Flask locale (qui repond reellement via l'API Telegram). Sert a tester
le bot en conditions reelles avant le deploiement webhook."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
except ImportError:
    pass

os.environ.setdefault("WEBHOOK_SECRET", "demo-secret")
os.environ.setdefault("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "demo.sqlite3"))

if not os.environ.get("BOT_TOKEN"):
    raise SystemExit(
        "BOT_TOKEN manquant. Renseignez-le dans .env (voir .env.example) "
        "avant de lancer le relais — ne jamais coder un token en dur dans ce fichier."
    )

import requests

from bot import config
config.DB_PATH = os.environ["DB_PATH"]

from bot import app as app_module

client = app_module.app.test_client()
API = f"https://api.telegram.org/bot{config.BOT_TOKEN}"

def get_updates(offset):
    r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 25}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]

def main(duration_seconds):
    start = time.time()
    offset = 0
    # ignorer tout ce qui est deja en attente au demarrage
    initial = get_updates(0)
    if initial:
        offset = initial[-1]["update_id"] + 1
    print(f"relay demarre, offset initial={offset}", flush=True)

    while time.time() - start < duration_seconds:
        try:
            updates = get_updates(offset)
        except requests.exceptions.RequestException as exc:
            print(f"  getUpdates a echoue ({exc.__class__.__name__}), nouvelle tentative...", flush=True)
            time.sleep(3)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            kind = "message" if "message" in update else ("callback_query" if "callback_query" in update else "autre")
            print(f"update {update['update_id']} ({kind}) -> traitement...", flush=True)
            try:
                resp = client.post(f"/webhook/{config.WEBHOOK_SECRET}", json=update)
                print(f"  reponse webhook: {resp.status_code}", flush=True)
            except Exception as exc:  # noqa: BLE001 - le relais doit survivre a une erreur ponctuelle
                print(f"  erreur de traitement: {exc}", flush=True)

    print("relay termine (duree ecoulee)", flush=True)

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 600)
