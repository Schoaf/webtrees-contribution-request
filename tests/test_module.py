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


def red_blue_png(width=100, height=60):
    """Left half red, right half blue - which way round it ends up shows the turn."""
    import struct
    import zlib
    half = width // 2
    row = b"\x00" + (b"\xff\x00\x00" * half) + (b"\x00\x00\xff" * (width - half))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def submit_with_photo(session, token, fields, png):
    """The guest form as a browser sends it: multipart with the photo."""
    import urllib.request
    import uuid
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in {"_csrf": session.csrf, "token": token, **fields}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="photo.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(session.url(f"/module/{environment.MODULE_NAME}/RequestSubmit/tree"),
                                 data=b"".join(parts), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    return session.send(req)


def image_file(media_xref):
    """Size and the colour at the top middle of a media object's file in the tree's media folder."""
    import subprocess
    gedcom = environment.sql("SELECT m_gedcom FROM wt_media WHERE m_id = ?", media_xref)[0][0]
    name = re.search(r"\n1 FILE (.+)", gedcom).group(1).strip()
    path = os.path.join(environment.WT, "data", "media", name)
    out = subprocess.run([environment.PHP, "-r", f"$i = imagecreatefrompng('{path}'); $w = imagesx($i); "
                          "echo $w, ' ', imagesy($i), ' ', dechex(imagecolorat($i, intdiv($w, 2), 5));"],
                         capture_output=True, text=True, check=True).stdout.split()
    return int(out[0]), int(out[1]), out[2]


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

    def test_guest_sends_a_photo_ready_to_use(self):
        """Turned, framed and described by the guest: applied as it is - the person's photo, with its frame."""
        editor, token = create_request("I2")
        guest = environment.Session(E.base)
        guest.page(f"/module/{environment.MODULE_NAME}/Request/tree", token=token)
        submit_with_photo(guest, token, {
            "photo_turns": "1", "crop_top": "0", "crop_left": "0", "crop_height": "50", "crop_width": "60",
            "photo_title": "Hochzeit 1950", "photo_note": "Am Kirchplatz", "photo_type": "document",
        }, red_blue_png())

        entry = next(r for r in editor.get("RequestList").json["requests"] if r["xref"] == "I2")
        detail = editor.get("RequestDetail", id=entry["id"]).json
        self.assertEqual(({"top": 0, "left": 0, "height": 50, "width": 60}, "Hochzeit 1950", "Am Kirchplatz", "document"),
                         (detail["photoCrop"], detail["photoTitle"], detail["photoNote"], detail["photoType"]), detail)

        self.assertTrue(editor.post("RequestApply", {"id": entry["id"], "accept": {"photo": True}}).json["ok"])
        pending = environment.sql("SELECT new_gedcom FROM wt_change WHERE xref = 'I2' AND status = 'pending' ORDER BY change_id DESC")[0][0]
        link = re.search(r"\n1 OBJE @(\w+)@\n2 _PRIM Y\n2 CROP\n3 TOP 0\n3 LEFT 0\n3 HEIGHT 50\n3 WIDTH 60", pending)
        self.assertIsNotNone(link, pending)
        media = environment.sql("SELECT m_gedcom FROM wt_media WHERE m_id = ?", link.group(1))[0][0]
        self.assertIn("2 TITL Hochzeit 1950", media)
        self.assertIn("3 TYPE document", media)
        self.assertIn("1 NOTE Am Kirchplatz", media)
        # Turned clockwise by the guest: 60 x 100, the red left half now on top
        self.assertEqual((60, 100, "ff0000"), image_file(link.group(1)))

    def test_reviewer_turns_and_frames_before_applying(self):
        """The reviewer can still turn and frame it before applying."""
        editor, token = create_request("I3")
        guest = environment.Session(E.base)
        guest.page(f"/module/{environment.MODULE_NAME}/Request/tree", token=token)
        submit_with_photo(guest, token, {}, red_blue_png())
        entry = next(r for r in editor.get("RequestList").json["requests"] if r["xref"] == "I3")
        self.assertIsNone(editor.get("RequestDetail", id=entry["id"]).json["photoCrop"])

        r = editor.post("RequestApply", {"id": entry["id"], "accept": {"photo": True}, "photoTurns": 2,
                                         "photoCrop": {"top": 10, "left": 20, "height": 30, "width": 30}})
        self.assertTrue(r.json["ok"], r)
        pending = environment.sql("SELECT new_gedcom FROM wt_change WHERE xref = 'I3' AND status = 'pending' ORDER BY change_id DESC")[0][0]
        link = re.search(r"\n1 OBJE @(\w+)@(\n2 _PRIM Y)?\n2 CROP\n3 TOP 10\n3 LEFT 20\n3 HEIGHT 30\n3 WIDTH 30", pending)
        self.assertIsNotNone(link, pending)
        # Half a turn: still 100 x 60
        width, height, _ = image_file(link.group(1))
        self.assertEqual((100, 60), (width, height))


if __name__ == "__main__":
    unittest.main(verbosity=2)
