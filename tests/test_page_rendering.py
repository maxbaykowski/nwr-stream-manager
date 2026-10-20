import base64
import http.client
import re
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from test_auth import load_web_control_module


class PageEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        import sys

        cls.pages = sys.modules["nwr_stream_manager.page_rendering"]

    PAGE = (
        '<html><head><title>Old</title></head><body>'
        '<a id="link" href="/" aria-current="page">Link</a>'
        '<div id="view_one" class="view">One</div>'
        '<div id="view_two" class="view needs-app" hidden>Two</div>'
        '<p id="message" class="message"></p>'
        '<script type="text/x-nwr-app">const html = \'<a id="link" hidden>\';</script>'
        '</body></html>'
    )

    def test_elements_are_changed_but_page_scripts_are_not(self) -> None:
        page = self.pages.set_hidden(self.PAGE, "link", True)
        page = self.pages.set_attribute(page, "link", "aria-current", None)
        page = self.pages.set_text(page, "message", "<b>Saved</b>")
        page = self.pages.set_title(page, "New & improved")

        self.assertIn('<a id="link" href="/" hidden>', page)
        self.assertIn('<p id="message" class="message">&lt;b&gt;Saved&lt;/b&gt;</p>', page)
        self.assertIn("<title>New &amp; improved</title>", page)
        self.assertIn("const html = '<a id=\"link\" hidden>';", page)

    def test_one_view_is_shown(self) -> None:
        page = self.pages.show_view(self.PAGE, "two")

        self.assertIn('<div id="view_one" class="view" hidden>', page)
        self.assertIn('<div id="view_two" class="view needs-app">', page)
        self.assertEqual(self.pages.page_views(page), {"one", "two"})
        self.assertTrue(self.pages.view_needs_app(page, "two"))
        self.assertFalse(self.pages.view_needs_app(page, "one"))

    def test_form_tokens_must_match_the_cookie(self) -> None:
        token = self.pages.new_form_token()

        self.assertTrue(self.pages.form_token_valid(token, token))
        self.assertFalse(self.pages.form_token_valid(token, token + "x"))
        self.assertFalse(self.pages.form_token_valid("", ""))
        self.assertIn("HttpOnly", self.pages.form_token_cookie_header(token))

    def test_elements_can_be_moved_and_added_to_even_with_nested_elements(self) -> None:
        page = '<div id="outer"><div id="inner">x</div></div><form id="form"><div>f</div></form><div id="home"><h3>Home</h3></div>'

        moved = self.pages.move_element(page, "form", "home")
        self.assertEqual(moved, '<div id="outer"><div id="inner">x</div></div><div id="home"><h3>Home</h3><form id="form"><div>f</div></form></div>')
        self.assertEqual(self.pages.set_content(page, "outer", "y")[:23], '<div id="outer">y</div>')
        self.assertIn('<div id="outer"><div id="inner">x</div>!</div>', self.pages.append_content(page, "outer", "!"))

    def test_notices_are_a_page_of_their_own_without_the_page_script(self) -> None:
        wc = load_web_control_module()
        page = wc.render_notice_page("Heads up", "<p>Something <b>happened</b>.</p>", {"view": "streams"})

        self.assertEqual(re.search(r"<title>(.*?)</title>", page).group(1), "Heads up - NWR Stream Manager")
        self.assertIn('<h1 id="notice_title">Heads up</h1>', page)
        self.assertIn('<div id="notice_body"><p>Something <b>happened</b>.</p></div>', page)
        self.assertIn('<input type="hidden" name="view" value="streams"><button id="notice_copy_button"', page)
        self.assertIn('<label id="notice_copy" hidden>', page)

    def test_internet_explorer_6_is_given_a_class_it_can_hide(self) -> None:
        self.assertTrue(self.pages.old_internet_explorer("Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1)"))
        self.assertFalse(self.pages.old_internet_explorer("Mozilla/4.0 (compatible; MSIE 7.0; Windows NT 6.0)"))
        page = self.pages.mark_hidden_for_old_internet_explorer(self.PAGE)

        self.assertIn('<div id="view_two" class="old-ie-hidden view needs-app" hidden>', page)
        self.assertIn(".old-ie-hidden { display: none; }</style>\n</head>", page)
        self.assertIn("const html = '<a id=\"link\" hidden>';", page)


class PageScriptLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def test_page_scripts_only_run_after_the_browser_check(self) -> None:
        wc = self.web_control
        for page in (wc.INDEX_HTML, wc.SETUP_HTML, wc.MUST_CHANGE_PASSWORD_HTML):
            head = page[: page.index("</head>")]
            self.assertIn('root.className += " js-app";', head)
            self.assertLess(head.index("js-app"), head.index("<style>"))
            self.assertIn('<script type="text/x-nwr-app">', page)
            self.assertLess(page.index('<script type="text/x-nwr-app">'), page.index('script.text = sources[j];'))
            # Every other script is the check, the starter or the old Internet Explorer fix,
            # and on the main page alert playback for browsers that can't run the page script.
            self.assertEqual(len(re.findall(r"<script>", page)), 4 if page is wc.INDEX_HTML else 3)
        playback = wc.INDEX_HTML.index("alert playback")
        self.assertLess(playback, wc.INDEX_HTML.index('<script type="text/x-nwr-app">'))
        self.assertIn("if (/(^| )js-app( |$)/.test(d.documentElement.className)", wc.INDEX_HTML[playback:])

    def test_the_more_links_are_plain_links_until_the_page_script_makes_a_menu(self) -> None:
        page = self.web_control.INDEX_HTML
        self.assertIn('<button id="nav_more_button" type="button" aria-haspopup="menu" aria-expanded="false" hidden>More</button>', page)
        self.assertIn('<span id="nav_more_menu" class="nav-more-menu">', page)
        self.assertIn('menu.setAttribute("role", "menu");', page)
        self.assertIn(".js-app .nav-more-menu { position: absolute;", page)

    def test_only_pages_that_work_without_the_page_script_are_shown_without_it(self) -> None:
        pages = __import__("sys").modules["nwr_stream_manager.page_rendering"]
        page = self.web_control.INDEX_HTML
        baseline = {
            "logs", "settings", "change_password", "eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail",
            "iq_recorder", "iq_recorder_start", "iq_recording_download", "rtl",
        }
        for view in baseline:
            self.assertFalse(pages.view_needs_app(page, view), view)
        for view in self.web_control.INDEX_VIEWS - baseline:
            self.assertTrue(pages.view_needs_app(page, view), view)


