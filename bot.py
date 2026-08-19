#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Telegram de gestion de la galerie nmouffok.fr (Mima)
- Commandes : /liste /ajouter /texte /prix /vendu /dispo /supprimer
- Surveillance du dossier Google Drive : analyse IA + retouche photo + validation
Écrit dans /var/www/nmouffok/data/oeuvres.json et img/oeuvres/
"""

import os
import io
import json
import time
import logging
import unicodedata
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageEnhance, ImageChops, ImageFilter
import anthropic

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters,
)

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ------------------------------------------------------------------ CONFIG
BASE = Path(__file__).resolve().parent
load_dotenv(BASE / "config.env")

TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}

DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")
DRIVE_KEY_FILE = BASE / "gdrive.json"
DRIVE_POLL_MINUTES = int(os.environ.get("DRIVE_POLL_MINUTES", "5"))

SITE_DIR = Path(os.environ.get("SITE_DIR", "/var/www/nmouffok"))
JSON_PATH = SITE_DIR / "data" / "oeuvres.json"
IMG_DIR = SITE_DIR / "img" / "oeuvres"
SEEN_FILE = BASE / "drive_vus.json"
SALLES_PATH = SITE_DIR / "data" / "salles.json"

SALLES_DEFAUT = [
    {"id": "tableaux", "nom": "Salle principale", "emoji": "🎨", "groupe": "tableaux",
     "wall": "#F3EDE4", "floor": "#8B7260", "ceil": "#FAF7F2", "amb": "#FFE8D0"},
    {"id": "macrame", "nom": "Salle principale", "emoji": "🪢", "groupe": "macrame",
     "wall": "#E8DFD3", "floor": "#9B8B7A", "ceil": "#F3EDE4", "amb": "#E0E8D8"},
    {"id": "pyrogravure", "nom": "Salle principale", "emoji": "🔥", "groupe": "pyrogravure",
     "wall": "#E0D5C8", "floor": "#6B5A4A", "ceil": "#EDE4D8", "amb": "#FFDEC0"},
]

PALETTES = [
    {"wall": "#EFE6DA", "floor": "#7A6A58", "ceil": "#F8F4EC", "amb": "#FFE4C8"},
    {"wall": "#E4E9E2", "floor": "#6E7B6A", "ceil": "#F2F5F0", "amb": "#DDE8D5"},
    {"wall": "#EAE0E6", "floor": "#77606E", "ceil": "#F5EFF3", "amb": "#F0DCE8"},
    {"wall": "#E0E4EA", "floor": "#5E6876", "ceil": "#EFF2F6", "amb": "#D5E0F0"},
]

CATS = {"tableaux": "Tableau", "macrame": "Macramé", "pyrogravure": "Pyrogravure"}

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s", level=logging.INFO)
log = logging.getLogger("nmouffok-bot")

claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY) if ANTHROPIC_KEY else None

# ------------------------------------------------------------------ OUTILS
def autorise(update: Update) -> bool:
    u = update.effective_user
    ok = u and u.id in ALLOWED_IDS
    if not ok and u:
        log.warning("Accès refusé pour %s (%s)", u.id, u.full_name)
    return ok


def lire_oeuvres() -> list:
    with open(JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


def ecrire_oeuvres(data: list) -> None:
    tmp = JSON_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, JSON_PATH)


def lire_salles() -> list:
    if not SALLES_PATH.exists():
        with open(SALLES_PATH, "w", encoding="utf-8") as f:
            json.dump(SALLES_DEFAUT, f, ensure_ascii=False, indent=2)
        return list(SALLES_DEFAUT)
    with open(SALLES_PATH, encoding="utf-8") as f:
        salles = json.load(f)
    modifie = False
    for s in salles:
        if "groupe" not in s:
            s["groupe"] = s["id"] if s["id"] in CATS else "tableaux"
            modifie = True
    if modifie:
        ecrire_salles(salles)
    return salles


def ecrire_salles(salles: list) -> None:
    tmp = SALLES_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(salles, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SALLES_PATH)


def salle_de(o: dict) -> str:
    return o.get("salle") or o["cat"]


def nouvel_id(data: list, cat: str) -> str:
    prefixe = {"tableaux": "t", "macrame": "m", "pyrogravure": "p"}[cat]
    nums = [int(o["id"][1:]) for o in data if o["id"][:1] == prefixe and o["id"][1:].isdigit()]
    return f"{prefixe}{max(nums, default=0) + 1}"


def trouver(data: list, oid: str):
    return next((o for o in data if o["id"] == oid), None)


def slug(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return "".join(c if c.isalnum() else "-" for c in txt.lower()).strip("-")[:40] or "oeuvre"


def recadrer_oeuvre(img: Image.Image) -> Image.Image:
    """Détecte la toile photographiée sur un mur uni et rogne le mur autour.
    Prudent : si la détection est douteuse, l'image d'origine est conservée."""
    petit = img.copy()
    petit.thumbnail((400, 400), Image.LANCZOS)
    w, h = petit.size
    # Couleur du mur estimée par la médiane des 4 coins
    coins = [petit.getpixel((2, 2)), petit.getpixel((w - 3, 2)),
             petit.getpixel((2, h - 3)), petit.getpixel((w - 3, h - 3))]
    fond = tuple(sorted(c[i] for c in coins)[1] for i in range(3))
    diff_brut = ImageChops.difference(petit, Image.new("RGB", petit.size, fond)).convert("L")
    diff = diff_brut.point(lambda p: 255 if p > 35 else 0)
    diff = diff.filter(ImageFilter.MedianFilter(5))
    bbox = diff.getbbox()
    if not bbox:
        return img
    # Resserrage côté par côté : on avance chaque bord tant que la ligne/colonne
    # n'est pas franchement de la toile. Seuil haut (55) pour ne pas confondre
    # l'ombre portée sur le mur avec de la matière peinte.
    px = list(diff_brut.getdata())
    def col_ok(x, t0, b0):
        seg = [px[y * w + x] for y in range(t0, b0)]
        return sum(1 for v in seg if v > 55) / max(1, len(seg)) >= 0.70
    def row_ok(y, l0, r0):
        seg = px[y * w + l0:y * w + r0]
        return sum(1 for v in seg if v > 55) / max(1, len(seg)) >= 0.70
    l0, t0, r0, b0 = bbox
    lim_x, lim_y = int((r0 - l0) * 0.12), int((b0 - t0) * 0.12)
    while l0 < bbox[0] + lim_x and not col_ok(l0, t0, b0): l0 += 1
    while r0 > bbox[2] - lim_x and not col_ok(r0 - 1, t0, b0): r0 -= 1
    while t0 < bbox[1] + lim_y and not row_ok(t0, l0, r0): t0 += 1
    while b0 > bbox[3] - lim_y and not row_ok(b0 - 1, l0, r0): b0 -= 1
    sx, sy = img.width / w, img.height / h
    l, t = int(l0 * sx), int(t0 * sy)
    r, b = int(r0 * sx), int(b0 * sy)
    # Léger retrait supplémentaire (0.5 %) : ombres de bord
    mx, my = int((r - l) * 0.005), int((b - t) * 0.005)
    l, t, r, b = l + mx, t + my, r - mx, b - my
    aire = (r - l) * (b - t) / float(img.width * img.height)
    if aire < 0.25 or aire > 0.98 or r <= l or b <= t:
        return img  # détection douteuse : on ne touche à rien
    return img.crop((l, t, r, b))


def encadrer(img: Image.Image) -> Image.Image:
    """Encadrement de galeriste : filet doré autour de l'œuvre, passe-partout
    crème assorti au site, fin liseré doré extérieur. Désactivable avec
    CADRE_PHOTO=non dans config.env."""
    if os.getenv("CADRE_PHOTO", "oui").lower() in ("non", "no", "0", "false"):
        return img
    m = min(img.size)
    filet = max(3, int(m * 0.008))      # filet doré contre l'œuvre
    marge = max(20, int(m * 0.055))     # passe-partout crème
    lisere = max(2, int(m * 0.004))     # liseré doré extérieur
    dore = (176, 141, 87)
    creme = (250, 247, 242)
    img = ImageOps.expand(img, border=filet, fill=dore)
    img = ImageOps.expand(img, border=marge, fill=creme)
    img = ImageOps.expand(img, border=lisere, fill=dore)
    return img


def sublimer(brut: bytes) -> tuple:
    """Retouche douce : orientation, rognage du mur, lumière, couleurs, netteté.
    Retourne (jpeg_bytes, ratio largeur/hauteur) pour un cadre 3D fidèle."""
    img = Image.open(io.BytesIO(brut))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = recadrer_oeuvre(img)
    # Retouche volontairement discrète : fidélité aux couleurs réelles avant tout
    img = ImageEnhance.Contrast(img).enhance(1.06)
    img = ImageEnhance.Brightness(img).enhance(1.03)
    img = ImageEnhance.Color(img).enhance(1.05)
    img = ImageEnhance.Sharpness(img).enhance(1.15)
    img = encadrer(img)
    if max(img.size) > 1800:
        img.thumbnail((1800, 1800), Image.LANCZOS)
    ratio = round(img.width / img.height, 3)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue(), ratio


PLACES_PAR_SALLE = 10  # emplacements muraux dans la galerie 3D


def salles_du_groupe(groupe: str) -> list:
    """Salles d'une aile avec leur nombre de places libres, triées par espace."""
    data = lire_oeuvres()
    resultat = []
    for s in lire_salles():
        if s.get("groupe", s["id"]) != groupe:
            continue
        occupees = sum(1 for o in data if salle_de(o) == s["id"])
        resultat.append((s, PLACES_PAR_SALLE - occupees))
    resultat.sort(key=lambda x: -x[1])
    return resultat


