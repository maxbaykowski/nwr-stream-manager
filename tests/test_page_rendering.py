import base64
import http.client
import re
import tempfile
import threading
import types
import unittest
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
            # Every other script is the check, the starter or the old Internet Explorer fix.
            self.assertEqual(len(re.findall(r"<script>", page)), 3)

    def test_the_more_links_are_plain_links_until_the_page_script_makes_a_menu(self) -> None:
        page = self.web_control.INDEX_HTML
        self.assertIn('<button id="nav_more_button" type="button" aria-haspopup="menu" aria-expanded="false" hidden>More</button>', page)
        self.assertIn('<span id="nav_more_menu" class="nav-more-menu">', page)
        self.assertIn('menu.setAttribute("role", "menu");', page)
        self.assertIn(".js-app .nav-more-menu { position: absolute;", page)

    def test_only_pages_that_work_without_the_page_script_are_shown_without_it(self) -> None:
        pages = __import__("sys").modules["nwr_stream_manager.page_rendering"]
        page = self.web_control.INDEX_HTML
        baseline = {"logs", "settings", "change_password"}
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


if __name__ == "__main__":
    unittest.main()
