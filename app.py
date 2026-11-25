"""Application Streamlit + Supabase : envoi de notes aux étudiants.

Comptes Supabase Auth, données isolées par RLS, envoi via Resend ou Brevo.
Les secrets (clés API, URL Supabase) sont lus depuis st.secrets.
"""

from __future__ import annotations

import io
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st
from supabase import Client, create_client

DAILY_LIMIT = 200  # limite d'envois par jour et par compte


# =========================
# Utilitaires généraux
# =========================

def secret(key: str, default=""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


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


def lire_fichier(upload) -> pd.DataFrame:
    data = upload.getvalue()
    name = upload.name.lower()
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(data))
    try:
        text = data.decode("utf-8-sig")
        enc = "utf-8-sig"
    except UnicodeDecodeError:
        text = data.decode("latin-1")
        enc = "latin-1"
    sep = ";" if (text.splitlines()[0].count(";") > text.splitlines()[0].count(",") if text.splitlines() else False) else ","
    return pd.read_csv(io.BytesIO(data), encoding=enc, sep=sep, dtype=str)


# =========================
# Supabase
# =========================

def get_supabase() -> Client:
    """Client Supabase par session Streamlit (pas de cache global pour l'auth)."""
    if "sb_client" in st.session_state:
        return st.session_state["sb_client"]
    url = secret("SUPABASE_URL")
    key = secret("SUPABASE_KEY")
    if not url or not key:
        st.error("Secrets manquants : SUPABASE_URL et SUPABASE_KEY.")
        st.stop()
    sb = create_client(url, key)
    st.session_state["sb_client"] = sb
    return sb


def ensure_auth() -> Client:
    """Applique la session Supabase stockée pour que les requêtes RLS passent."""
    sb = get_supabase()
    sess = st.session_state.get("session")
    if sess:
        try:
            sb.auth.set_session(sess.access_token, sess.refresh_token)
        except Exception:
            pass
    return sb


def current_user():
    return st.session_state.get("user")


def require_auth():
    user = current_user()
    if not user:
        page_auth()
        st.stop()
    return user


def db_call(fn, error_msg="Erreur de base de données"):
    try:
        return fn()
    except Exception as exc:
        st.error(f"{error_msg} : {exc}")
        return None


# =========================
# Authentification
# =========================

def page_auth():
    st.title("Notes de classe")
    st.caption("Envoi sécurisé des notes aux étudiants.")
    tab_login, tab_signup, tab_reset = st.tabs(["Connexion", "Créer un compte", "Mot de passe oublié"])

    with tab_login:
        email = st.text_input("Adresse e-mail", key="login_email")
        pwd = st.text_input("Mot de passe", type="password", key="login_pwd")
        if st.button("Se connecter", type="primary"):
            try:
                res = ensure_auth().auth.sign_in_with_password({"email": email.strip(), "password": pwd})
                st.session_state["user"] = res.user
                st.session_state["session"] = res.session
                st.rerun()
            except Exception as exc:
                st.error(f"Connexion impossible : {exc}")

    with tab_signup:
        name = st.text_input("Votre nom (affiché aux étudiants)", key="su_name")
        email = st.text_input("Adresse e-mail", key="su_email")
        pwd = st.text_input("Mot de passe (8 caractères min.)", type="password", key="su_pwd")
        if st.button("Créer le compte"):
            if not name.strip() or not email.strip() or len(pwd) < 8:
                st.error("Nom, e-mail valide et mot de passe de 8 caractères minimum requis.")
            else:
                try:
                    ensure_auth().auth.sign_up({
                        "email": email.strip(),
                        "password": pwd,
                        "options": {"data": {"full_name": name.strip()}},
                    })
                    st.success("Compte créé. Vérifiez votre boîte mail pour confirmer votre adresse, puis connectez-vous.")
                except Exception as exc:
                    st.error(f"Inscription impossible : {exc}")

    with tab_reset:
        email = st.text_input("Adresse e-mail du compte", key="reset_email")
        if st.button("Envoyer le lien de réinitialisation"):
            if not email.strip():
                st.error("Indiquez votre adresse e-mail.")
            else:
                try:
                    redirect = secret("SITE_URL", "http://localhost:8501")
                    ensure_auth().auth.reset_password_for_email(email.strip(), {"redirect_to": redirect})
                    st.success("Si un compte existe, un e-mail de réinitialisation a été envoyé.")
                except Exception as exc:
                    st.error(f"Demande impossible : {exc}")