def enregistrer_image(oid: str, titre: str, donnees: bytes) -> str:
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    nom = f"{oid}-{slug(titre)}.jpg"
    with open(IMG_DIR / nom, "wb") as f:
        f.write(donnees)
    return f"img/oeuvres/{nom}"


def analyser_photo(donnees: bytes) -> dict:
    """Demande à Claude un titre, une catégorie, une technique et un texte."""
    defaut = {"titre": "Nouvelle œuvre", "categorie": "tableaux",
              "technique": "", "description": ""}
    if not claude:
        return defaut
    import base64
    b64 = base64.standard_b64encode(donnees).decode()
    prompt = (
        "Tu es l'assistant éditorial de la galerie de Nadège Mouffok, artiste "
        "contemplative (site nmouffok.fr, ton poétique et doux). Analyse la photo "
        "de cette œuvre et réponds UNIQUEMENT avec un objet JSON, sans texte autour : "
        '{"titre": "...", "categorie": "tableaux|macrame|pyrogravure", '
        '"technique": "...", "description": "..."} '
        "Le titre est court et évocateur. La description fait 2 phrases poétiques "
        "dans le style du site. La technique est plausible d'après l'image."
    )
    try:
        rep = claude.messages.create(
            model=MODEL, max_tokens=400,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        txt = rep.content[0].text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        res = json.loads(txt)
        if res.get("categorie") not in CATS:
            res["categorie"] = "tableaux"
        return {**defaut, **res}
    except Exception as e:
        log.error("Analyse Claude impossible : %s", e)
        return defaut


# ------------------------------------------------------------------ GOOGLE DRIVE
def drive_service():
    creds = service_account.Credentials.from_service_account_file(
        str(DRIVE_KEY_FILE), scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_vus() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def drive_marquer(vus: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(vus)))


def drive_nouvelles_images():
    svc = drive_service()
    q = f"'{DRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed=false"
    res = svc.files().list(q=q, fields="files(id,name,createdTime)",
                           orderBy="createdTime").execute()
    vus = drive_vus()
    nouvelles = [f for f in res.get("files", []) if f["id"] not in vus]
    return svc, nouvelles, vus


def drive_telecharger(svc, fid: str) -> bytes:
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, svc.files().get_media(fileId=fid))
    fini = False
    while not fini:
        _, fini = dl.next_chunk()
    return buf.getvalue()


