# Notes de classe — Streamlit + Supabase

Application web pour chefs de classe : créer une classe, importer une liste d'étudiants, charger des notes et envoyer **un e-mail séparé par étudiant**. Comptes Supabase Auth, données isolées par RLS, envoi via Brevo ou Resend.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | Application Streamlit (comptes, classes, envoi, confidentialité) |
| `schema.sql` | Schéma Supabase + Row Level Security + trigger profil |
| `requirements.txt` | Dépendances Python |
| `classe_exemple.xlsx` | Exemple de liste de classe (3 étudiants fictifs) |
| `notes_exemple.xlsx` | Exemple de notes (dont un nom mal orthographié pour tester la correction) |
| `README.md` | Ce document |

## 1. Créer le projet Supabase

1. Créez un projet sur <https://supabase.com> (offre gratuite suffisante).
2. Dans **SQL Editor**, collez le contenu de `schema.sql` et exécutez-le. Cela crée les tables (`profiles`, `classes`, `students`, `send_history`), active la RLS et ajoute le trigger de création de profil.
3. Dans **Authentication > Providers > Email**, laissez l'e-mail activé. Gardez la confirmation d'e-mail activée (recommandé).
4. Dans **Authentication > URL Configuration**, ajoutez l'URL de votre app (local : `http://localhost:8501`, puis l'URL Streamlit Cloud) dans **Site URL** et **Redirect URLs**.
5. Dans **Project Settings > API**, récupérez :
   - `Project URL` → `SUPABASE_URL`
   - clé `publishable` / `anon public` → `SUPABASE_KEY`

> Ne mettez jamais la clé `secret` / `service_role` dans Streamlit : l'app utilise uniquement la clé publishable + RLS.

## 2. Configurer le service d'e-mail

Choisissez **un** service (clé API dans les secrets, jamais dans le code).

### Option A — Resend (simple)
1. Créez un compte sur <https://resend.com>.
2. Vérifiez un domaine ou utilisez l'adresse de test pour vos essais.
3. Créez une API key → `RESEND_API_KEY`.
4. Renseignez `SENDER_EMAIL` avec une adresse autorisée par Resend.

### Option B — Brevo
1. Créez un compte sur <https://www.brevo.com>.
2. Dans **SMTP & API > API Keys**, créez une clé → `BREVO_API_KEY`.
3. Vérifiez l'expéditeur/domaine et utilisez cette adresse dans `SENDER_EMAIL`.

## 3. Lancer en local

```bash
cd notes-classe-supabase
python -m venv .venv
# Windows : .venv\Scripts\activate
# macOS/Linux : source .venv/bin/activate
pip install -r requirements.txt
```

Créez `.streamlit/secrets.toml` (ne pas commiter) :

```toml
SUPABASE_URL = "https://xxxxx.supabase.co"
SUPABASE_KEY = "votre-cle-publishable-anon"
SITE_URL = "http://localhost:8501"

# E-mail : renseigner Resend OU Brevo
RESEND_API_KEY = "re_..."
# BREVO_API_KEY = "xkeysib-..."
SENDER_EMAIL = "notes@votre-domaine.fr"
SENDER_NAME = "Notes de classe"
REPLY_TO = "notes@votre-domaine.fr"
```

Puis :

```bash
streamlit run app.py
```

## 4. Déployer gratuitement sur Streamlit Community Cloud

1. Poussez les fichiers sur GitHub **sans** `.streamlit/secrets.toml` (ajoutez `.streamlit/` au `.gitignore`).
2. Allez sur <https://share.streamlit.io> > **New app**, sélectionnez le dépôt, la branche et `app.py`.
3. Dans **Advanced settings > Secrets**, collez les mêmes secrets, en remplaçant `SITE_URL` par l'URL publique de l'app.
4. Ajoutez cette URL publique dans Supabase **Authentication > URL Configuration**.
5. Déployez : Streamlit fournit un lien à partager aux chefs de classe.

## Mode d'emploi chef de classe (une demi-page)

1. **Compte.** Ouvrez le lien, créez un compte (e-mail + mot de passe), confirmez l'e-mail reçu, puis connectez-vous. En cas d'oubli, utilisez « Mot de passe oublié ».
2. **Créer sa classe (une seule fois).** Dans « Mes classes », nommez la classe (ex. `MSMD1`) et chargez le fichier Excel/CSV avec les colonnes `Nom, Prénom, Email`. L'app vérifie les e-mails invalides et doublons. Vous pouvez ensuite ajouter, corriger, supprimer un étudiant ou recharger la liste.
3. **Envoyer des notes.** Dans « Envoyer des notes », choisissez la classe, la matière et le type (Test/Partiel), puis chargez un fichier avec `Nom, Prénom, Note` uniquement.
4. **Vérifier l'aperçu.** Chaque ligne affiche nom, e-mail trouvé et note. Les lignes rouges (introuvable, plusieurs correspondances, note vide/hors 0-20) ne sont pas envoyées ; pour un nom mal orthographié, choisissez le bon étudiant dans la liste déroulante.
5. **Envoyer.** Cliquez sur « Envoyer ». Chaque étudiant reçoit uniquement sa propre note : « Bonjour [Prénom], ta note de [Matière] ([Type]) est : [Note]/20. Envoyé par [Nom du chef de classe]. » Une barre de progression puis un récapitulatif envoyés/échecs s'affichent.
6. **Confidentialité.** Les notes ne sont pas conservées après envoi. L'historique garde seulement classe, matière, type, date et nombre d'e-mails. Vous pouvez supprimer une classe et toutes ses données en un clic depuis « Mes classes ».

## Test avant envoi réel

Utilisez `classe_exemple.xlsx` puis `notes_exemple.xlsx` (3 étudiants fictifs, dont un nom mal orthographié) avec un service d'e-mail en mode test/sandbox, ou vérifiez l'aperçu et la correction manuelle avant d'utiliser une vraie liste. La limite quotidienne par compte protège contre les envois accidentels.
