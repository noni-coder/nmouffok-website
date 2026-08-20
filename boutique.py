#!/usr/bin/env python3
"""
Service boutique nmouffok.fr — paiement Stripe Checkout.
- POST /checkout  : crée une session de paiement à partir du panier (ids d'œuvres)
- POST /webhook   : confirmation Stripe -> marque vendue + notification Telegram
Tourne en local sur 127.0.0.1:8300, exposé par Nginx sous /api/boutique/.
"""
import json
import os
import tempfile

import requests as rq
import stripe
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv("/opt/nmouffok-shop/boutique.env")

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
SITE_URL = os.getenv("SITE_URL", "https://nmouffok.fr")
SITE_DIR = os.getenv("SITE_DIR", "/var/www/nmouffok")
OEUVRES = os.path.join(SITE_DIR, "data", "oeuvres.json")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
NOTIF_IDS = [i.strip() for i in os.getenv("NOTIF_IDS", "").split(",") if i.strip()]
PAYS_LIVRAISON = ["FR", "BE", "LU", "CH", "DE", "ES", "IT", "NL", "PT"]

app = Flask(__name__)


def lire_oeuvres():
    with open(OEUVRES, encoding="utf-8") as f:
        return json.load(f)


def ecrire_oeuvres(data):
    d = os.path.dirname(OEUVRES)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OEUVRES)


def notifier(texte):
    for cid in NOTIF_IDS:
        try:
            rq.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                    json={"chat_id": cid, "text": texte}, timeout=10)
        except Exception:
            pass


@app.post("/checkout")
def checkout():
    corps = request.get_json(silent=True) or {}
    ids = corps.get("ids", [])
    if not ids:
        return jsonify(erreur="Panier vide"), 400
    data = lire_oeuvres()
    lignes = []
    for oid in ids:
        o = next((x for x in data if x["id"] == oid), None)
        if not o:
            return jsonify(erreur=f"Œuvre {oid} introuvable"), 400
        if o.get("statut") == "vendue":
            return jsonify(erreur=f"« {o['title']} » vient d'être vendue"), 409
        if not o.get("price"):
            return jsonify(erreur=f"« {o['title']} » n'a pas encore de prix"), 400
        produit = {"name": o["title"],
                   "description": f"{o['catL']} · {o.get('tech', '')}".strip(" ·")}
        if o.get("img"):
            produit["images"] = [f"{SITE_URL}/{o['img']}"]
        lignes.append({
            "price_data": {"currency": "eur", "unit_amount": int(o["price"]) * 100,
                            "product_data": produit},
            "quantity": 1,
        })
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=lignes,
            locale="fr",
            success_url=f"{SITE_URL}/?paiement=ok",
            cancel_url=f"{SITE_URL}/?paiement=annule",
            shipping_address_collection={"allowed_countries": PAYS_LIVRAISON},
            metadata={"ids": ",".join(ids)},
        )
    except Exception as e:
        app.logger.error(f"Stripe : {e}")
        return jsonify(erreur="Paiement indisponible pour le moment"), 502
    return jsonify(url=session.url)


@app.post("/webhook")
def webhook():
    try:
        evt = stripe.Webhook.construct_event(
            request.data, request.headers.get("Stripe-Signature", ""), WEBHOOK_SECRET)
    except Exception:
        return "signature invalide", 400
    if evt["type"] == "checkout.session.completed":
        session = evt["data"]["object"]
        ids = (session.get("metadata", {}).get("ids") or "").split(",")
        data = lire_oeuvres()
        vendues = []
        for o in data:
            if o["id"] in ids and o.get("statut") != "vendue":
                o["statut"] = "vendue"
                vendues.append(f"« {o['title']} » ({o['id']}) : {o['price']} €")
        if vendues:
            ecrire_oeuvres(data)
            total = session.get("amount_total", 0) / 100
            notifier("🎉 VENTE sur nmouffok.fr !\n" + "\n".join(vendues) +
                     f"\nTotal encaissé : {total:.0f} €\n"
                     "L'œuvre est marquée vendue sur le site.")
    return "ok", 200


if __name__ == "__main__":
    from waitress import serve
    serve(app, host="127.0.0.1", port=8300)