async def surveiller_drive(context: ContextTypes.DEFAULT_TYPE):
    """Tâche périodique : nouvelles photos Drive -> retouche -> IA -> validation."""
    if not DRIVE_FOLDER_ID or not DRIVE_KEY_FILE.exists():
        return
    try:
        svc, nouvelles, vus = drive_nouvelles_images()
    except Exception as e:
        log.error("Drive inaccessible : %s", e)
        return
    for f in nouvelles[:3]:  # au plus 3 par passage
        try:
            brut = drive_telecharger(svc, f["id"])
            photo, ratio = sublimer(brut)
            infos = analyser_photo(photo)
            cle = f"d{int(time.time() * 1000) % 10**9}"
            context.bot_data.setdefault("attente", {})[cle] = {
                "photo": photo, "infos": infos, "ratio": ratio}
            salles = salles_du_groupe(infos["categorie"])
            lignes_boutons = []
            for s, libres in salles[:4]:
                etiquette = f"🖼 {s['nom']}" + (f" ({libres} pl.)" if libres > 0 else " (complète)")
                lignes_boutons.append([InlineKeyboardButton(
                    etiquette, callback_data=f"dv:{cle}:{s['id']}")])
            lignes_boutons.append([InlineKeyboardButton("❌ Refuser", callback_data=f"dr:{cle}")])
            boutons = InlineKeyboardMarkup(lignes_boutons)
            meilleure = salles[0][0]["nom"] if salles else "?"
            fmt = "paysage" if ratio > 1.05 else ("carré" if ratio > 0.95 else "portrait")
            legende = (f"🖼 Nouvelle photo Drive : {f['name']}\n\n"
                       f"✨ Proposition :\n"
                       f"Titre : {infos['titre']}\n"
                       f"Catégorie : {CATS[infos['categorie']]}\n"
                       f"Technique : {infos['technique']}\n"
                       f"Format : {fmt} ({ratio})\n"
                       f"Texte : {infos['description']}\n\n"
                       f"📍 Emplacement conseillé : {meilleure}\n"
                       f"Choisis la salle où l'accrocher :")
            for uid in ALLOWED_IDS:
                try:
                    await context.bot.send_photo(uid, photo=photo, caption=legende, reply_markup=boutons)
                except Exception:
                    pass
            vus.add(f["id"])
        except Exception as e:
            log.error("Traitement Drive %s : %s", f.get("name"), e)
    drive_marquer(vus)