class AccountDateTests(unittest.TestCase):
    def test_dates_read_as_the_page_script_shows_them(self) -> None:
        wc = load_web_control_module()
        self.assertEqual(wc.format_account_date(None), "Never")
        self.assertRegex(wc.format_account_date(1792375834), r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")


class StandInService(types.SimpleNamespace):
    """Just what these pages use; anything else the handler looks up does nothing."""

    def __getattr__(self, name):
        if name == "eas_alert_streams":
            return lambda: {"streams": []}
        if name in {"iq_recorder_status", "redacted_iq_recorder_status"}:
            return lambda: {"active": False, "status": "idle"}
        return lambda *args, **kwargs: None


class BaselinePageServerTests(unittest.TestCase):
    """The pages and forms, as a browser without the page script uses them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def setUp(self) -> None:
        wc = self.web_control
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.revoked: list[int] = []
        service = StandInService(
            accounts=self.accounts,
            auth_sessions=wc.AuthSessionStore(),
            status=lambda read_only=False: {
                "logs": ["first <line>", "repeated", "repeated"],
                "active": False,
                "received_chunks": 4,
                "received_bytes": 2048,
                "last_batch_at": None,
                "capture_error": None,
            },
            revoke_account_long_lived_resources=lambda account_id, **kwargs: self.revoked.append(account_id),
        )
        handler = type("BaselinePageTestHandler", (wc.RtlControlHandler,), {"service": service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def request(self, method: str, path: str, *, fields=None, cookie="", user=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        headers = {}
        body = None
        if fields is not None:
            body = urlencode(fields)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = cookie
        if user:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user[0]}:{user[1]}".encode()).decode()
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response, response.read().decode("utf-8")

    def form_token(self, response) -> str:
        cookie = response.getheader("Set-Cookie") or ""
        match = re.search(r"nwrstmgr_form=([^;]+)", cookie)
        self.assertIsNotNone(match, cookie)
        return match.group(1)

    def title(self, page: str) -> str:
        return re.search(r"<title>(.*?)</title>", page).group(1)

    def test_setup_works_as_plain_pages(self) -> None:
        response, page = self.request("GET", "/")
        token = self.form_token(response)
        self.assertRegex(page, r'<div id="setup_welcome">')
        self.assertRegex(page, r'<form id="setup_form" method="post" action="/setup" hidden>')

        response, page = self.request("GET", "/?step=account", cookie=f"nwrstmgr_form={token}")
        self.assertIsNone(response.getheader("Set-Cookie"))
        self.assertIn('<form id="setup_form" method="post" action="/setup">', page)
        self.assertIn(f'<input type="hidden" name="form_token" value="{token}">', page)

        fields = {"username": "owner", "password": "password1", "confirm_password": "password2", "form_token": token}
        response, page = self.request("POST", "/setup", fields=fields, cookie=f"nwrstmgr_form={token}")
        self.assertEqual(response.status, 400)
        self.assertEqual(self.title(page), "Error: Passwords do not match. - NWR Stream Manager Setup")
        self.assertIn('role="alert"', page)
        self.assertIn('value="owner"', page)
        self.assertFalse(self.accounts.has_account())

        response, page = self.request("POST", "/setup", fields={**fields, "confirm_password": "password1"}, cookie=f"nwrstmgr_form={token}")
        self.assertEqual(response.status, 200)
        self.assertIn('<form id="setup_done" method="get" action="/">', page)
        self.assertTrue(self.accounts.has_account())

        response, _ = self.request("POST", "/setup", fields=fields, cookie=f"nwrstmgr_form={token}")
        self.assertEqual((response.status, response.getheader("Location")), (303, "/"))

    def test_setup_forms_from_other_websites_are_refused(self) -> None:
        fields = {"username": "intruder", "password": "password1", "confirm_password": "password1"}
        for cookie, token in (("", ""), ("nwrstmgr_form=real", "guessed")):
            response, page = self.request("POST", "/setup", fields={**fields, "form_token": token}, cookie=cookie)
            self.assertEqual(response.status, 400)
            self.assertIn("This page had expired", page)
        self.assertFalse(self.accounts.has_account())

    def test_pages_ask_for_a_sign_in_with_a_page_and_the_api_with_json(self) -> None:
        self.accounts.create_admin("owner", "password1", "password1")

        response, page = self.request("GET", "/?view=logs")
        self.assertEqual(response.status, 401)
        self.assertIn("<title>Sign in required - NWR Stream Manager</title>", page)
        response, page = self.request("GET", "/api/status")
        self.assertEqual((response.status, page), (401, '{"error":"authentication required"}'))

    def test_logs_are_filled_in_and_other_pages_say_they_need_a_newer_browser(self) -> None:
        self.accounts.create_admin("owner", "password1", "password1")
        owner = ("owner", "password1")

        _, page = self.request("GET", "/?view=logs", user=owner)
        self.assertEqual(self.title(page), "Logs - NWR Stream Manager")
        self.assertIn('<div id="view_logs" class="view">', page)
        self.assertIn('<section id="baseline_unavailable" class="baseline-only" hidden>', page)
        self.assertIn('<span data-row-key="first &lt;line&gt;">first &lt;line&gt;\n</span>', page)
        self.assertIn('<span data-row-key="repeated#1">repeated\n</span>', page)
        self.assertIn('<span id="chunks">4</span>', page)
        self.assertIn('<a id="nav_logs" href="/?view=logs" data-view="logs" aria-current="page">', page)
        self.assertIn('<a id="nav_settings" href="/?view=settings" data-view="settings">', page)

        _, page = self.request("GET", "/?view=streams", user=owner)
        self.assertIn('<section id="baseline_unavailable" class="baseline-only">', page)
        self.assertIn('<div id="view_streams" class="view needs-app">', page)

    def test_read_only_accounts_see_their_own_menu_and_no_settings(self) -> None:
        self.accounts.create_admin("owner", "password1", "password1")
        account, secret = self.accounts.create_account("viewer", True)
        self.accounts.change_password(account["id"], secret, "password2", "password2")

        _, page = self.request("GET", "/?view=rtl", user=("viewer", "password2"))
        self.assertEqual(self.title(page), "Dashboard - NWR Stream Manager")
        self.assertIn('<a id="nav_rtl" href="/?view=rtl" data-view="rtl" hidden>', page)
        self.assertIn('<a id="nav_settings" href="/?view=settings" data-view="settings" hidden>', page)
        self.assertIn('<a id="nav_change_password" href="/?view=change_password" data-view="change_password">', page)

    def owner_with_token(self):
        self.accounts.create_admin("owner", "password1", "password1")
        owner = ("owner", "password1")
        response, page = self.request("GET", "/?view=settings&section=accounts", user=owner)
        token = self.form_token(response)
        return owner, token, f"nwrstmgr_form={token}"

    def test_the_owner_manages_accounts_with_plain_forms(self) -> None:
        owner, token, cookie = self.owner_with_token()

        _, page = self.request("GET", "/?view=settings&section=accounts", cookie=cookie, user=owner)
        self.assertEqual(self.title(page), "Accounts - Settings - NWR Stream Manager")
        self.assertIn('<a class="button-link" href="/?view=settings&amp;section=account">Change account password</a>', page)
        self.assertIn('<button id="settings_tab_accounts" type="submit" name="section" value="accounts" data-settings-section="accounts" data-baseline-submit aria-current="page">', page)

        response, page = self.request("POST", "/accounts/create", fields={"username": "bad name", "read_only": "1", "form_token": token}, cookie=cookie, user=owner)
        self.assertEqual(response.status, 400)
        self.assertTrue(self.title(page).startswith("Error: Username may contain only letters"))
        self.assertIn('value="bad name"', page)
        self.assertRegex(page, r'<input id="new_account_read_only"[^>]* checked="checked">')

        # The temporary password is on a page of its own, not in a notice some browsers can't show.
        response, page = self.request("POST", "/accounts/create", fields={"username": "helper", "form_token": token}, cookie=cookie, user=owner)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.title(page), "Account helper created. - Temporary password - NWR Stream Manager")
        self.assertNotIn('id="view_settings"', page)
        secret = re.search(r'<input id="notice_copy_value" readonly value="([^"]+)">', page).group(1)
        self.assertIsNotNone(self.accounts.verify_basic_account("helper", secret))
        self.assertIn('<input type="hidden" name="section" value="accounts">', page)
        helper = next(account for account in self.accounts.list_accounts() if account["username"] == "helper")
        _, page = self.request("GET", "/?view=settings&section=accounts", cookie=cookie, user=owner)
        self.assertIn('aria-label="Make helper read-only">Make read-only</button>', page)

        self.request("POST", "/accounts/read-only", fields={"account_id": helper["id"], "read_only": "1", "form_token": token}, cookie=cookie, user=owner)
        self.assertTrue(self.accounts.account_by_id(helper["id"]).is_read_only)

        _, page = self.request("GET", f"/?view=settings&section=accounts&confirm=reset&account={helper['id']}", cookie=cookie, user=owner)
        self.assertEqual(self.title(page), "Reset the password for helper? - Accounts - Settings - NWR Stream Manager")
        self.assertIn('<form method="post" action="/accounts/reset-password">', page)
        _, page = self.request("POST", "/accounts/reset-password", fields={"account_id": helper["id"], "form_token": token}, cookie=cookie, user=owner)
        self.assertEqual(self.title(page), "Password reset for helper. - Temporary password - NWR Stream Manager")
        self.assertIn(helper["id"], self.revoked)

        _, page = self.request("POST", "/accounts/delete", fields={"account_id": helper["id"], "form_token": token}, cookie=cookie, user=owner)
        self.assertEqual(self.title(page), "Account deleted. - Accounts - Settings - NWR Stream Manager")
        self.assertIsNone(self.accounts.account_by_id(helper["id"]))

    def test_account_forms_need_the_owner_and_a_matching_token(self) -> None:
        owner, token, cookie = self.owner_with_token()
        account, secret = self.accounts.create_account("helper", False)
        self.accounts.change_password(account["id"], secret, "password2", "password2")

        response, _ = self.request("POST", "/accounts/delete", fields={"account_id": 1, "form_token": token}, cookie=cookie, user=("helper", "password2"))
        self.assertEqual(response.status, 403)
        response, page = self.request("POST", "/accounts/delete", fields={"account_id": account["id"], "form_token": "guess"}, cookie=cookie, user=owner)
        self.assertEqual(response.status, 400)
        self.assertIn("This page had expired", page)
        self.assertIsNotNone(self.accounts.account_by_id(account["id"]))

    def test_each_account_changes_its_own_password_where_its_settings_are(self) -> None:
        owner, token, cookie = self.owner_with_token()
        account, secret = self.accounts.create_account("helper", False)
        self.accounts.change_password(account["id"], secret, "password2", "password2")
        helper = ("helper", "password2")

        _, page = self.request("GET", "/?view=settings&section=accounts", cookie=cookie, user=helper)
        self.assertEqual(self.title(page), "Account - Settings - NWR Stream Manager")
        self.assertRegex(page, r'<div id="settings_panel_account" class="settings-panel">\s*<h3>Account</h3>\s*<form id="change_password_form"')
        self.assertIn('<a id="change_password_cancel_link" class="baseline-only button-link" href="/?view=settings">', page)

        fields = {"current_password": "wrong", "new_password": "password3", "confirm_password": "password3", "form_token": token}
        response, page = self.request("POST", "/change-password", fields=fields, cookie=cookie, user=helper)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.title(page), "Error: Current password is incorrect. - Account - Settings - NWR Stream Manager")

        _, page = self.request("GET", "/?view=settings&section=account", cookie=cookie, user=owner)
        self.assertRegex(page, r'<div id="settings_owner_password_page">\s*<h3>Change account password</h3>\s*<form id="change_password_form"')

    def test_a_required_password_change_works_as_a_plain_form(self) -> None:
        self.accounts.create_admin("owner", "password1", "password1")
        account, secret = self.accounts.create_account("viewer", True)
        user = ("viewer", secret)

        response, page = self.request("GET", "/", user=user)
        token = self.form_token(response)
        self.assertIn('<form id="password_form" method="post" action="/change-password">', page)
        cookie = f"nwrstmgr_form={token}"
        fields = {"current_password": "wrong", "new_password": "password2", "confirm_password": "password2", "form_token": token}

        response, page = self.request("POST", "/change-password", fields=fields, cookie=cookie, user=user)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.title(page), "Error: Current password is incorrect. - Change Account Password")

        response, _ = self.request("POST", "/change-password", fields={**fields, "current_password": secret}, cookie=cookie, user=user)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/"))
        self.assertEqual(self.revoked, [account["id"]])
        self.assertIsNotNone(self.accounts.verify_basic_account("viewer", "password2"))


class EasAlertServiceStandIn(StandInService):
    """The real alert pages' service methods, on alerts written for the test."""

    REAL = ("eas_alert", "remove_eas_alert", "_eas_alert", "_stream_has_eas_alert_index", "_stream_locked")

    def __getattr__(self, name):
        if name.startswith(self.REAL):
            return getattr(self.service_class, name).__get__(self)
        return super().__getattr__(name)


class EasAlertPageTests(unittest.TestCase):
    """The EAS alert pages, as a browser without the page script uses them."""

    request = BaselinePageServerTests.request
    form_token = BaselinePageServerTests.form_token
    title = BaselinePageServerTests.title

    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def setUp(self) -> None:
        wc = self.web_control
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        state = Path(tempdir.name)
        self.accounts = wc.AccountStore(state / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        account, secret = self.accounts.create_account("viewer", True)
        self.accounts.change_password(account["id"], secret, "password2", "password2")
        self.owner, self.viewer = ("owner", "password1"), ("viewer", "password2")
        self.alerts_directory = state / "streams" / "WXN99" / "alerts"
        self.alerts_directory.mkdir(parents=True)
        # Thirty alerts an hour apart, up to an hour ago.
        now = datetime.now(timezone.utc).replace(microsecond=0)
        alerts = []
        for number in range(30):
            audio = self.alerts_directory / f"alert{number}.wav"
            audio.write_bytes(b"RIFF" + bytes(40))
            issued = now - timedelta(hours=number + 1)
            alerts.append({
                "event_type": "RWT",
                "fips_codes": ["026139"],
                "start_time_utc": issued.isoformat().replace("+00:00", "Z"),
                "expires_at_utc": (issued + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
                "raw_same_header": f"ZCZC-WXR-RWT-026139+0015-{number:07d}-WXN99   -",
                "file_path": str(audio),
            })
        (self.alerts_directory / "index.json").write_text(__import__("json").dumps({"version": 1, "alerts": alerts}))
        stream = {"id": "s1", "station": {"callsign": "WXN99", "frequency": "162.425"}, "outputs": []}
        service = EasAlertServiceStandIn(
            accounts=self.accounts,
            auth_sessions=wc.AuthSessionStore(),
            service_class=wc.RtlControlService,
            lock=threading.RLock(),
            streams=[stream],
            streams_directory=state / "streams",
        )
        self.service = service
        handler = type("EasAlertPageTestHandler", (wc.RtlControlHandler,), {"service": service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def alert_ids(self) -> list[str]:
        return [alert["id"] for page in (1, 2) for alert in self.service.eas_alerts("s1", page, 25)["alerts"]]

    def token(self):
        response, _ = self.request("GET", "/?view=eas_alerts", user=self.owner)
        token = self.form_token(response)
        return token, f"nwrstmgr_form={token}"

    def test_the_alert_list_is_filled_in_with_plain_links(self) -> None:
        _, page = self.request("GET", "/?view=eas_alerts", user=self.owner)
        self.assertEqual(self.title(page), "EAS Alerts - NWR Stream Manager")
        self.assertIn('<a id="nav_eas_alerts" href="/?view=eas_alerts" data-view="eas_alerts" data-eas-entry="restore" aria-current="page">', page)
        self.assertIn('<option value="s1" selected>WXN99 162.425 MHz (30 alerts)</option>', page)
        newest = self.alert_ids()[0]
        self.assertIn(
            f'<a class="stream-item" data-row-key="alert:{newest}" data-alert-id="{newest}" '
            f'href="/?view=eas_alert_detail&amp;stream=s1&amp;alert={newest}&amp;page=1">', page)
        self.assertEqual(page.count('class="stream-item"'), 25)
        self.assertIn('<span id="eas_alert_page" class="hint">Page 1 of 2</span>', page)
        self.assertRegex(page, r'<a id="eas_alert_page_prev_link" class="baseline-only button-link" hidden href="[^"]*">')
        self.assertIn('<a id="eas_alert_page_next_link" class="baseline-only button-link" href="/?view=eas_alerts&amp;stream=s1&amp;page=2">', page)
        self.assertIn('<a id="open_eas_delete_link" class="baseline-only button-link" href="/?view=eas_alert_delete&amp;stream=s1&amp;page=1">', page)

        _, page = self.request("GET", "/?view=eas_alerts&stream=s1&page=2", user=self.owner)
        self.assertEqual(page.count('class="stream-item"'), 5)
        self.assertRegex(page, r'<a id="eas_alert_page_next_link" class="baseline-only button-link" hidden href="[^"]*">')

        _, page = self.request("GET", "/?view=eas_alerts", user=self.viewer)
        self.assertRegex(page, r'<a id="open_eas_delete_link"[^>]* hidden>')

    def test_an_alert_plays_where_the_browser_can_and_links_to_its_neighbours(self) -> None:
        ids = self.alert_ids()
        _, page = self.request("GET", f"/?view=eas_alert_detail&stream=s1&alert={ids[24]}&page=1", user=self.owner)
        self.assertTrue(self.title(page).startswith("Details for alert Required Weekly Test issued on "))
        self.assertIn('<h2 id="eas_alert_detail_title">Required Weekly Test</h2>', page)
        self.assertIn(f'<audio id="eas_alert_baseline_audio" controls preload="none" src="/api/eas-alert-audio?stream_id=s1&amp;alert_id={ids[24]}" data-audio-type="audio/wav">', page)
        self.assertIn(f'<a id="eas_alert_download" role="button" href="/api/eas-alert-audio?stream_id=s1&amp;alert_id={ids[24]}&amp;download=1">', page)
        # The previous alert is the older one, on the next page of the list.
        self.assertIn(f'<a id="eas_alert_previous_link" class="button-link" href="/?view=eas_alert_detail&amp;stream=s1&amp;alert={ids[25]}&amp;page=2">', page)
        self.assertIn(f'<a id="eas_alert_next_link" class="button-link" href="/?view=eas_alert_detail&amp;stream=s1&amp;alert={ids[23]}&amp;page=1">', page)
        self.assertIn('<a id="back_to_eas_alerts" href="/?view=eas_alerts&amp;stream=s1&amp;page=1">', page)

        _, page = self.request("GET", f"/?view=eas_alert_detail&stream=s1&alert={ids[0]}", user=self.owner)
        self.assertRegex(page, r'<a id="eas_alert_next_link"[^>]* hidden>')

        response, page = self.request("GET", "/?view=eas_alert_detail&stream=s1&alert=missing", user=self.owner)
        self.assertEqual(self.title(page), "Error: EAS alert was not found. - EAS Alert Details - NWR Stream Manager")
        self.assertRegex(page, r'<div id="eas_alert_player" class="baseline-only" hidden>')

    def test_an_alert_is_removed_after_a_confirmation(self) -> None:
        token, cookie = self.token()
        alert = self.alert_ids()[3]
        _, page = self.request("GET", f"/?view=eas_alert_detail&stream=s1&alert={alert}&page=1&confirm=remove", cookie=cookie, user=self.owner)
        self.assertTrue(self.title(page).startswith("Remove this EAS alert and its audio file? - Details for alert"))
        self.assertIn('<form method="post" action="/eas-alerts/remove">', page)

        fields = {"stream": "s1", "alert": alert, "page": "1", "form_token": token}
        response, _ = self.request("POST", "/eas-alerts/remove", fields=fields, cookie=cookie, user=self.viewer)
        self.assertEqual(response.status, 403)
        response, page = self.request("POST", "/eas-alerts/remove", fields={**fields, "form_token": "guess"}, cookie=cookie, user=self.owner)
        self.assertEqual(response.status, 400)
        self.assertIn(alert, self.alert_ids())

        response, page = self.request("POST", "/eas-alerts/remove", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual(self.title(page), "Alert removed. - EAS Alerts - NWR Stream Manager")
        self.assertEqual(len(self.alert_ids()), 29)

    def test_alerts_are_exported_from_a_plain_form(self) -> None:
        _, page = self.request("GET", "/?view=eas_alert_export&stream=s1&page=1", user=self.viewer)
        self.assertIn('<form id="eas_export_form" method="get" action="/eas-alerts/export">', page)
        self.assertIn('<div id="eas_export_options"><label class="checkbox-row"><input type="radio" name="eas_export_mode" value="last_24" checked> Last 24 hours (', page)
        self.assertIn('<label class="checkbox-row"><input type="radio" name="eas_export_mode" value="all"> Export all alerts (30)</label>', page)
        self.assertIn('<select id="eas_export_end_minute" name="eas_export_end_minute">', page)
        self.assertIn('<input type="submit" name="eas_export_now" value="Now">', page)

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        authorization = "Basic " + base64.b64encode(b"viewer:password2").decode()
        connection.request("GET", "/eas-alerts/export?stream=s1&eas_export_mode=all", headers={"Authorization": authorization})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Disposition"), 'attachment; filename="WXN99-eas-alerts.zip"')
        self.assertEqual(response.read()[:2], b"PK")

        fields = {"stream": "s1", "page": "1", "eas_export_mode": "manual",
                  "eas_export_start_month": "1", "eas_export_start_day": "1", "eas_export_start_year": str(datetime.now().year - 5),
                  "eas_export_start_hour": "12", "eas_export_start_minute": "0", "eas_export_start_ampm": "AM",
                  "eas_export_end_month": "1", "eas_export_end_day": "2", "eas_export_end_year": str(datetime.now().year - 5),
                  "eas_export_end_hour": "12", "eas_export_end_minute": "0", "eas_export_end_ampm": "AM"}
        response, page = self.request("GET", "/eas-alerts/export?" + urlencode(fields), user=self.viewer)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.title(page), "Error: No alerts were issued during this time. - Export Alerts - NWR Stream Manager")
        # What was chosen is kept.
        self.assertIn('value="manual" checked', page)
        self.assertIn(f'<option value="{datetime.now().year - 5}" selected>', page)

        response, page = self.request("GET", "/eas-alerts/export?" + urlencode({**fields, "eas_export_now": "Now"}), user=self.viewer)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.title(page), "Export Alerts - NWR Stream Manager")
        self.assertRegex(page, r'<select id="eas_export_start_year" name="eas_export_start_year">(<option value="\d+">\d+</option>)*<option value="%d" selected>' % (datetime.now().year - 5))
        self.assertRegex(page, r'<select id="eas_export_end_year" name="eas_export_end_year">(<option value="%d" selected>)' % datetime.now().year)

    def test_alerts_are_deleted_after_a_confirmation(self) -> None:
        token, cookie = self.token()
        fields = {"stream": "s1", "page": "1", "eas_delete_mode": "all", "form_token": token}
        response, page = self.request("POST", "/eas-alerts/delete", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.title(page), "Delete 30 alerts? - Delete Alerts - NWR Stream Manager")
        self.assertIn("30 alerts and their audio files will be permanently deleted.", page)
        self.assertRegex(page, r'<form id="eas_delete_form" method="post" action="/eas-alerts/delete" hidden>')
        self.assertIn('<input type="hidden" name="eas_delete_mode" value="all">', page)
        self.assertIn('<input type="hidden" name="confirmed" value="1">', page)
        self.assertEqual(len(self.alert_ids()), 30)

        response, _ = self.request("POST", "/eas-alerts/delete", fields={**fields, "confirmed": "1"}, cookie=cookie, user=self.viewer)
        self.assertEqual(response.status, 403)
        _, page = self.request("POST", "/eas-alerts/delete", fields={**fields, "confirmed": "1"}, cookie=cookie, user=self.owner)
        self.assertEqual(self.title(page), "30 alerts deleted. - EAS Alerts - NWR Stream Manager")
        self.assertEqual(self.alert_ids(), [])
        self.assertIn("No EAS alerts recorded for this stream.", page)


class IqRecorderServiceStandIn(StandInService):
    """The real recordings list, removal and downloads, with a recorder the test controls."""

    REAL = ("iq_recordings", "remove_iq_recording", "iq_recording_download", "begin_iq_recording_download", "finish_iq_recording_download")

    def __getattr__(self, name):
        if name.startswith(self.REAL):
            return getattr(self.service_class, name).__get__(self)
        return super().__getattr__(name)


class IqRecorderPageTests(unittest.TestCase):
    """The I/Q recorder pages, as a browser without the page script uses them."""

    request = BaselinePageServerTests.request
    form_token = BaselinePageServerTests.form_token
    title = BaselinePageServerTests.title

    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def setUp(self) -> None:
        wc = self.web_control
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        state = Path(tempdir.name)
        self.accounts = wc.AccountStore(state / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        account, secret = self.accounts.create_account("viewer", True)
        self.accounts.change_password(account["id"], secret, "password2", "password2")
        self.owner, self.viewer = ("owner", "password1"), ("viewer", "password2")
        directory = state / "iq-recordings"
        directory.mkdir()
        recordings = []
        for number, started in enumerate((1_790_000_000, 1_790_003_600)):
            path = directory / f"recording{number}.cf32"
            path.write_bytes(bytes(8 * 100))
            recordings.append({
                "id": f"rec{number}", "mode": "stream", "stream_label": "WXN99", "sample_rate": 24000,
                "frequency_hz": 162425000, "duration_seconds": 65, "file_path": str(path), "started_at": started,
            })
        (directory / "index.json").write_text(__import__("json").dumps({"version": 1, "recordings": recordings}))
        self.recorder = {"active": False, "status": "idle"}
        self.started: list[dict] = []
        self.stopped: list[bool] = []
        streams = [
            {"id": "s1", "enabled": True, "station": {"callsign": "WXN99", "frequency": "162.425"}},
            {"id": "s2", "enabled": False, "station": {"callsign": "WZ2560", "frequency": "162.550"}},
        ]
        service = IqRecorderServiceStandIn(
            accounts=self.accounts,
            auth_sessions=wc.AuthSessionStore(),
            service_class=wc.RtlControlService,
            lock=threading.RLock(),
            streams=streams,
            iq_recordings_directory=directory,
            iq_recordings_index_path=directory / "index.json",
            iq_recording_downloads=set(),
            iq_recording_download_accounts={},
            iq_recording_download_recordings={},
            aborted_iq_recording_downloads=set(),
            iq_recorder_status=lambda: dict(self.recorder),
            redacted_iq_recorder_status=lambda: {"active": False, "status": "idle"},
            start_iq_recording=lambda payload, account_id=None: self.started.append(payload),
            stop_iq_recording=lambda: self.stopped.append(True),
            storage_monitor=types.SimpleNamespace(snapshot=lambda: {"filesystems": [{"summary": "Storage: 12.0 GB free"}]}),
        )
        service.iq_recording_download_aborted = lambda download_id: False
        handler = type("IqRecorderPageTestHandler", (wc.RtlControlHandler,), {"service": service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def token(self):
        response, _ = self.request("GET", "/?view=iq_recorder", user=self.owner)
        token = self.form_token(response)
        return token, f"nwrstmgr_form={token}"

    def test_recordings_are_listed_with_plain_links(self) -> None:
        _, page = self.request("GET", "/?view=iq_recorder", user=self.owner)
        self.assertEqual(self.title(page), "I/Q Recorder - NWR Stream Manager")
        self.assertIn('<tr data-row-key="recording:rec1" role="row"><td role="cell" data-label="Time of recording">', page)
        self.assertIn('<td role="cell" data-label="Duration">1:05</td><td role="cell" data-label="Sample rate">24000 S/s</td>', page)
        self.assertIn('<a class="button-link" href="/?view=iq_recording_download&amp;recording=rec1">Download recording<span class="screen-reader-only"> from ', page)
        self.assertIn('<a class="button-link" href="/?view=iq_recorder&amp;confirm=remove&amp;recording=rec1">Remove recording', page)
        # The newest first.
        self.assertLess(page.index("recording:rec1"), page.index("recording:rec0"))
        self.assertIn('<a id="open_iq_start_link" class="baseline-only button-link" href="/?view=iq_recorder_start">', page)
        self.assertRegex(page, r'<div id="iq_recorder_active" hidden>')

        _, page = self.request("GET", "/?view=iq_recorder", user=self.viewer)
        self.assertIn('href="/?view=iq_recording_download&amp;recording=rec1"', page)
        self.assertNotIn("confirm=remove", page)
        self.assertRegex(page, r'<a id="open_iq_start_link"[^>]* hidden>')
        _, page = self.request("GET", "/?view=iq_recorder_start", user=self.viewer)
        self.assertEqual(self.title(page), "Dashboard - NWR Stream Manager")

    def test_a_running_recording_shows_its_status_and_a_stop_button(self) -> None:
        self.recorder = {
            "active": True, "status": "recording", "mode": "stream", "stream_label": "WXN99", "sample_rate": 24000,
            "bytes_written": 2_500_000, "elapsed_seconds": 75.6, "duration_seconds": 600, "storage_remaining_seconds": 7200,
        }
        _, page = self.request("GET", "/?view=iq_recorder", user=self.owner)
        self.assertIn('<div id="iq_recorder_active">', page)
        for element_id, text in (
            ("iq_status", "Recording"), ("iq_source", "WXN99"), ("iq_active_sample_rate", "24000 S/s"),
            ("iq_file_size", "2.5 MB"), ("iq_elapsed", "1:15"), ("iq_remaining", "2:00:00"),
            ("iq_auto_stop_remaining", "8:45"), ("iq_storage", "12.0 GB free"),
        ):
            self.assertIn(f'<span id="{element_id}">{text}</span>', page)
        self.assertIn('<div class="metric" id="iq_auto_stop_metric"><b>', page)
        self.assertIn('<form id="iq_stop_form" class="actions" method="post" action="/iq-recorder/stop">', page)
        self.assertRegex(page, r'<a id="open_iq_start_link"[^>]* hidden>')
        # Every page has the banner, with what a script needs to keep its timer going.
        _, page = self.request("GET", "/?view=eas_alerts", user=self.owner)
        self.assertIn('<div id="iq_recording_banner" class="global-status-banner" data-elapsed-seconds="75" data-duration-seconds="600">', page)
        self.assertIn('<span id="iq_recording_banner_elapsed">1:15</span>', page)
        _, page = self.request("GET", "/?view=iq_recorder_start", user=self.owner)
        self.assertRegex(page, r'<form id="iq_recorder_idle" method="post" action="/iq-recorder/start" hidden>')
        # A read-only account doesn't see it.
        _, page = self.request("GET", "/?view=eas_alerts", user=self.viewer)
        self.assertIn('<div id="iq_recording_banner" class="global-status-banner" hidden>', page)

    def test_recordings_start_and_stop_from_plain_forms(self) -> None:
        token, cookie = self.token()
        _, page = self.request("GET", "/?view=iq_recorder_start", user=self.owner)
        self.assertIn('<select id="iq_stream_select" name="stream_id"><option value="s1">WXN99 162.425 MHz</option></select>', page)
        self.assertIn('<option value="192000" selected>192000 S/s</option>', page)
        self.assertIn('<div id="iq_spectrum_fields">', page)

        fields = {"iq_recording_mode": "spectrum", "sample_rate": "1024000", "duration_minutes": "abc", "form_token": token}
        response, page = self.request("POST", "/iq-recorder/start", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.title(page), "Error: Enter a number of minutes from 0 to 1440. - New I/Q Recording - NWR Stream Manager")
        self.assertIn('<option value="1024000" selected>', page)
        self.assertIn('id="iq_mode_spectrum" name="iq_recording_mode" type="radio" value="spectrum" checked="checked">', page)
        self.assertIn('value="abc"', page)
        response, _ = self.request("POST", "/iq-recorder/start", fields={**fields, "form_token": "guess"}, cookie=cookie, user=self.owner)
        self.assertEqual(response.status, 400)
        response, _ = self.request("POST", "/iq-recorder/start", fields={**fields, "duration_minutes": "5"}, cookie=cookie, user=self.viewer)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.started, [])

        response, _ = self.request("POST", "/iq-recorder/start", fields={**fields, "duration_minutes": "5"}, cookie=cookie, user=self.owner)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/?view=iq_recorder&result=started"))
        self.assertEqual(self.started, [{"mode": "spectrum", "duration_seconds": 300, "sample_rate": "1024000"}])
        fields = {"iq_recording_mode": "stream", "stream_id": "s1", "duration_minutes": "", "form_token": token}
        self.request("POST", "/iq-recorder/start", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual(self.started[-1], {"mode": "stream", "duration_seconds": 0, "stream_id": "s1"})

        self.recorder = {"active": True, "status": "recording"}
        _, page = self.request("GET", "/?view=iq_recorder&result=started", user=self.owner)
        self.assertEqual(self.title(page), "I/Q recording started. - I/Q Recorder - NWR Stream Manager")
        response, _ = self.request("POST", "/iq-recorder/stop", fields={"form_token": token}, cookie=cookie, user=self.owner)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/?view=iq_recorder&result=stopped"))
        self.assertEqual(self.stopped, [True])

        # One that stopped by itself says so when the recorder opens, as the page script does.
        self.recorder = {"active": False, "status": "completed"}
        _, page = self.request("GET", "/?view=iq_recorder&result=started", user=self.owner)
        self.assertEqual(self.title(page), "I/Q recording completed. - I/Q Recorder - NWR Stream Manager")

    def test_a_recording_is_removed_after_a_confirmation(self) -> None:
        token, cookie = self.token()
        _, page = self.request("GET", "/?view=iq_recorder&confirm=remove&recording=rec0", user=self.owner)
        self.assertTrue(self.title(page).startswith("Remove I/Q recording from "))
        self.assertIn('<form method="post" action="/iq-recordings/remove">', page)
        self.assertIn('<input type="hidden" name="recording" value="rec0">', page)

        fields = {"recording": "rec0", "form_token": token}
        response, _ = self.request("POST", "/iq-recordings/remove", fields=fields, cookie=cookie, user=self.viewer)
        self.assertEqual(response.status, 403)
        response, page = self.request("POST", "/iq-recordings/remove", fields={"recording": "missing", "form_token": token}, cookie=cookie, user=self.owner)
        self.assertEqual(self.title(page), "Error: I/Q recording was not found. - I/Q Recorder - NWR Stream Manager")
        response, _ = self.request("POST", "/iq-recordings/remove", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual(response.getheader("Location"), "/?view=iq_recorder&result=removed")
        _, page = self.request("GET", response.getheader("Location"), user=self.owner)
        self.assertEqual(self.title(page), "I/Q recording removed. - I/Q Recorder - NWR Stream Manager")
        self.assertNotIn("recording:rec0", page)

    def test_a_recording_downloads_from_a_plain_form(self) -> None:
        _, page = self.request("GET", "/?view=iq_recording_download&recording=rec0", user=self.viewer)
        self.assertTrue(self.title(page).startswith("Download I/Q recording from "))
        self.assertIn('<form id="iq_download_form" method="get" action="/iq-recordings/download">', page)
        self.assertIn('<input id="iq_download_recording_id" type="hidden" name="id" value="rec0">', page)

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        authorization = "Basic " + base64.b64encode(b"viewer:password2").decode()
        connection.request("GET", "/iq-recordings/download?id=rec0&format=s16", headers={"Authorization": authorization})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("attachment", response.getheader("Content-Disposition"))
        self.assertEqual(len(response.read()), 400)

        response, page = self.request("GET", "/iq-recordings/download?id=rec0&format=wav", user=self.viewer)
        self.assertEqual(response.status, 400)
        self.assertTrue(self.title(page).startswith("Error: "))
        _, page = self.request("GET", "/?view=iq_recording_download&recording=missing", user=self.viewer)
        self.assertIn('<p id="iq_download_recording_label" class="hint">I/Q recording was not found.</p>', page)
        self.assertRegex(page, r'<button id="iq_download_recording" type="submit" data-baseline-submit disabled')


class RtlSettingsPageTests(unittest.TestCase):
    """The Configure RTL-SDR page, as a browser without the page script uses it."""

    request = BaselinePageServerTests.request
    form_token = BaselinePageServerTests.form_token
    title = BaselinePageServerTests.title

    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def setUp(self) -> None:
        wc = self.web_control
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        account, secret = self.accounts.create_account("viewer", True)
        self.accounts.change_password(account["id"], secret, "password2", "password2")
        self.owner, self.viewer = ("owner", "password1"), ("viewer", "password2")
        self.settings = {
            "serial": "00000001", "gain": 33.8, "ppm_correction": 2, "bias_tee": False, "alias_filter_strength": 80,
            "notify_sdr_failures": True, "source_mode": "local", "remote_host_id": "", "remote_buffer_seconds": 0.5,
        }
        self.remote_status = {"reachable": True, "host_id": "host1", "host_name": "Attic", "sdr": {"device_name": "Blog V4"}}
        self.updates: list[dict] = []
        self.resets: list[str] = []
        service = StandInService(
            accounts=self.accounts,
            auth_sessions=wc.AuthSessionStore(),
            status=self.status,
            devices=lambda: {"devices": [{"serial": "00000001", "name": "Blog V4", "vendor": "RTL-SDR Blog"}], "errors": []},
            update=self.update,
            reset_rtl_device=self.reset,
        )
        handler = type("RtlSettingsPageTestHandler", (wc.RtlControlHandler,), {"service": service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def status(self, read_only=False):
        remote = self.settings["source_mode"] == "remote"
        return {
            "settings": dict(self.settings),
            "gain_values": [0.0, 28.0, 33.8, 49.6],
            "notifications": {"topic": "secret-topic"},
            "sdr_reset": None,
            "remote_sdr": {
                "devices": [{"id": "host1", "name": "Attic"}],
                "remote": {"source_mode": self.settings["source_mode"], "device_id": "host1" if remote else "", "status": self.remote_status if remote else None},
            },
        }

    def update(self, changes):
        if changes.get("ppm_correction") == 150:
            raise ValueError("the RTL-SDR could not be opened")
        self.updates.append(changes)
        self.settings.update(changes)
        return self.status()

    def reset(self, serial):
        self.resets.append(serial)
        return {"success": True, "reappeared": True, "message": "RTL-SDR reset finished."}

    def token(self):
        response, _ = self.request("GET", "/?view=rtl", user=self.owner)
        token = self.form_token(response)
        return token, f"nwrstmgr_form={token}"

    def page_fields(self, token):
        """Everything the page sends with Save changes, as it was filled in."""
        _, page = self.request("GET", "/?view=rtl", user=self.owner)
        fields = dict(re.findall(r'<input type="hidden" name="(was_\w+)" value="([^"]*)">', page))
        fields.update({name[4:]: value for name, value in fields.items()})
        for name in ("bias_tee", "notify_sdr_failures"):
            if fields[name] != "1":
                del fields[name]
        return {**fields, "form": "rtl", "form_token": token}

    def test_the_settings_are_filled_in(self) -> None:
        _, page = self.request("GET", "/?view=rtl", user=self.owner)
        self.assertEqual(self.title(page), "Configure RTL-SDR - NWR Stream Manager")
        self.assertIn('<select id="serial" name="serial" aria-describedby="serial_hint">'
                      '<option value="00000001" selected>RTL-SDR Blog, Blog V4, serial 00000001</option>'
                      '<option value="remote:host1">Shared by Attic over the network</option></select>', page)
        self.assertIn('<option value="auto">Automatic gain control</option><option value="0">0 dB</option>'
                      '<option value="28">28 dB</option><option value="33.8" selected>33.8 dB</option>', page)
        self.assertIn('<input id="ppm_correction" name="ppm_correction" type="number" min="-200" max="200" step="1" aria-describedby="ppm_correction_hint" value="2">', page)
        self.assertIn('<span id="alias_filter_strength_label" class="sdr-value" aria-hidden="true">80%</span>', page)
        self.assertIn('<input type="hidden" name="was_serial" value="00000001"><input type="hidden" name="was_gain" value="33.8">', page)
        self.assertRegex(page, r'<input id="notify_sdr_failures" [^>]*checked="checked">')
        self.assertRegex(page, r'<div id="remote_sdr_controls" hidden>')
        # One Save changes, last on the page, under the notification setting.
        view = page[page.index('<div id="view_rtl"'):page.index('<div id="view_receiver"')]
        self.assertEqual(view.count('type="submit"'), 1)
        self.assertRegex(page, r'(?s)<section id="rtl_notifications_section">.*<div id="rtl_save_actions" class="baseline-only">.*</section>(\s*<input type="hidden"[^>]*>)*\s*</form>')
        _, page = self.request("GET", "/?view=rtl", user=self.viewer)
        self.assertEqual(self.title(page), "Dashboard - NWR Stream Manager")

    def test_only_the_settings_changed_are_saved(self) -> None:
        token, cookie = self.token()
        fields = {**self.page_fields(token), "gain": "28", "bias_tee": "1"}
        response, _ = self.request("POST", "/rtl-settings", fields=fields, cookie=cookie, user=self.viewer)
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/rtl-settings", fields={**fields, "form_token": "guess"}, cookie=cookie, user=self.owner)
        self.assertEqual(response.status, 400)
        response, _ = self.request("POST", "/rtl-settings", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/?view=rtl&result=saved"))
        self.assertEqual(self.updates, [{"gain": 28.0, "bias_tee": True}])
        _, page = self.request("GET", "/?view=rtl&result=saved", user=self.owner)
        self.assertEqual(self.title(page), "Changes saved. - Configure RTL-SDR - NWR Stream Manager")

        # Nothing changed, nothing saved; and turning things off counts as a change.
        fields = self.page_fields(token)
        self.request("POST", "/rtl-settings", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual(len(self.updates), 1)
        fields.pop("bias_tee"); fields.pop("notify_sdr_failures")
        self.request("POST", "/rtl-settings", fields={**fields, "gain": "auto"}, cookie=cookie, user=self.owner)
        self.assertEqual(self.updates[-1], {"gain": None, "bias_tee": False, "notify_sdr_failures": False})

        fields = self.page_fields(token)
        for change, message in (({"gain": "31"}, "Select a gain from the list."),
                                ({"ppm_correction": "abc"}, "Enter a PPM correction from -200 to 200."),
                                ({"alias_filter_strength": "101"}, "Enter an alias filter strength from 0 to 100."),
                                ({"remote_buffer_seconds": "20"}, "The network buffer must be between 0.1 and 10 seconds."),
                                ({"ppm_correction": "150", "bias_tee": "1"}, "The RTL-SDR could not be opened.")):
            response, page = self.request("POST", "/rtl-settings", fields={**fields, **change}, cookie=cookie, user=self.owner)
            self.assertEqual(response.status, 400)
            self.assertEqual(self.title(page), f"Error: {message} - Configure RTL-SDR - NWR Stream Manager")
        self.assertEqual(len(self.updates), 2)
        # What was entered stays, and so does what the page first had.
        self.assertIn('value="150"', page)
        self.assertIn('<input type="hidden" name="was_ppm_correction" value="2">', page)
        self.assertRegex(page, r'<input id="bias_tee" [^>]*checked="checked">')

    def test_a_shared_sdr_is_chosen_without_carrying_settings_across(self) -> None:
        token, cookie = self.token()
        fields = {**self.page_fields(token), "serial": "remote:host1", "gain": "28", "remote_buffer_seconds": "2"}
        response, _ = self.request("POST", "/rtl-settings", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual(response.getheader("Location"), "/?view=rtl&result=source-only")
        self.assertEqual(self.updates, [{"source_mode": "remote", "remote_host_id": "host1", "remote_buffer_seconds": 2.0}])
        _, page = self.request("GET", response.getheader("Location"), user=self.owner)
        self.assertTrue(self.title(page).startswith("Now using the RTL-SDR shared by Attic. The gain and other settings you changed were for the other RTL-SDR"))
        self.assertIn('<option value="remote:host1" selected>', page)
        self.assertIn('<div id="remote_sdr_controls">', page)
        self.assertIn('<p id="remote_sdr_status">Connected to Attic. Using Blog V4.</p>', page)
        self.assertRegex(page, r'<a id="reset_sdr_link" [^>]*href="/\?view=rtl&amp;confirm=reset">')

        # Back to this machine's RTL-SDR.
        fields = {**self.page_fields(token), "serial": "00000001"}
        response, _ = self.request("POST", "/rtl-settings", fields=fields, cookie=cookie, user=self.owner)
        self.assertEqual(response.getheader("Location"), "/?view=rtl&result=source")
        self.assertEqual(self.updates[-1], {"source_mode": "local", "serial": "00000001"})
        self.remote_status = {"reachable": False, "host_id": "host1", "host_name": "Attic"}
        self.settings.update(source_mode="remote", remote_host_id="host1")
        _, page = self.request("GET", "/?view=rtl", user=self.owner)
        self.assertIn("Attic is not reachable. Check that it is running and sharing its RTL-SDR.", page)

    def test_the_sdr_is_reset_after_a_confirmation(self) -> None:
        token, cookie = self.token()
        _, page = self.request("GET", "/?view=rtl&confirm=reset", user=self.owner)
        self.assertEqual(self.title(page), "Reset the RTL-SDR? - Configure RTL-SDR - NWR Stream Manager")
        self.assertIn('<h2>Reset the RTL-SDR?</h2>', page)
        self.assertRegex(page, r'<form id="rtl_settings_form" [^>]*hidden>')
        response, _ = self.request("POST", "/rtl-settings/reset", fields={"form_token": token}, cookie=cookie, user=self.viewer)
        self.assertEqual(response.status, 403)
        response, page = self.request("POST", "/rtl-settings/reset", fields={"form_token": token}, cookie=cookie, user=self.owner)
        self.assertEqual(self.resets, ["00000001"])
        self.assertEqual(self.title(page), "RTL-SDR reset finished. - Configure RTL-SDR - NWR Stream Manager")

if __name__ == "__main__":
    unittest.main()
