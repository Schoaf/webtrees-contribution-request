"""Test environment: a fresh webtrees with SQLite under `php -S` on 127.0.0.1, with this module linked into
modules_v4/.

Environment variables:
  PHP           PHP binary (default: php)
  WEBTREES_ZIP  an existing webtrees ZIP instead of downloading one
  WEBTREES      version to download (default 2.2.6, checksum see CHECKSUMS)
"""
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE = os.path.dirname(HERE)
WORK = os.path.join(HERE, ".work")
WT = os.path.join(WORK, "webtrees")
PHP = os.environ.get("PHP", "php")
MODULE_NAME = "_webtrees-contribution-request_"

CHECKSUMS = {
    "2.2.6": "c6b82f0269733b71de1a160a93eb32029e3467674e9bbfe3956d46a8bb04f925",
}

PASSWORD = "test-password-1"
# webtrees roles per account (canedit none/access/edit/accept/admin); "admin" comes from the setup wizard.
ACCOUNTS = {"member": "access", "editor": "edit"}

# php -S knows no .htaccess - data/ must be blocked.
ROUTER = r"""<?php
$path = rawurldecode((string) parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH));
foreach (explode('/', str_replace('\\', '/', $path)) as $part) {
    if (strtolower(rtrim($part, ' .')) === 'data' || ($part !== '' && $part[0] === '.')) { http_response_code(403); return true; }
}
return false;
"""


def _zip():
    own = os.environ.get("WEBTREES_ZIP")
    if own:
        return own
    version = os.environ.get("WEBTREES", "2.2.6")
    target = os.path.join(HERE, ".cache", f"webtrees-{version}.zip")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if not os.path.isfile(target):
        url = f"https://github.com/fisharebest/webtrees/releases/download/{version}/webtrees-{version}.zip"
        urllib.request.urlretrieve(url, target + ".tmp")
        os.replace(target + ".tmp", target)
    expected = CHECKSUMS.get(version)
    if expected and hashlib.sha256(open(target, "rb").read()).hexdigest() != expected:
        os.remove(target)
        raise RuntimeError(f"Checksum mismatch for webtrees-{version}.zip")
    return target


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wt(*args):
    """webtrees command line (index.php with arguments)."""
    r = subprocess.run([PHP, "index.php", "--no-interaction", *args], cwd=WT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"webtrees {' '.join(args)}: {r.stdout.strip()} {r.stderr.strip()}")
    return r.stdout


def sql(statement, *values):
    con = sqlite3.connect(os.path.join(WT, "data", "webtrees.sqlite"), timeout=30)
    try:
        cur = con.execute(statement, values)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


class Environment:
    def __init__(self):
        self.process = None
        self.port = 0

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/"

    def start(self):
        shutil.rmtree(WORK, ignore_errors=True)
        os.makedirs(WORK)
        with zipfile.ZipFile(_zip()) as z:
            z.extractall(WORK)
        os.symlink(MODULE, os.path.join(WT, "modules_v4", "webtrees-contribution-request"))
        router = os.path.join(WORK, "router.php")
        open(router, "w").write(ROUTER)

        self.port = _free_port()
        log = open(os.path.join(WORK, "php.log"), "ab")
        self.process = subprocess.Popen(
            [PHP, "-d", "display_errors=0", "-d", "log_errors=1", "-S", f"127.0.0.1:{self.port}", "-t", WT, router],
            cwd=WT, stdout=log, stderr=subprocess.STDOUT)
        self._wait()

        fields = {
            "lang": "de", "tblpfx": "wt_", "baseurl": "", "dbtype": "sqlite", "dbhost": "", "dbport": "",
            "dbuser": "", "dbpass": "", "dbname": "webtrees", "wtname": "Admin", "wtuser": "admin",
            "wtpass": PASSWORD, "wtemail": "admin@example.invalid", "step": "6",
        }
        try:
            urllib.request.urlopen(self.base + "index.php", urllib.parse.urlencode(fields).encode(), timeout=120).read()
        except urllib.error.HTTPError:
            pass
        if not os.path.isfile(os.path.join(WT, "data", "config.ini.php")):
            raise RuntimeError("webtrees setup failed, see tests/.work/php.log")

        wt("tree", "tree", "--create", "--title=Test tree")
        wt("tree-import", "tree", os.path.join(HERE, "tree.ged"))
        for name, role in ACCOUNTS.items():
            wt("user", name, "--create", f"--real-name={name}", f"--email={name}@example.invalid", f"--password={PASSWORD}")
            wt("user-setting", name, "verified", "1")
            wt("user-setting", name, "verified_by_admin", "1")
            wt("user-tree-setting", name, "tree", "canedit", role)
        return self

    def _wait(self):
        end = time.time() + 20
        while time.time() < end:
            try:
                urllib.request.urlopen(self.base + "public/css/webtrees.min.css", timeout=2).read(1)
                return
            except OSError:
                if self.process.poll() is not None:
                    break
                time.sleep(0.2)
        raise RuntimeError("php -S does not start, see tests/.work/php.log")

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait(5)

    def session(self, user=None):
        s = Session(self.base)
        if user:
            s.login(user, PASSWORD)
        return s


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Response:
    def __init__(self, status, text):
        self.status, self.text = status, text
        try:
            self.json = json.loads(text)
        except ValueError:
            self.json = None

    def __repr__(self):
        return f"Response({self.status}, {self.text[:300]!r})"


class Session:
    """A browser/app-like client: one cookie jar, CSRF token from webtrees' pages, no redirects followed."""

    def __init__(self, base):
        self.base = base
        self.csrf = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect)

    def url(self, route, **params):
        return self.base + "index.php?" + urllib.parse.urlencode({"route": route, **{k: str(v) for k, v in params.items()}})

    def send(self, req):
        try:
            with self.opener.open(req, timeout=60) as r:
                return Response(r.status, r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read().decode("utf-8", "replace"))

    def page(self, route, **params):
        """GET an HTML page and remember its CSRF token."""
        r = self.send(urllib.request.Request(self.url(route, **params)))
        m = re.search(r'name="_csrf" value="([^"]+)"', r.text) or re.search(r'name="csrf" content="([^"]+)"', r.text)
        if m:
            self.csrf = m.group(1)
        return r

    def form(self, route, fields, **params):
        data = urllib.parse.urlencode({"_csrf": self.csrf, **fields}).encode()
        return self.send(urllib.request.Request(self.url(route, **params), data=data, method="POST"))

    def get(self, action, tree="tree", **params):
        return self.send(urllib.request.Request(self.url(f"/module/{MODULE_NAME}/{action}/{tree}", **params)))

    def post(self, action, body, tree="tree", **params):
        req = urllib.request.Request(self.url(f"/module/{MODULE_NAME}/{action}/{tree}", **params),
                                     data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-CSRF-TOKEN", self.csrf)
        return self.send(req)

    def login(self, user, password):
        self.page("/login/tree")
        self.form("/login/tree", {"username": user, "password": password, "url": self.base})
        # Logged out, "My account" redirects to the login page. Also refreshes the CSRF token (new session).
        if self.page("/my-account/tree").status != 200:
            raise RuntimeError(f"Login {user} failed")