async def rep_drive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réponse aux boutons Valider/Refuser d'une photo Drive."""
    q = update.callback_query
    await q.answer()
    if not autorise(update):
        return
    parts = q.data.split(":")
    action, cle = parts[0], parts[1]
    salle_choisie = parts[2] if len(parts) > 2 else None
    item = context.bot_data.get("attente", {}).pop(cle, None)
    if not item:
        await q.edit_message_caption(caption="⏰ Proposition expirée (bot redémarré).")
        return
    if action == "dr":
        await q.edit_message_caption(caption="❌ Photo refusée, rien n'a été publié.")
        return
    data = lire_oeuvres()
    infos = item["infos"]
    oid = nouvel_id(data, infos["categorie"])
    chemin = enregistrer_image(oid, infos["titre"], item["photo"])
    salle_finale = salle_choisie or infos["categorie"]
    data.append({
        "id": oid, "title": infos["titre"], "cat": infos["categorie"],
        "catL": CATS[infos["categorie"]], "tech": infos["technique"],
        "desc": infos["description"], "dim": "", "price": 0,
        "c1": "#C4A282", "c2": "#A3B5A0", "img": chemin, "statut": "disponible",
        "salle": salle_finale, "ratio": item.get("ratio"),
    })
    ecrire_oeuvres(data)
    nom_salle = next((s["nom"] for s in lire_salles() if s["id"] == salle_finale), salle_finale)
    await q.edit_message_caption(
        caption=f"✅ Œuvre {oid} accrochée dans « {nom_salle} » !\n"
                f"À compléter : /prix {oid} <montant>\n"
                f"Pour ajuster : /titre {oid} <nom> · /texte {oid}")


