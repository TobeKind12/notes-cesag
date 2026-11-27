"""Test local : aperçu -> confirmation, sans Supabase ni e-mail réels.

Lancer depuis la racine du projet :
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import html as html_lib
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Valeurs factices obligatoirement avant l'import de app.py (aucun secret réel).
os.environ["SUPABASE_URL"] = "http://localhost"
os.environ["SUPABASE_KEY"] = "cle-de-test"
os.environ["SITE_URL"] = "http://localhost:5000"
os.environ["SECRET_KEY"] = "secret-de-test-local-uniquement"
os.environ["RESEND_API_KEY"] = ""
os.environ["BREVO_API_KEY"] = ""
os.environ["SENDER_EMAIL"] = "notes@example.test"
os.environ["SENDER_NAME"] = "Notes-Cesag"

import app as flask_app  # noqa: E402

CLASSE_XLSX = ROOT / "tests" / "classe_test.xlsx"
NOTES_XLSX = ROOT / "tests" / "notes_test.xlsx"
CLASSES = [{"id": "class-1", "name": "Classe test"}]
ROWS_RE = re.compile(r'<textarea name="rows"[^>]*>(.*?)</textarea>', re.S)


class SendFlowTest(unittest.TestCase):
    def setUp(self):
        # On garde la vraie fonction pour tester l'isolation des refus Resend.
        self.real_envoyer_notes = flask_app.envoyer_notes

        classe_df = flask_app.lire_fichier(CLASSE_XLSX.read_bytes(), CLASSE_XLSX.name)
        classe_rows, errors = flask_app.validate_class_file(classe_df)
        self.assertEqual(errors, [])
        self.students = [{"id": f"s-{i}", **row} for i, row in enumerate(classe_rows, 1)]

        self._patch("fetch_classes", return_value=CLASSES)
        self._patch("fetch_students", return_value=self.students)
        self._patch("fetch_history", return_value=[])
        self._patch("sent_today", return_value=0)
        self.insert_mock = self._patch("api_insert", return_value=[{"id": "h-1"}])
        self.envoyer_mock = self._patch("envoyer_notes", return_value=(0, []))

        self.client = flask_app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = {"id": "teacher-1"}
            sess["access_token"] = "jeton-de-test"
        self.client.get("/send")  # initialise le jeton CSRF en session

    def _patch(self, name, **kwargs):
        patcher = patch.object(flask_app, name, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _csrf(self):
        with self.client.session_transaction() as sess:
            return sess["csrf_token"]

    def _preview(self):
        response = self.client.post(
            "/send",
            data={
                "csrf_token": self._csrf(),
                "class_id": "class-1",
                "matiere": "Maths",
                "eval_type": "Test",
                "file": (io.BytesIO(NOTES_XLSX.read_bytes()), NOTES_XLSX.name),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        match = ROWS_RE.search(response.get_data(as_text=True))
        self.assertIsNotNone(match, "textarea name=rows absent de l'aperçu")
        rows = json.loads(html_lib.unescape(match.group(1)))
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        return rows

    def _confirm(self, rows, extra=None, raw_rows=None):
        payload = {
            "csrf_token": self._csrf(),
            "class_id": "class-1",
            "matiere": "Maths",
            "eval_type": "Test",
            "sender_name": "Notes-Cesag",
            "rows": raw_rows if raw_rows is not None else json.dumps(rows, ensure_ascii=False),
        }
        payload.update(extra or {})
        return self.client.post("/send/confirm", data=payload)

    def test_apercu_puis_confirmation(self):
        rows = self._preview()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [row["Vérification"] for row in rows],
            ["OK", "Introuvable dans la classe", "OK"],
        )

        bob_id = next(s["id"] for s in self.students if s["prenom"] == "Bob")
        bob_row_id = next(row["row_id"] for row in rows if row["Prénom"] == "Bobo")
        self.envoyer_mock.return_value = (3, [])

        response = self._confirm(rows, {f"student_{bob_row_id}": bob_id})
        self.assertEqual(response.status_code, 200)
        self.assertIn("<strong>3</strong> e-mail(s)", response.get_data(as_text=True))

        sent_rows = self.envoyer_mock.call_args.args[0]
        self.assertEqual(
            [row["email"] for row in sent_rows],
            ["alice.martin@example.test", "bob.durand@example.test", "carole.petit@example.test"],
        )
        self.assertEqual(sent_rows[2]["note"], "17,5")
        self.assertEqual(self.insert_mock.call_args.args[1]["sent_count"], 3)

    def test_ancien_apercu_doublement_encode(self):
        rows = self._preview()
        legacy = json.dumps(json.dumps(rows, ensure_ascii=False))
        self.envoyer_mock.return_value = (2, [])

        response = self._confirm([], raw_rows=legacy)
        self.assertEqual(response.status_code, 200)
        # La ligne « Bobo » n'est pas corrigée : elle est ignorée, les deux autres partent.
        self.assertEqual(len(self.envoyer_mock.call_args.args[0]), 2)

    def test_lignes_invalides_ne_provoquent_pas_un_500(self):
        for raw in ["pas du json", json.dumps(["a", "b"]), json.dumps(42)]:
            response = self._confirm([], raw_rows=raw)
            self.assertEqual(response.status_code, 302, raw)

    @staticmethod
    def _send_one_fake(msg):
        if msg["to"] == "onboarding@resend.dev":
            raise RuntimeError("Resend 403 : domaine de test limité au propriétaire")
        return None

    def test_refus_resend_individuel_continue(self):
        rows = [
            {"email": "ok@example.test", "prenom": "Alice", "note": "14"},
            {"email": "onboarding@resend.dev", "prenom": "Bob", "note": "11"},
        ]
        with patch.object(flask_app, "RESEND_API_KEY", "cle-factice"), \
             patch.object(flask_app, "send_batch_resend", side_effect=RuntimeError("lot refusé")), \
             patch.object(flask_app, "send_one_resend", side_effect=self._send_one_fake):
            sent, failures = self.real_envoyer_notes(rows, "Maths", "Test", "Notes-Cesag")

        self.assertEqual(sent, 1)
        self.assertEqual(
            failures,
            ["onboarding@resend.dev : Échec : Resend 403 : domaine de test limité au propriétaire"],
        )

    def test_erreur_inattendue_d_envoi_affiche_echec(self):
        rows = self._preview()
        self.envoyer_mock.side_effect = RuntimeError("service indisponible")

        response = self._confirm(rows)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Échec : service indisponible", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