def logout():
    try:
        ensure_auth().auth.sign_out()
    except Exception:
        pass
    for k in ["user", "session", "preview_df", "preview_key", "sb_client"]:
        st.session_state.pop(k, None)
    st.rerun()


# =========================
# Données classes / étudiants
# =========================

def fetch_classes(user_id: str):
    res = ensure_auth().table("classes").select("*").eq("user_id", user_id).order("created_at").execute()
    return res.data or []


def fetch_students(class_id: str):
    res = ensure_auth().table("students").select("*").eq("class_id", class_id).order("nom").execute()
    return res.data or []


def insert_students(user_id: str, class_id: str, rows: list[dict]):
    payload = [{"user_id": user_id, "class_id": class_id, **r} for r in rows]
    if payload:
        ensure_auth().table("students").insert(payload).execute()


def validate_class_file(df: pd.DataFrame):
    cols = colonnes(df)
    missing = {"nom", "prenom", "email"} - cols.keys()
    if missing:
        return [], [f"Colonnes manquantes : {', '.join(sorted(missing))}"]
    errors, rows, seen = [], [], set()
    for i, row in df.iterrows():
        nom, prenom, email = txt(row[cols["nom"]]), txt(row[cols["prenom"]]), txt(row[cols["email"]]).lower()
        if not nom or not prenom or not email:
            errors.append(f"Ligne {i+2} : nom, prénom et e-mail requis.")
            continue
        if not email_valide(email):
            errors.append(f"Ligne {i+2} ({nom} {prenom}) : e-mail invalide « {email} ».")
            continue
        key = email
        if key in seen:
            errors.append(f"Ligne {i+2} : doublon d'e-mail « {email} ».")
            continue
        seen.add(key)
        rows.append({"nom": nom, "prenom": prenom, "email": email})
    return rows, errors


# =========================
# Matching des notes
# =========================

def build_index(students: list[dict]):
    index = {}
    for s in students:
        for key in [(clean(s["nom"]), clean(s["prenom"])), (clean(s["prenom"]), clean(s["nom"]))]:
            index.setdefault(key, [])
            if s["id"] not in {x["id"] for x in index[key]}:
                index[key].append(s)
    return index


def preparer_apercu(notes_df: pd.DataFrame, students: list[dict]):
    cols = colonnes(notes_df)
    missing = {"nom", "prenom", "note"} - cols.keys()
    if missing:
        raise ValueError("Colonnes manquantes : " + ", ".join(sorted(missing)))
    index = build_index(students)
    rows = []
    for i, row in notes_df.iterrows():
        nom, prenom = txt(row[cols["nom"]]), txt(row[cols["prenom"]])
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
        chosen = next((m for m in matches if m["id"] == match_id), matches[0] if len(matches) == 1 else None)
        rows.append({
            "row_id": i,
            "Nom": nom,
            "Prénom": chosen["prenom"] if chosen else prenom,
            "E-mail trouvé": chosen["email"] if chosen else "",
            "Note": fmt_note(note) if note_ok else txt(row[cols["note"]]),
            "Vérification": status,
            "student_id": match_id,
        })
    return pd.DataFrame(rows)


def appliquer_corrections(preview: pd.DataFrame, students: list[dict]) -> pd.DataFrame:
    by_id = {s["id"]: s for s in students}
    options = {"": "— choisir un étudiant —"}
    options.update({s["id"]: f"{s['nom']} {s['prenom']} <{s['email']}>" for s in students})
    corrected = preview.copy()
    for idx, row in corrected.iterrows():
        if row["Vérification"] == "OK":
            continue
        key = f"fix_{st.session_state.get('preview_key','')}_{row['row_id']}"
        choice = st.selectbox(
            f"Correction pour {row['Nom']} {row['Prénom']} ({row['Note']}) — {row['Vérification']}",
            options=list(options.keys()),
            format_func=lambda x: options[x],
            key=key,
        )
        if choice and choice in by_id:
            s = by_id[choice]
            corrected.at[idx, "student_id"] = s["id"]
            corrected.at[idx, "E-mail trouvé"] = s["email"]
            corrected.at[idx, "Prénom"] = s["prenom"]
            note, note_ok = parse_note(row["Note"])
            corrected.at[idx, "Vérification"] = "OK" if note_ok else "Note invalide (vide ou hors 0-20)"
    return corrected


# =========================
# Envoi e-mail
# =========================

def build_message(prenom, matiere, eval_type, note, sender_name):
    return (
        f"Bonjour {prenom}, ta note de {matiere} ({eval_type}) est : {note}/20. "
        f"Envoyé par {sender_name}."
    )