# ------------------------------------------------------------------ COMMANDES SIMPLES
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    await update.message.reply_text(
        "🎨 Bot galerie nmouffok.fr\n\n"
        "/liste — voir les œuvres\n"
        "/ajouter — ajouter une œuvre (photo + infos)\n"
        "/titre <id> <nom> — renommer une œuvre\n"
        "/texte <id> — modifier le texte d'une œuvre\n"
        "/prix <id> <montant> — modifier un prix\n"
        "/vendu <id> — marquer vendue\n"
        "/dispo <id> — remettre disponible\n"
        "/supprimer <id> — retirer une œuvre\n\n"
        "🏛 Salles : /salles · /nouvellesalle · /deplacer · /supprimersalle\n\n"
        "📁 Les photos déposées dans le dossier Drive partagé sont "
        "proposées ici automatiquement.")


async def cmd_liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    data = lire_oeuvres()
    if not data:
        await update.message.reply_text("La galerie est vide.")
        return
    lignes = []
    for c, label in CATS.items():
        oeuvres = [o for o in data if o["cat"] == c]
        if oeuvres:
            lignes.append(f"— {label}s —")
            for o in oeuvres:
                etat = "🔴 vendue" if o.get("statut") == "vendue" else "🟢"
                photo = "📷" if o.get("img") else "▫️"
                lignes.append(f"{etat} {photo} {o['id']} · {o['title']} · {o['price']} €")
    await update.message.reply_text("\n".join(lignes))


async def cmd_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    try:
        oid, montant = context.args[0], int(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Utilisation : /prix t3 490")
        return
    data = lire_oeuvres()
    o = trouver(data, oid)
    if not o:
        await update.message.reply_text(f"Œuvre {oid} introuvable (voir /liste).")
        return
    o["price"] = montant
    ecrire_oeuvres(data)
    await update.message.reply_text(f"💶 « {o['title']} » ({oid}) : {montant} €")


async def _statut(update: Update, context: ContextTypes.DEFAULT_TYPE, statut: str):
    if not autorise(update):
        return
    if not context.args:
        await update.message.reply_text(f"Utilisation : /{'vendu' if statut=='vendue' else 'dispo'} t3")
        return
    data = lire_oeuvres()
    o = trouver(data, context.args[0])
    if not o:
        await update.message.reply_text("Œuvre introuvable (voir /liste).")
        return
    o["statut"] = statut
    ecrire_oeuvres(data)
    emoji = "🔴" if statut == "vendue" else "🟢"
    await update.message.reply_text(f"{emoji} « {o['title']} » est maintenant {statut}.")


async def cmd_vendu(update, context):
    await _statut(update, context, "vendue")


async def cmd_dispo(update, context):
    await _statut(update, context, "disponible")


async def cmd_supprimer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    if not context.args:
        await update.message.reply_text("Utilisation : /supprimer t3")
        return
    oid = context.args[0]
    o = trouver(lire_oeuvres(), oid)
    if not o:
        await update.message.reply_text("Œuvre introuvable (voir /liste).")
        return
    boutons = InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Oui, retirer", callback_data=f"sup:{oid}"),
        InlineKeyboardButton("Annuler", callback_data="sup:non"),
    ]])
    await update.message.reply_text(
        f"Retirer définitivement « {o['title']} » ({oid}) de la galerie ?", reply_markup=boutons)


