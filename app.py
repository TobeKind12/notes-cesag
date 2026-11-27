"""Notes-Cesag — application Flask serverless pour Vercel.

Authentification et données : Supabase (REST + RLS).
Envoi d'e-mails : Resend (batch) ou Brevo.
Aucun état serveur persistant : les aperçus sont transmis par formulaire.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import re
import secrets as pysecrets
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pandas as pd
import requests
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY") or pysecrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024

APP_NAME = "Notes-Cesag"
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "100"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_NAME = os.getenv("SENDER_NAME", APP_NAME)
REPLY_TO = os.getenv("REPLY_TO", SENDER_EMAIL)


# =========================
# Sécurité / contexte
# =========================

@app.before_request
def csrf_protect():
    if "csrf_token" not in session:
        session["csrf_token"] = pysecrets.token_hex(16)
    if request.method == "POST":
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
        if not token or not hmac.compare_digest(token, session["csrf_token"]):
            abort(400, "Jeton CSRF invalide.")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.context_processor
def inject_globals():
    return {
        "csrf_token": session.get("csrf_token", ""),
        "current_user": current_user(),
        "DAILY_LIMIT": DAILY_LIMIT,
        "SENDER_NAME": SENDER_NAME,
        "APP_NAME": APP_NAME,
    }


# =========================
# Utilitaires
# =========================

def clean(value) -> str:
    text = unicodedata.normalize("NFD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"['’\-]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def txt(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def email_valide(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def parse_note(value):
    s = txt(value).replace(",", ".")
    if not s:
        return None, False
    try:
        note = float(s)
    except ValueError:
        return None, False
    return (note, True) if 0 <= note <= 20 else (None, False)


def fmt_note(note: float) -> str:
    return f"{note:g}".replace(".", ",")


def colonnes(df: pd.DataFrame) -> dict[str, str]:
    return {clean(c): c for c in df.columns}


def lire_fichier(data: bytes, name: str) -> pd.DataFrame:
    name = name.lower()
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(data))
    try:
        text = data.decode("utf-8-sig")
        enc = "utf-8-sig"
    except UnicodeDecodeError:
        text = data.decode("latin-1")
        enc = "latin-1"
    first = text.splitlines()[0] if text.splitlines() else ""
    sep = ";" if first.count(";") > first.count(",") else ","
    return pd.read_csv(io.BytesIO(data), encoding=enc, sep=sep, dtype=str)


def public_url() -> str:
    if SITE_URL:
        return SITE_URL
    return request.host_url.rstrip("/")


# =========================
# Supabase REST
# =========================

def sb_base() -> str:
    base = SUPABASE_URL.rstrip("/")
    if base.endswith("/rest/v1"):
        base = base[: -len("/rest/v1")]
    return base


def sb_headers(token: str | None = None, prefer: str | None = None) -> dict[str, str]:
    headers = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if prefer:
        headers["Prefer"] = prefer
    return headers


def sb_request(method: str, path: str, *, token: str | None = None,
               json_body=None, params=None, prefer: str | None = None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Secrets Supabase manquants (SUPABASE_URL, SUPABASE_KEY).")
    url = f"{sb_base()}{path}"
    response = requests.request(
        method, url, headers=sb_headers(token, prefer),
        json=json_body, params=params, timeout=20,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Supabase {response.status_code} : {response.text[:300]}")
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def sb_call(method: str, path: str, **kwargs):
    """Appel Supabase avec rafraîchissement automatique du token."""
    token = session.get("access_token")
    try:
        return sb_request(method, path, token=token, **kwargs)
    except RuntimeError as exc:
        if "401" not in str(exc) or not session.get("refresh_token"):
            raise
        data = sb_request(
            "POST", "/auth/v1/token?grant_type=refresh_token",
            json_body={"refresh_token": session["refresh_token"]},
        )
        session["access_token"] = data["access_token"]
        session["refresh_token"] = data.get("refresh_token", session["refresh_token"])
        session["user"] = data.get("user", session.get("user"))
        session.modified = True
        return sb_request(method, path, token=session["access_token"], **kwargs)


def eq(value) -> str:
    return quote(str(value), safe="")


def api_get(table: str, filters: dict[str, str], select: str = "*", order: str | None = None,
            limit: int | None = None):
    params = {"select": select}
    for key, value in filters.items():
        params[key] = f"eq.{value}"
    if order:
        params["order"] = order
    if limit:
        params["limit"] = str(limit)
    return sb_call("GET", f"/rest/v1/{table}", params=params) or []


def api_insert(table: str, payload):
    return sb_call("POST", f"/rest/v1/{table}", json_body=payload,
                   prefer="return=representation")


def api_update(table: str, filters: dict[str, str], payload: dict):
    params = {key: f"eq.{value}" for key, value in filters.items()}
    return sb_call("PATCH", f"/rest/v1/{table}", params=params, json_body=payload,
                   prefer="return=representation")


def api_delete(table: str, filters: dict[str, str]):
    params = {key: f"eq.{value}" for key, value in filters.items()}
    return sb_call("DELETE", f"/rest/v1/{table}", params=params,
                   prefer="return=representation")


def auth_signup(email: str, password: str, full_name: str):
    return sb_request(
        "POST", "/auth/v1/signup",
        json_body={"email": email, "password": password,
                   "options": {"data": {"full_name": full_name}}},
    )


def auth_signin(email: str, password: str):
    return sb_request(
        "POST", "/auth/v1/token?grant_type=password",
        json_body={"email": email, "password": password},
    )


def auth_recover(email: str, redirect_to: str):
    return sb_request("POST", "/auth/v1/recover",
                      json_body={"email": email, "redirect_to": redirect_to})


# =========================
# Session utilisateur
# =========================

def current_user():
    return session.get("user")


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            flash("Connecte-toi pour accéder à cette page.", "error")
            return redirect(url_for("home"))
        return view(*args, **kwargs)

    return wrapped


def clear_session():
    session.pop("access_token", None)
    session.pop("refresh_token", None)
    session.pop("user", None)


# =========================
# Classes / étudiants
# =========================

def fetch_classes(user_id: str):
    return api_get("classes", {"user_id": user_id}, order="created_at")


def fetch_students(class_id: str, user_id: str | None = None):
    filters = {"class_id": class_id}
    if user_id:
        filters["user_id"] = user_id
    return api_get("students", filters, order="nom")


def validate_class_file(df: pd.DataFrame):
    cols = colonnes(df)
    missing = {"nom", "prenom", "email"} - cols.keys()
    if missing:
        return [], [f"Colonnes manquantes : {', '.join(sorted(missing))}"]
    errors, rows, seen = [], [], set()
    for i, row in df.iterrows():
        nom = txt(row[cols["nom"]])
        prenom = txt(row[cols["prenom"]])
        email = txt(row[cols["email"]]).lower()
        if not nom or not prenom or not email:
            errors.append(f"Ligne {i + 2} : nom, prénom et e-mail requis.")
            continue
        if not email_valide(email):
            errors.append(f"Ligne {i + 2} ({nom} {prenom}) : e-mail invalide « {email} ».")
            continue
        if email in seen:
            errors.append(f"Ligne {i + 2} : doublon d'e-mail « {email} ».")
            continue
        seen.add(email)
        rows.append({"nom": nom, "prenom": prenom, "email": email})
    return rows, errors


def insert_students(user_id: str, class_id: str, rows: list[dict]):
    payload = [{"user_id": user_id, "class_id": class_id, **row} for row in rows]
    if payload:
        api_insert("students", payload)


def build_index(students: list[dict]):
    index: dict[tuple[str, str], list[dict]] = {}
    for student in students:
        keys = [
            (clean(student["nom"]), clean(student["prenom"])),
            (clean(student["prenom"]), clean(student["nom"])),
        ]
        for key in keys:
            bucket = index.setdefault(key, [])
            if student["id"] not in {item["id"] for item in bucket}:
                bucket.append(student)
    return index


def preparer_apercu(notes_df: pd.DataFrame, students: list[dict]):
    cols = colonnes(notes_df)
    missing = {"nom", "prenom", "note"} - cols.keys()
    if missing:
        raise ValueError("Colonnes manquantes : " + ", ".join(sorted(missing)))
    index = build_index(students)
    rows = []
    for i, row in notes_df.iterrows():
        nom = txt(row[cols["nom"]])
        prenom = txt(row[cols["prenom"]])
        note, note_ok = parse_note(row[cols["note"]])
        matches = index.get((clean(nom), clean(prenom)), [])
        if not note_ok:
            status = "Note invalide (vide ou hors 0-20)"
            match_id = ""
        elif len(matches) == 1:
            status = "OK"
            match_id = matches[0]["id"]
        elif len(matches) > 1:
            status = "Plusieurs correspondances possibles"
            match_id = ""
        else:
            status = "Introuvable dans la classe"
            match_id = ""
        chosen = next((m for m in matches if m["id"] == match_id),
                      matches[0] if len(matches) == 1 else None)
        rows.append({
            "row_id": i,
            "Nom": nom,
            "Prénom": chosen["prenom"] if chosen else prenom,
            "E-mail trouvé": chosen["email"] if chosen else "",
            "Note": fmt_note(note) if note_ok else txt(row[cols["note"]]),
            "Vérification": status,
            "student_id": match_id,
        })
    return rows


# =========================
# Historique / limites
# =========================

def fetch_history(user_id: str):
    return api_get("send_history", {"user_id": user_id}, order="sent_at.desc", limit=200)


def sent_today(user_id: str) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = sb_call(
        "GET", "/rest/v1/send_history",
        params={"select": "sent_count", "user_id": f"eq.{user_id}", "sent_at": f"gte.{start}"},
    ) or []
    return sum(int(row.get("sent_count", 0)) for row in rows)


def already_sent(history: list[dict], class_id: str, matiere: str, eval_type: str) -> bool:
    return any(
        str(row.get("class_id")) == str(class_id)
        and clean(row.get("matiere")) == clean(matiere)
        and clean(row.get("eval_type")) == clean(eval_type)
        for row in history
    )


# =========================
# E-mails
# =========================

def build_message(prenom: str, matiere: str, eval_type: str, note: str, sender_name: str) -> str:
    return (
        f"Bonjour {prenom}, ta note de {matiere} ({eval_type}) est : {note}/20. "
        f"Envoyé par {sender_name}."
    )


def send_batch_resend(messages: list[dict]):
    if not messages:
        return
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY non configuré.")
    sender = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    for start in range(0, len(messages), 100):
        chunk = messages[start:start + 100]
        payload = [
            {
                "from": sender,
                "to": [msg["to"]],
                "subject": msg["subject"],
                "text": msg["body"],
                "reply_to": REPLY_TO,
            }
            for msg in chunk
        ]
        response = requests.post(
            "https://api.resend.com/emails/batch",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"Resend {response.status_code} : {response.text[:200]}")


def send_one_brevo(msg: dict):
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY non configuré.")
    response = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json",
                 "accept": "application/json"},
        json={
            "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
            "to": [{"email": msg["to"]}],
            "subject": msg["subject"],
            "textContent": msg["body"],
            "replyTo": {"email": REPLY_TO, "name": SENDER_NAME},
        },
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Brevo {response.status_code} : {response.text[:200]}")


def envoyer_notes(rows: list[dict], matiere: str, eval_type: str, sender_name: str):
    messages = [
        {
            "to": row["email"],
            "subject": f"Note {matiere} ({eval_type})",
            "body": build_message(row["prenom"], matiere, eval_type, row["note"], sender_name),
        }
        for row in rows
        if email_valide(row.get("email", "")) and row.get("prenom")
    ]
    if not messages:
        return 0, []

    if RESEND_API_KEY:
        try:
            send_batch_resend(messages)
            return len(messages), []
        except Exception as exc:
            return 0, [f"Envoi groupé Resend : {exc}"]

    if BREVO_API_KEY:
        sent, failures = 0, []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(send_one_brevo, msg): msg for msg in messages}
            for future in as_completed(futures):
                msg = futures[future]
                try:
                    future.result()
                    sent += 1
                except Exception as exc:
                    failures.append(f"{msg['to']} : {exc}")
        return sent, failures

    return 0, ["Aucun service d'e-mail configuré (RESEND_API_KEY ou BREVO_API_KEY)."]


# =========================
# Routes
# =========================

@app.route("/")
def home():
    if current_user():
        return redirect(url_for("classes_page"))
    return render_template("auth.html")


@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    try:
        data = auth_signin(email, password)
        session["access_token"] = data["access_token"]
        session["refresh_token"] = data.get("refresh_token", "")
        session["user"] = data.get("user", {})
        flash("Connexion réussie.", "success")
        return redirect(url_for("classes_page"))
    except Exception as exc:
        flash(f"Connexion impossible : {exc}", "error")
        return redirect(url_for("home"))


@app.route("/signup", methods=["POST"])
def signup():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    if not name or not email_valide(email) or len(password) < 8:
        flash("Nom, e-mail valide et mot de passe de 8 caractères minimum requis.", "error")
        return redirect(url_for("home"))
    try:
        auth_signup(email, password, name)
        flash("Compte créé. Confirme ton e-mail puis connecte-toi.", "success")
    except Exception as exc:
        flash(f"Inscription impossible : {exc}", "error")
    return redirect(url_for("home"))


@app.route("/reset", methods=["POST"])
def reset_password():
    email = request.form.get("email", "").strip()
    if not email_valide(email):
        flash("Indique une adresse e-mail valide.", "error")
        return redirect(url_for("home"))
    try:
        auth_recover(email, public_url())
        flash("Si un compte existe, un lien de réinitialisation a été envoyé.", "success")
    except Exception as exc:
        flash(f"Demande impossible : {exc}", "error")
    return redirect(url_for("home"))


@app.route("/logout", methods=["POST"])
def logout():
    clear_session()
    flash("Tu es déconnecté.", "success")
    return redirect(url_for("home"))


@app.route("/classes")
@login_required
def classes_page():
    user = current_user()
    classes = fetch_classes(user["id"])
    class_id = request.args.get("class") or (classes[0]["id"] if classes else None)
    students = fetch_students(class_id, user["id"]) if class_id else []
    edit_id = request.args.get("edit")
    editing = next((s for s in students if s["id"] == edit_id), None)
    return render_template(
        "classes.html", classes=classes, selected_class=class_id,
        students=students, editing=editing,
    )


@app.route("/classes/create", methods=["POST"])
@login_required
def class_create():
    user = current_user()
    name = request.form.get("name", "").strip()
    upload = request.files.get("file")
    if not name or not upload or not upload.filename:
        flash("Nom et fichier de classe requis.", "error")
        return redirect(url_for("classes_page"))
    try:
        df = lire_fichier(upload.read(), upload.filename)
        rows, errors = validate_class_file(df)
        if errors:
            for error in errors[:10]:
                flash(error, "error")
        elif not rows:
            flash("Aucun étudiant valide trouvé.", "error")
        else:
            created = api_insert("classes", {"user_id": user["id"], "name": name})
            class_id = created[0]["id"]
            insert_students(user["id"], class_id, rows)
            flash(f"Classe « {name} » créée avec {len(rows)} étudiant(s).", "success")
            return redirect(url_for("classes_page", **{"class": class_id}))
    except Exception as exc:
        flash(f"Fichier illisible : {exc}", "error")
    return redirect(url_for("classes_page"))


@app.route("/classes/<class_id>/students/add", methods=["POST"])
@login_required
def student_add(class_id: str):
    user = current_user()
    nom = request.form.get("nom", "").strip()
    prenom = request.form.get("prenom", "").strip()
    email = request.form.get("email", "").strip().lower()
    if not (nom and prenom and email_valide(email)):
        flash("Nom, prénom et e-mail valide requis.", "error")
    else:
        insert_students(user["id"], class_id, [{"nom": nom, "prenom": prenom, "email": email}])
        flash("Étudiant ajouté.", "success")
    return redirect(url_for("classes_page", **{"class": class_id}))


@app.route("/students/<student_id>/update", methods=["POST"])
@login_required
def student_update(student_id: str):
    user = current_user()
    class_id = request.form.get("class_id", "")
    nom = request.form.get("nom", "").strip()
    prenom = request.form.get("prenom", "").strip()
    email = request.form.get("email", "").strip().lower()
    if not (nom and prenom and email_valide(email)):
        flash("Nom, prénom et e-mail valide requis.", "error")
    else:
        api_update("students", {"id": student_id, "user_id": user["id"]},
                   {"nom": nom, "prenom": prenom, "email": email})
        flash("Étudiant modifié.", "success")
    return redirect(url_for("classes_page", **{"class": class_id}))


@app.route("/students/<student_id>/delete", methods=["POST"])
@login_required
def student_delete(student_id: str):
    user = current_user()
    class_id = request.form.get("class_id", "")
    api_delete("students", {"id": student_id, "user_id": user["id"]})
    flash("Étudiant supprimé.", "success")
    return redirect(url_for("classes_page", **{"class": class_id}))


@app.route("/classes/<class_id>/students/reload", methods=["POST"])
@login_required
def students_reload(class_id: str):
    user = current_user()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        flash("Fichier requis.", "error")
        return redirect(url_for("classes_page", **{"class": class_id}))
    try:
        df = lire_fichier(upload.read(), upload.filename)
        rows, errors = validate_class_file(df)
        if errors:
            for error in errors[:10]:
                flash(error, "error")
        else:
            api_delete("students", {"class_id": class_id, "user_id": user["id"]})
            insert_students(user["id"], class_id, rows)
            flash(f"Liste remplacée : {len(rows)} étudiant(s).", "success")
    except Exception as exc:
        flash(f"Fichier illisible : {exc}", "error")
    return redirect(url_for("classes_page", **{"class": class_id}))


@app.route("/classes/<class_id>/delete", methods=["POST"])
@login_required
def class_delete(class_id: str):
    user = current_user()
    classes = fetch_classes(user["id"])
    target = next((c for c in classes if c["id"] == class_id), None)
    if not target:
        abort(404)
    if request.form.get("confirmation", "").strip() != target["name"]:
        flash("Le nom de la classe ne correspond pas.", "error")
        return redirect(url_for("classes_page", **{"class": class_id}))
    api_delete("classes", {"id": class_id, "user_id": user["id"]})
    flash("Classe et données associées supprimées.", "success")
    return redirect(url_for("classes_page"))


@app.route("/send", methods=["GET", "POST"])
@login_required
def send_page():
    user = current_user()
    classes = fetch_classes(user["id"])
    if not classes:
        flash("Crée d'abord une classe.", "error")
        return redirect(url_for("classes_page"))

    if request.method == "POST":
        class_id = request.form.get("class_id", "")
        matiere = request.form.get("matiere", "").strip()
        eval_type = request.form.get("eval_type", "Test")
        upload = request.files.get("file")
        selected = next((c for c in classes if c["id"] == class_id), None)
        if not selected or not matiere or not upload or not upload.filename:
            flash("Classe, matière et fichier requis.", "error")
            return redirect(url_for("send_page"))
        try:
            students = fetch_students(class_id, user["id"])
            df = lire_fichier(upload.read(), upload.filename)
            rows = preparer_apercu(df, students)
        except Exception as exc:
            flash(f"Fichier illisible : {exc}", "error")
            return redirect(url_for("send_page"))
        if not rows:
            flash("Aucune ligne trouvée dans le fichier.", "error")
            return redirect(url_for("send_page"))
        history = fetch_history(user["id"])
        warning = already_sent(history, class_id, matiere, eval_type)
        used = sent_today(user["id"])
        return render_template(
            "send_preview.html", classes=classes, selected=selected,
            matiere=matiere, eval_type=eval_type, rows=rows,
            rows_json=json.dumps(rows, ensure_ascii=False),
            options=[{"id": s["id"], "label": f"{s['nom']} {s['prenom']} <{s['email']}>"}
                     for s in students],
            used=used, remaining=max(DAILY_LIMIT - used, 0), warning=warning,
        )

    return render_template("send.html", classes=classes)


@app.route("/send/confirm", methods=["POST"])
@login_required
def send_confirm():
    user = current_user()
    try:
        rows = json.loads(request.form.get("rows", "[]"))
    except (ValueError, TypeError):
        rows = []
    class_id = request.form.get("class_id", "")
    matiere = request.form.get("matiere", "").strip()
    eval_type = request.form.get("eval_type", "Test")
    sender_name = request.form.get("sender_name", "").strip() or SENDER_NAME

    classes = fetch_classes(user["id"])
    target = next((c for c in classes if c["id"] == class_id), None)
    if not target or not matiere:
        abort(400)

    students = {s["id"]: s for s in fetch_students(class_id, user["id"])}
    valid = []
    for row in rows:
        override_id = request.form.get(f"student_{row.get('row_id')}", "")
        if override_id in students:
            row["student_id"] = override_id
        student = students.get(row.get("student_id"))
        note, note_ok = parse_note(row.get("Note"))
        if not student or not note_ok:
            continue
        valid.append({
            "email": student["email"],
            "prenom": student["prenom"],
            "note": fmt_note(note),
        })

    history = fetch_history(user["id"])
    warning = already_sent(history, class_id, matiere, eval_type)
    used = sent_today(user["id"])
    remaining = max(DAILY_LIMIT - used, 0)

    if not valid:
        flash("Aucune ligne valide à envoyer.", "error")
    elif len(valid) > remaining:
        flash("Limite quotidienne dépassée. Réduis le nombre d'envois.", "error")
    else:
        sent, failures = envoyer_notes(valid, matiere, eval_type, sender_name)
        if sent:
            api_insert("send_history", {
                "user_id": user["id"],
                "class_id": class_id,
                "class_name": target["name"],
                "matiere": matiere,
                "eval_type": eval_type,
                "sent_count": sent,
            })
        return render_template("send_result.html", sent=sent, failures=failures,
                               class_name=target["name"], matiere=matiere,
                               eval_type=eval_type, warning=warning)

    return redirect(url_for("send_page"))


@app.route("/history")
@login_required
def history_page():
    user = current_user()
    history = fetch_history(user["id"])
    return render_template("history.html", history=history)


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


if __name__ == "__main__":
    app.run(debug=True)