def send_email(to_email: str, subject: str, body: str):
    sender_email = secret("SENDER_EMAIL")
    sender_name = secret("SENDER_NAME", "Notes de classe")
    reply_to = secret("REPLY_TO", sender_email)
    resend_key = secret("RESEND_API_KEY")
    brevo_key = secret("BREVO_API_KEY")

    if resend_key:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            json={
                "from": f"{sender_name} <{sender_email}>",
                "to": [to_email],
                "subject": subject,
                "text": body,
                "reply_to": reply_to,
            },
            timeout=30,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Resend {r.status_code}: {r.text[:200]}")
        return
    if brevo_key:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": brevo_key, "Content-Type": "application/json", "accept": "application/json"},
            json={
                "sender": {"name": sender_name, "email": sender_email},
                "to": [{"email": to_email}],
                "subject": subject,
                "textContent": body,
                "replyTo": {"email": reply_to, "name": sender_name},
            },
            timeout=30,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Brevo {r.status_code}: {r.text[:200]}")
        return
    raise RuntimeError("Aucun service d'e-mail configuré (RESEND_API_KEY ou BREVO_API_KEY).")


def envoyer_notes(rows: list[dict], matiere: str, eval_type: str, sender_name: str, on_progress=None):
    valid = [r for r in rows if r["Vérification"] == "OK" and r.get("student_id") and r.get("E-mail trouvé")]
    sent, failures = 0, []
    for i, row in enumerate(valid, 1):
        body = build_message(row["Prénom"], matiere, eval_type, row["Note"], sender_name)
        subject = f"Note {matiere} ({eval_type})"
        try:
            send_email(row["E-mail trouvé"], subject, body)
            sent += 1
        except Exception as exc:
            failures.append(f"{row['Nom']} {row['Prénom']} : {exc}")
        if on_progress:
            on_progress(i, len(valid))
    return sent, failures


# =========================
# Historique / limites
# =========================

def fetch_history(user_id: str):
    res = ensure_auth().table("send_history").select("*").eq("user_id", user_id).order("sent_at", desc=True).limit(200).execute()
    return res.data or []


def sent_today(user_id: str) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    res = ensure_auth().table("send_history").select("sent_count").eq("user_id", user_id).gte("sent_at", start).execute()
    return sum(int(r.get("sent_count", 0)) for r in (res.data or []))


def already_sent(history: list[dict], class_id: str, matiere: str, eval_type: str) -> bool:
    return any(
        str(h.get("class_id")) == str(class_id)
        and clean(h.get("matiere")) == clean(matiere)
        and clean(h.get("eval_type")) == clean(eval_type)
        for h in history
    )


# =========================
# Pages
# =========================