async def rep_supprimer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not autorise(update):
        return
    oid = q.data.split(":", 1)[1]
    if oid == "non":
        await q.edit_message_text("Suppression annulée.")
        return
    data = lire_oeuvres()
    o = trouver(data, oid)
    if not o:
        await q.edit_message_text("Œuvre déjà retirée.")
        return
    data.remove(o)
    ecrire_oeuvres(data)
    await q.edit_message_text(f"🗑 « {o['title']} » a été retirée de la galerie.")


# ------------------------------------------------------------------ SALLES
async def cmd_salles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    salles = lire_salles()
    data = lire_oeuvres()
    lignes = ["🏛 Salles de la galerie :"]
    for gid, glabel in CATS.items():
        du_groupe = [s for s in salles if s.get("groupe", s["id"]) == gid]
        if du_groupe:
            lignes.append(f"\n— Aile {glabel}s —")
            for s in du_groupe:
                n = sum(1 for o in data if salle_de(o) == s["id"])
                lignes.append(f"{s.get('emoji','🖼')} {s['id']} · {s['nom']} · {n} œuvre(s)")
    lignes.append("\n/nouvellesalle <aile> <emoji> <nom> — créer une salle")
    lignes.append("   (aile : tableaux, macrame ou pyrogravure)")
    lignes.append("/deplacer <œuvre> <salle> — déplacer une œuvre")
    lignes.append("/supprimersalle <salle> — retirer une salle vide")
    await update.message.reply_text("\n".join(lignes))


async def cmd_nouvellesalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    args = list(context.args)
    if not args or args[0] not in CATS:
        await update.message.reply_text(
            "Utilisation : /nouvellesalle <aile> <emoji> <nom>\n"
            "Ailes possibles : tableaux, macrame, pyrogravure\n"
            "Exemple : /nouvellesalle tableaux 🌙 Petits formats")
        return
    groupe = args.pop(0)
    emoji = ""
    if args and not args[0].isalnum() and len(args[0]) <= 3:
        emoji = args.pop(0)
    nom = " ".join(args).strip()
    if not nom:
        await update.message.reply_text("Il manque le nom de la salle.")
        return
    salles = lire_salles()
    sid = slug(nom)[:20]
    if any(s["id"] == sid for s in salles):
        await update.message.reply_text(f"Une salle « {sid} » existe déjà.")
        return
    pal = PALETTES[len(salles) % len(PALETTES)]
    salles.append({"id": sid, "nom": nom, "emoji": emoji or "🖼", "groupe": groupe, **pal})
    ecrire_salles(salles)
    await update.message.reply_text(
        f"🏛 Salle « {nom} » créée dans l'aile {CATS[groupe]}s (id : {sid}).\n"
        f"Déplace des œuvres avec : /deplacer t1 {sid}")


async def cmd_deplacer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Utilisation : /deplacer t3 petits-formats\n(voir /liste et /salles)")
        return
    oid, sid = context.args[0], context.args[1]
    data = lire_oeuvres()
    o = trouver(data, oid)
    if not o:
        await update.message.reply_text(f"Œuvre {oid} introuvable (voir /liste).")
        return
    salles = lire_salles()
    s = next((x for x in salles if x["id"] == sid), None)
    if not s:
        await update.message.reply_text(f"Salle {sid} introuvable (voir /salles).")
        return
    o["salle"] = sid
    ecrire_oeuvres(data)
    await update.message.reply_text(
        f"🚚 « {o['title']} » accrochée dans {s.get('emoji','🖼')} {s['nom']}.")


async def cmd_supprimersalle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return
    if not context.args:
        await update.message.reply_text("Utilisation : /supprimersalle petits-formats")
        return
    sid = context.args[0]
    salles = lire_salles()
    s = next((x for x in salles if x["id"] == sid), None)
    if not s:
        await update.message.reply_text(f"Salle {sid} introuvable (voir /salles).")
        return
    if len(salles) <= 1:
        await update.message.reply_text("Impossible : c'est la dernière salle de la galerie.")
        return
    data = lire_oeuvres()
    n = sum(1 for o in data if salle_de(o) == sid)
    if n:
        await update.message.reply_text(
            f"La salle contient encore {n} œuvre(s). Déplace-les d'abord avec /deplacer.")
        return
    salles.remove(s)
    ecrire_salles(salles)
    await update.message.reply_text(f"🏛 Salle « {s['nom']} » retirée.")


