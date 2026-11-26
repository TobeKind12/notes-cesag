# Notes de classe — Flask + Supabase + Vercel

Application web pour chefs de classe : créer une classe, importer une liste d'étudiants, charger des notes et envoyer **un e-mail séparé par étudiant**. Comptes Supabase Auth, données isolées par RLS, envoi via Resend (batch) ou Brevo.

> Streamlit n'est pas compatible avec Vercel (websockets/état serveur). L'application a donc été convertie en **Flask serverless** : même fonctionnalités, sans état serveur persistant.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | Application Flask (entrypoint Vercel) |
| `templates/` | Interfaces (connexion, classes, envoi, historique, confidentialité) |
| `public/style.css` | Style servi par Vercel |
| `vercel.json` | Rewrite vers `app.py` + durée max 60 s |
| `schema.sql` | Schéma Supabase + RLS + trigger profil |
| `requirements.txt` | Dépendances Python |
| `classe_exemple.xlsx`, `notes_exemple.xlsx` | Fichiers de test |

## 1. Supabase (déjà en place)

Le schéma `schema.sql` est exécuté. Dans **Authentication > URL Configuration**, après déploiement :

- **Site URL** : `https://TON-PROJECT.vercel.app`
- **Redirect URLs** : `https://TON-PROJECT.vercel.app/**`

Utiliser la clé **publishable** (`SUPABASE_KEY`), jamais la clé `service_role`.

## 2. E-mail

Choisir **un** service :

### Resend (recommandé, envoi groupé)
1. <https://resend.com> → créer un compte et vérifier un domaine.
2. Créer une API key → `RESEND_API_KEY`.
3. `SENDER_EMAIL` doit être une adresse autorisée par Resend.

### Brevo
1. <https://www.brevo.com> → **SMTP & API > API Keys**.
2. Clé → `BREVO_API_KEY`, `SENDER_EMAIL` vérifié.

## 3. Variables d'environnement (Vercel)

Dans **Vercel > Project > Settings > Environment Variables**, ajouter :

```text
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=sb_publishable_xxxxx
SITE_URL=https://TON-PROJECT.vercel.app
SECRET_KEY=<chaîne aléatoire longue>
RESEND_API_KEY=re_xxxxx
SENDER_EMAIL=notes@votre-domaine.fr
SENDER_NAME=Notes de classe
REPLY_TO=notes@votre-domaine.fr
```

`SECRET_KEY` est obligatoire en production (signature des sessions). Pour Brevo, remplacer `RESEND_API_KEY` par `BREVO_API_KEY`.

## 4. Déploiement Vercel

1. Le dépôt GitHub est connecté à Vercel : <https://github.com/TobeKind12/notes-cesag>
2. <https://vercel.com/new> → **Import Git Repository** → `TobeKind12/notes-cesag`
3. Framework : **Other** (Vercel détecte `app.py` via `vercel.json`)
4. Ajouter les variables d'environnement ci-dessus
5. **Deploy**

Vercel fournit ensuite une URL publique. La mise à jour du code sur `main` redéploie automatiquement.

## 5. Test local (optionnel)

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .streamlit\secrets.toml.example .env   # puis renseigner les valeurs
flask --app app run
```

Ouvrir <http://localhost:5000>.

## 6. Mode d'emploi

1. Créer un compte, confirmer l'e-mail, se connecter.
2. **Mes classes** : créer une classe avec `classe_exemple.xlsx` (colonnes `Nom, Prénom, Email`).
3. **Envoyer** : choisir la classe, la matière, le type, charger `notes_exemple.xlsx` (`Nom, Prénom, Note`).
4. Corriger les lignes rouges avec la liste déroulante, puis **Envoyer**.
5. Chaque étudiant reçoit uniquement sa note. **Historique** garde classe, matière, type, date et nombre d'e-mails — pas les notes.

## Sécurité

- CSRF sur tous les formulaires POST.
- Sessions signées par `SECRET_KEY`, tokens Supabase rafraîchis automatiquement.
- RLS Supabase : chaque chef de classe ne voit que ses données.
- Aucun secret dans le dépôt (`.env`, `.streamlit/secrets.toml` et `.vercel/` ignorés).