def page_classes(user):
    st.subheader("Étape 1 — Créer / gérer sa classe")
    classes = fetch_classes(user.id)
    with st.expander("Créer une nouvelle classe", expanded=not classes):
        name = st.text_input("Nom de la classe", placeholder="MSMD1", key="new_class_name")
        upload = st.file_uploader("Liste de classe (Excel/CSV : Nom, Prénom, Email)", type=["xlsx", "xls", "csv"], key="new_class_file")
        if st.button("Créer la classe", type="primary", disabled=not (name.strip() and upload)):
            df = lire_fichier(upload)
            rows, errors = validate_class_file(df)
            if errors:
                for e in errors:
                    st.error(e)
            elif not rows:
                st.error("Aucun étudiant valide trouvé.")
            else:
                res = ensure_auth().table("classes").insert({"user_id": user.id, "name": name.strip()}).execute()
                class_id = res.data[0]["id"]
                insert_students(user.id, class_id, rows)
                st.success(f"Classe « {name.strip()} » créée avec {len(rows)} étudiant(s).")
                st.rerun()

    if not classes:
        st.info("Créez d'abord une classe.")
        return

    class_map = {f"{c['name']} ({c['id'][:8]})": c for c in classes}
    label = st.selectbox("Classe à gérer", list(class_map.keys()))
    cls = class_map[label]
    students = fetch_students(cls["id"])
    st.write(f"**{len(students)} étudiant(s)** dans « {cls['name']} ».")
    if students:
        st.dataframe(pd.DataFrame(students)[["nom", "prenom", "email"]], hide_index=True, use_container_width=True)

    with st.expander("Ajouter un étudiant"):
        c1, c2, c3 = st.columns(3)
        nom = c1.text_input("Nom", key="add_nom")
        prenom = c2.text_input("Prénom", key="add_prenom")
        email = c3.text_input("Email", key="add_email")
        if st.button("Ajouter"):
            if not (nom.strip() and prenom.strip() and email_valide(email.strip())):
                st.error("Nom, prénom et e-mail valide requis.")
            else:
                insert_students(user.id, cls["id"], [{"nom": nom.strip(), "prenom": prenom.strip(), "email": email.strip().lower()}])
                st.success("Étudiant ajouté.")
                st.rerun()

    with st.expander("Modifier / supprimer un étudiant"):
        if students:
            s_map = {f"{s['nom']} {s['prenom']} <{s['email']}>": s for s in students}
            s_label = st.selectbox("Étudiant", list(s_map.keys()), key="edit_select")
            s = s_map[s_label]
            c1, c2, c3 = st.columns(3)
            n_nom = c1.text_input("Nom", value=s["nom"], key="edit_nom")
            n_prenom = c2.text_input("Prénom", value=s["prenom"], key="edit_prenom")
            n_email = c3.text_input("Email", value=s["email"], key="edit_email")
            b1, b2 = st.columns(2)
            if b1.button("Enregistrer la modification"):
                if not (n_nom.strip() and n_prenom.strip() and email_valide(n_email.strip())):
                    st.error("Nom, prénom et e-mail valide requis.")
                else:
                    ensure_auth().table("students").update({"nom": n_nom.strip(), "prenom": n_prenom.strip(), "email": n_email.strip().lower()}).eq("id", s["id"]).eq("user_id", user.id).execute()
                    st.success("Étudiant modifié.")
                    st.rerun()
            if b2.button("Supprimer cet étudiant"):
                ensure_auth().table("students").delete().eq("id", s["id"]).eq("user_id", user.id).execute()
                st.success("Étudiant supprimé.")
                st.rerun()

    with st.expander("Recharger toute la liste (remplace la liste actuelle)"):
        upload = st.file_uploader("Nouveau fichier classe", type=["xlsx", "xls", "csv"], key="reload_class_file")
        if st.button("Remplacer la liste", disabled=not upload):
            df = lire_fichier(upload)
            rows, errors = validate_class_file(df)
            if errors:
                for e in errors:
                    st.error(e)
            else:
                ensure_auth().table("students").delete().eq("class_id", cls["id"]).eq("user_id", user.id).execute()
                insert_students(user.id, cls["id"], rows)
                st.success(f"Liste remplacée : {len(rows)} étudiant(s).")
                st.rerun()

    with st.expander("Supprimer la classe et toutes ses données"):
        st.warning("Cette action supprime la classe, ses étudiants et son historique d'envois.")
        confirm = st.text_input(f"Tapez le nom de la classe ({cls['name']}) pour confirmer", key="del_class_confirm")
        if st.button("Supprimer définitivement", disabled=confirm != cls["name"]):
            ensure_auth().table("classes").delete().eq("id", cls["id"]).eq("user_id", user.id).execute()
            st.success("Classe supprimée.")
            st.rerun()