async def cmd_titre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Renommer une œuvre : /titre t1 Nouveau nom"""
    if not autorise(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Utilisation : /titre t1 Le nouveau titre")
        return
    oid = context.args[0]
    nouveau = " ".join(context.args[1:]).strip()
    data = lire_oeuvres()
    o = trouver(data, oid)
    if not o:
        await update.message.reply_text(f"Œuvre {oid} introuvable (voir /liste).")
        return
    ancien = o["title"]
    o["title"] = nouveau
    ecrire_oeuvres(data)
    await update.message.reply_text(f"✏️ « {ancien} » s'appelle désormais « {nouveau} ».")


# ------------------------------------------------------------------ /texte (dialogue)
T_ATTENTE = 0

async def cmd_texte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return ConversationHandler.END
    if not context.args:
        await update.message.reply_text("Utilisation : /texte t3")
        return ConversationHandler.END
    o = trouver(lire_oeuvres(), context.args[0])
    if not o:
        await update.message.reply_text("Œuvre introuvable (voir /liste).")
        return ConversationHandler.END
    context.user_data["texte_id"] = o["id"]
    await update.message.reply_text(
        f"Texte actuel de « {o['title']} » :\n\n{o['desc']}\n\n"
        "Envoie le nouveau texte (ou /annuler).")
    return T_ATTENTE


async def texte_recu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = lire_oeuvres()
    o = trouver(data, context.user_data.pop("texte_id"))
    o["desc"] = update.message.text.strip()
    ecrire_oeuvres(data)
    await update.message.reply_text(f"✍️ Texte de « {o['title']} » mis à jour.")
    return ConversationHandler.END


# ------------------------------------------------------------------ /ajouter (dialogue)
A_PHOTO, A_TITRE, A_CAT, A_TECH, A_DESC, A_DIM, A_PRIX = range(1, 8)

async def cmd_ajouter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not autorise(update):
        return ConversationHandler.END
    context.user_data["nouv"] = {}
    await update.message.reply_text("📷 Envoie la photo de l'œuvre (ou /annuler).")
    return A_PHOTO


async def aj_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    fichier = await photo.get_file()
    brut = bytes(await fichier.download_as_bytearray())
    retouchee, ratio = sublimer(brut)
    context.user_data["nouv"]["photo"] = retouchee
    context.user_data["nouv"]["ratio"] = ratio
    infos = analyser_photo(retouchee)
    context.user_data["nouv"]["sugg"] = infos
    await update.message.reply_text(
        f"✨ Suggestion IA : « {infos['titre']} »\n"
        f"Quel est le titre ? (envoie « ok » pour garder la suggestion)")
    return A_TITRE


async def aj_titre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data["nouv"]
    txt = update.message.text.strip()
    n["titre"] = n["sugg"]["titre"] if txt.lower() == "ok" else txt
    boutons = InlineKeyboardMarkup([[InlineKeyboardButton(lbl, callback_data=f"cat:{c}")]
                                    for c, lbl in CATS.items()])
    await update.message.reply_text("Catégorie ?", reply_markup=boutons)
    return A_CAT


async def aj_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["nouv"]["cat"] = q.data.split(":")[1]
    sugg = context.user_data["nouv"]["sugg"]["technique"]
    await q.edit_message_text(
        f"Technique ? (suggestion : « {sugg} », envoie « ok » pour la garder)")
    return A_TECH


async def aj_tech(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data["nouv"]
    txt = update.message.text.strip()
    n["tech"] = n["sugg"]["technique"] if txt.lower() == "ok" else txt
    await update.message.reply_text(
        f"Texte sous l'œuvre ? Suggestion :\n\n{n['sugg']['description']}\n\n"
        "(envoie « ok » pour la garder)")
    return A_DESC


async def aj_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data["nouv"]
    txt = update.message.text.strip()
    n["desc"] = n["sugg"]["description"] if txt.lower() == "ok" else txt
    await update.message.reply_text("Dimensions ? (ex. : 80 × 60 cm)")
    return A_DIM


async def aj_dim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nouv"]["dim"] = update.message.text.strip()
    await update.message.reply_text("Prix en euros ? (ex. : 450)")
    return A_PRIX


async def aj_prix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = context.user_data.pop("nouv")
    try:
        prix = int(update.message.text.strip().replace("€", "").strip())
    except ValueError:
        context.user_data["nouv"] = n
        await update.message.reply_text("Un nombre entier, par exemple : 450")
        return A_PRIX
    data = lire_oeuvres()
    oid = nouvel_id(data, n["cat"])
    chemin = enregistrer_image(oid, n["titre"], n["photo"])
    data.append({
        "id": oid, "title": n["titre"], "cat": n["cat"], "catL": CATS[n["cat"]],
        "tech": n["tech"], "desc": n["desc"], "dim": n["dim"], "price": prix,
        "c1": "#C4A282", "c2": "#A3B5A0", "img": chemin, "statut": "disponible",
        "salle": n["cat"], "ratio": n.get("ratio"),
    })
    ecrire_oeuvres(data)
    await update.message.reply_text(
        f"🎨 « {n['titre']} » ({oid}) est en ligne : {prix} € — https://nmouffok.fr")
    return ConversationHandler.END


async def cmd_annuler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Action annulée.")
    return ConversationHandler.END


# ------------------------------------------------------------------ LANCEMENT
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler(["start", "aide", "help"], cmd_start))
    app.add_handler(CommandHandler("liste", cmd_liste))
    app.add_handler(CommandHandler("prix", cmd_prix))
    app.add_handler(CommandHandler("titre", cmd_titre))
    app.add_handler(CommandHandler("vendu", cmd_vendu))
    app.add_handler(CommandHandler("dispo", cmd_dispo))
    app.add_handler(CommandHandler("supprimer", cmd_supprimer))
    app.add_handler(CommandHandler("salles", cmd_salles))
    app.add_handler(CommandHandler("nouvellesalle", cmd_nouvellesalle))
    app.add_handler(CommandHandler("deplacer", cmd_deplacer))
    app.add_handler(CommandHandler("supprimersalle", cmd_supprimersalle))
    app.add_handler(CallbackQueryHandler(rep_supprimer, pattern=r"^sup:"))
    app.add_handler(CallbackQueryHandler(rep_drive, pattern=r"^(dv|dr):"))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("texte", cmd_texte)],
        states={T_ATTENTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, texte_recu)]},
        fallbacks=[CommandHandler("annuler", cmd_annuler)],
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("ajouter", cmd_ajouter)],
        states={
            A_PHOTO: [MessageHandler(filters.PHOTO, aj_photo)],
            A_TITRE: [MessageHandler(filters.TEXT & ~filters.COMMAND, aj_titre)],
            A_CAT: [CallbackQueryHandler(aj_cat, pattern=r"^cat:")],
            A_TECH: [MessageHandler(filters.TEXT & ~filters.COMMAND, aj_tech)],
            A_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, aj_desc)],
            A_DIM: [MessageHandler(filters.TEXT & ~filters.COMMAND, aj_dim)],
            A_PRIX: [MessageHandler(filters.TEXT & ~filters.COMMAND, aj_prix)],
        },
        fallbacks=[CommandHandler("annuler", cmd_annuler)],
    ))

    if DRIVE_FOLDER_ID and DRIVE_KEY_FILE.exists():
        app.job_queue.run_repeating(surveiller_drive, interval=DRIVE_POLL_MINUTES * 60, first=20)
        log.info("Surveillance Drive active (toutes les %s min)", DRIVE_POLL_MINUTES)
    else:
        log.warning("Surveillance Drive inactive (dossier ou clé manquants)")

    log.info("Bot nmouffok démarré")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
