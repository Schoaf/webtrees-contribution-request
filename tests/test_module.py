"""Tests for webtrees-contribution-request against a real webtrees (see environment.py).

Run: python3 tests/test_module.py
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import environment  # noqa: E402

E = None


def setUpModule():
    global E
    E = environment.Environment().start()


def tearDownModule():
    if E:
        E.stop()


def create_request(xref="I1"):
    editor = E.session("editor")
    r = editor.post("CreateRequest", {"xref": xref})
    assert r.json and r.json.get("ok"), r
    return editor, re.search(r"token=([A-Za-z0-9]+)", r.json["url"]).group(1)


class Module(unittest.TestCase):
    def test_info(self):
        version = re.search(r"function customModuleVersion\(\): string\s*\{\s*return '([^']+)'",
                            open(os.path.join(environment.MODULE, "WebtreesShareModule.php")).read()).group(1)
        r = environment.Session(E.base).get("Info")
        self.assertEqual({"active": True, "version": version}, r.json)
        self.assertEqual(version, open(os.path.join(environment.MODULE, "latest-version.txt")).read().strip())

    def test_only_editors_create_requests(self):
        guest = environment.Session(E.base)
        guest.page("/tree/tree")
        self.assertNotEqual(True, (guest.post("CreateRequest", {"xref": "I1"}).json or {}).get("ok"))
        member = E.session("member")
        self.assertEqual("not-editable", member.post("CreateRequest", {"xref": "I1"}).json["error"])
        editor = E.session("editor")
        self.assertEqual("not-found", editor.post("CreateRequest", {"xref": "I999"}).json["error"])
        self.assertTrue(editor.post("CreateRequest", {"xref": "I1"}).json["ok"])

    def test_email_follows_requested_language(self):
        editor, token = create_request()
        de = editor.get("RequestEmailTemplate", token=token, lang="de").json
        en = editor.get("RequestEmailTemplate", token=token, lang="en").json
        self.assertIn("Stammbaum", de["subject"])
        self.assertIn("family tree", en["subject"])
        self.assertTrue(en["body"].startswith("Hello"))
        self.assertIn("{{PERSONAL_MESSAGE}}", en["body"])

    def test_guest_answers_and_editor_applies(self):
        editor, token = create_request()
        guest = environment.Session(E.base)
        page = guest.page(f"/module/{environment.MODULE_NAME}/Request/tree", token=token)
        self.assertEqual(200, page.status)
        self.assertIn("Anna", page.text)

        guest.form(f"/module/{environment.MODULE_NAME}/RequestSubmit/tree",
                   {"token": token, "BIRT_PLAC": "Graz", "note": "Sie war Lehrerin", "responder_name": "Tante Erna"})
        again = guest.page(f"/module/{environment.MODULE_NAME}/Request/tree", token=token)
        self.assertNotIn('name="BIRT_PLAC"', again.text, "an answered request shows no form any more")

        self.assertGreaterEqual(editor.get("RequestNotifications").json["unread"], 1)
        entry = next(r for r in editor.get("RequestList").json["requests"] if r["xref"] == "I1" and r["responder"])
        detail = editor.get("RequestDetail", id=entry["id"]).json
        self.assertEqual((entry["id"], False), (detail["id"], detail["applied"]), detail)

        r = editor.post("RequestApply", {"id": entry["id"], "accept": {"BIRT_PLAC": True}})
        self.assertTrue(r.json["ok"], r)
        # The editor has no auto-accept: the change waits for approval, as any other edit.
        pending = environment.sql("SELECT new_gedcom FROM wt_change WHERE xref = 'I1' AND status = 'pending'")
        self.assertTrue(any("2 PLAC Graz" in row[0] for row in pending), pending)

    def test_expired_link_is_refused(self):
        _, token = create_request()
        environment.sql("UPDATE wt_webtreesshare_request SET expires_at = '2000-01-01 00:00:00' WHERE token = ?", token)
        page = environment.Session(E.base).page(f"/module/{environment.MODULE_NAME}/Request/tree", token=token)
        self.assertNotIn('name="BIRT_PLAC"', page.text)

    def test_guest_page_in_english(self):
        _, token = create_request()
        guest = environment.Session(E.base)
        environment.sql("UPDATE wt_site_setting SET setting_value = 'en-US' WHERE setting_name = 'LANGUAGE'")
        try:
            page = guest.page(f"/module/{environment.MODULE_NAME}/Request/tree", token=token)
            self.assertIn("First name", page.text)
        finally:
            environment.sql("UPDATE wt_site_setting SET setting_value = 'de' WHERE setting_name = 'LANGUAGE'")

    def test_requester_can_delete(self):
        editor, token = create_request()
        row = environment.sql("SELECT id FROM wt_webtreesshare_request WHERE token = ?", token)[0][0]
        E.session("member").post("RequestDelete", {"id": row})
        self.assertEqual(1, len(environment.sql("SELECT id FROM wt_webtreesshare_request WHERE id = ?", row)),
                         "only the requester may delete")
        editor.post("RequestDelete", {"id": row})
        self.assertEqual([], environment.sql("SELECT id FROM wt_webtreesshare_request WHERE id = ?", row))


if __name__ == "__main__":
    unittest.main(verbosity=2)