def page_send(user):
    st.subheader("Étape 2 — Envoyer des notes")
    classes = fetch_classes(user.id)
    if not classes:
        st.info("Créez d'abord une classe dans l'étape 1.")
        return

    class_map = {c["name"]: c for c in classes}
    c1, c2, c3 = st.columns(3)
    cls_name = c1.selectbox("Classe", list(class_map.keys()))
    cls = class_map[cls_name]
    matiere = c2.text_input("Matière", placeholder="Mathématiques")
    eval_type = c3.selectbox("Type", ["Test", "Partiel"])

    history = fetch_history(user.id)
    if matiere.strip() and already_sent(history, cls["id"], matiere, eval_type):
        st.warning(f"⚠️ {cls_name} / {matiere} / {eval_type} a déjà été envoyé.")

    upload = st.file_uploader("Fichier de notes (Nom, Prénom, Note)", type=["xlsx", "xls", "csv"], key="notes_file")
    if not upload or not matiere.strip():
        st.info("Choisissez une classe, indiquez la matière et chargez le fichier de notes.")
        return

    students = fetch_students(cls["id"])
    if not students:
        st.error("Cette classe ne contient aucun étudiant.")
        return

    preview_key = f"{cls['id']}|{matiere}|{eval_type}|{upload.name}|{len(upload.getvalue())}"
    if st.session_state.get("preview_key") != preview_key:
        try:
            st.session_state["preview_df"] = preparer_apercu(lire_fichier(upload), students)
            st.session_state["preview_key"] = preview_key
        except Exception as exc:
            st.error(f"Fichier illisible : {exc}")
            return

    preview = st.session_state["preview_df"]
    st.write("**Aperçu** — corrigez les lignes rouges avec la liste déroulante, puis envoyez.")
    corrected = appliquer_corrections(preview, students)

    def style_row(row):
        ok = row["Vérification"] == "OK"
        return ["" if ok else "color:#b00020;font-weight:bold"] * len(row)

    show = corrected[["Nom", "Prénom", "E-mail trouvé", "Note", "Vérification"]]
    st.dataframe(show.style.apply(style_row, axis=1), hide_index=True, use_container_width=True)
    valid_rows = corrected[corrected["Vérification"] == "OK"].to_dict("records")
    st.caption(f"{len(valid_rows)} ligne(s) prête(s) sur {len(corrected)}. Les lignes rouges ne seront pas envoyées.")

    used = sent_today(user.id)
    remaining = max(DAILY_LIMIT - used, 0)
    st.caption(f"Limite quotidienne : {used}/{DAILY_LIMIT} e-mails envoyés aujourd'hui ({remaining} restant(s)).")
    if len(valid_rows) > remaining:
        st.error("La limite quotidienne serait dépassée. Réduisez le nombre d'envois ou réessayez demain.")

    sender_name = st.text_input("Nom affiché aux étudiants", value=secret("SENDER_NAME", user.email or "Chef de classe"))
    disabled = not valid_rows or len(valid_rows) > remaining or not sender_name.strip()
    if st.button("Envoyer", type="primary", disabled=disabled):
        progress = st.progress(0)
        sent, failures = envoyer_notes(
            valid_rows, matiere.strip(), eval_type, sender_name.strip(),
            on_progress=lambda i, total: progress.progress(i / total if total else 1),
        )
        if sent:
            ensure_auth().table("send_history").insert({
                "user_id": user.id,
                "class_id": cls["id"],
                "class_name": cls_name,
                "matiere": matiere.strip(),
                "eval_type": eval_type,
                "sent_count": sent,
            }).execute()
        st.success(f"Récapitulatif : {sent} envoyé(s), {len(failures)} échec(s).")
        for f in failures:
            st.error(f)
        st.session_state.pop("preview_df", None)
        st.session_state.pop("preview_key", None)


def page_history(user):
    st.subheader("Historique des envois")
    history = fetch_history(user.id)
    if not history:
        st.info("Aucun envoi pour le moment.")
        return
    df = pd.DataFrame(history)[["sent_at", "class_name", "matiere", "eval_type", "sent_count"]]
    df.columns = ["Date", "Classe", "Matière", "Type", "Envoyés"]
    st.dataframe(df, hide_index=True, use_container_width=True)
    st.caption("Les notes ne sont pas conservées après l'envoi.")


def page_privacy():
    st.subheader("Politique de confidentialité")
    st.markdown(
        """
**Responsable.** Le chef de classe qui utilise l'application est responsable des données qu'il importe.  
**Données stockées.** Compte (e-mail, nom), classes, listes d'étudiants (nom, prénom, e-mail) et historique des envois (classe, matière, type, date, nombre d'e-mails envoyés).  
**Notes.** Les notes ne sont **pas conservées** après l'envoi. Elles sont utilisées uniquement pour composer l'e-mail.  
**Accès.** Chaque chef de classe ne voit que ses propres données (contrôle d'accès Supabase RLS).  
**Sous-traitants.** Supabase (authentification/base de données) et le service d'e-mail configuré (Brevo ou Resend) traitent les données nécessaires à leur fonction.  
**Suppression.** Vous pouvez supprimer une classe et toutes ses données depuis l'étape 1. Pour supprimer votre compte, contactez l'administrateur de l'instance.  
**Contact.** Pour toute question, contactez l'administrateur de l'application.
        """.strip()
    )


# =========================
# Main
# =========================

def main():
    st.set_page_config(page_title="Notes de classe", page_icon="✉️", layout="wide")
    user = current_user()
    if not user:
        page_auth()
        return

    with st.sidebar:
        st.write(f"Connecté : **{user.email}**")
        page = st.radio("Navigation", ["Envoyer des notes", "Mes classes", "Historique", "Confidentialité"])
        if st.button("Se déconnecter"):
            logout()

    if page == "Mes classes":
        page_classes(user)
    elif page == "Envoyer des notes":
        page_send(user)
    elif page == "Historique":
        page_history(user)
    else:
        page_privacy()


if __name__ == "__main__":
    main()
