"""Client HTTP minimal pour l'API Telegram Bot (appels directs via `requests`)."""
import json
import os

import requests

from . import config

API_BASE = "https://api.telegram.org/bot{token}"


def send_message(chat_id, text, reply_markup=None, parse_mode=None):
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def send_photo(chat_id, photo_bytes, caption=None, reply_markup=None, parse_mode=None):
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/sendPhoto"
    files = {"photo": ("graph.png", photo_bytes, "image/png")}
    data = {"chat_id": chat_id}
    if caption is not None:
        data["caption"] = caption
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    if parse_mode is not None:
        data["parse_mode"] = parse_mode
    resp = requests.post(url, data=data, files=files, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_document(chat_id, file_path, caption=None):
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/sendDocument"
    with open(file_path, "rb") as f:
        files = {"document": (os.path.basename(file_path), f)}
        data = {"chat_id": chat_id}
        if caption is not None:
            data["caption"] = caption
        resp = requests.post(url, data=data, files=files, timeout=60)
    resp.raise_for_status()
    return resp.json()


def edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode=None):
    """Edite un message deja envoye (bot v2 B4.2 : editer plutot qu'empiler
    un nouveau message pour une confirmation/correction/navigation)."""
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    resp = requests.post(url, json=payload, timeout=10)
    if resp.status_code == 400 and "message is not modified" in resp.text.lower():
        return {"ok": True, "result": None}
    resp.raise_for_status()
    return resp.json()


def edit_message_reply_markup(chat_id, message_id, reply_markup=None):
    """Retire/remplace le clavier inline d'un message (bot v2 B4.3 :
    desactiver les boutons deja utilises pour empecher une double action)."""
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/editMessageReplyMarkup"
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup or {"inline_keyboard": []}}
    resp = requests.post(url, json=payload, timeout=10)
    if resp.status_code == 400 and "message is not modified" in resp.text.lower():
        return {"ok": True, "result": None}
    resp.raise_for_status()
    return resp.json()


def set_my_commands(commands):
    """`commands` : liste de {"command": "start", "description": "..."}."""
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/setMyCommands"
    resp = requests.post(url, json={"commands": commands}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def answer_callback_query(callback_query_id, text=None):
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def set_webhook(webhook_url):
    url = f"{API_BASE.format(token=config.BOT_TOKEN)}/setWebhook"
    resp = requests.post(url, json={"url": webhook_url}, timeout=10)
    resp.raise_for_status()
    return resp.json()
