from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_auth import load_web_control_module


def view_markup(html: str, view: str) -> str:
    start = html.index(f'<div id="view_{view}" class="view')
    end = html.index('\n  <div id="view_', start + 1)
    return html[start:end]


class SettingsPageMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()
        cls.html = cls.web_control.INDEX_HTML

    def test_more_menu_has_settings_and_a_password_page_for_read_only_accounts(self) -> None:
        menu = self.html[self.html.index('<span id="nav_more_menu"'):self.html.index("</nav>")]

        self.assertIn('<a id="nav_settings" href="/?view=settings" data-view="settings" hidden>Settings</a>', menu)
        self.assertIn('id="nav_change_password"', menu)
        for old in ("nav_notifications", "nav_accounts", "view=notifications", "view=accounts"):
            self.assertNotIn(old, menu)

    def test_accounts_sharing_and_notifications_are_in_settings(self) -> None:
        settings = view_markup(self.html, "settings")
        rtl = view_markup(self.html, "rtl")

        for element in (
            "accounts-body",
            "open_create_account",
            "new_account_username",
            "sdr_sharing_section",
            "remote_pair_section",
            "paired-devices-body",
            "notification_intro",
            "notification_wizard",
        ):
            self.assertIn(f'id="{element}"', settings)
            self.assertNotIn(f'id="{element}"', rtl)
        for old in ("view_notifications", "view_accounts", "view_create_account"):
            self.assertNotIn(f'id="{old}"', self.html)

    def test_settings_list_is_a_vertical_set_of_tabs_with_matching_panels(self) -> None:
        settings = view_markup(self.html, "settings")

        # Without the page script the list is a form whose buttons open each setting;
        # the page script makes it tabs, or buttons on phones.
        self.assertIn('<form id="settings_list" class="settings-list" method="get" action="/" aria-label="Settings">', settings)
        tabs = re.findall(r'<button id="settings_tab_(\w+)" type="submit" name="section" value="(\w+)" data-settings-section="(\w+)" data-baseline-submit', settings)
        self.assertEqual([tab[0] for tab in tabs], ["general", "accounts", "account", "sdr_sharing", "notifications", "fallback_audio", "advanced"])
        for name, value, section in tabs:
            self.assertEqual(name, value)
            self.assertEqual(name, section)
            self.assertIn(f'<div id="settings_panel_{name}" class="settings-panel" hidden>', settings)
        self.assertIn('list.setAttribute("role", "tablist");', self.html)
        self.assertIn('tab.setAttribute("role", "tab");', self.html)
        self.assertIn("tab.tabIndex = selected ? 0 : -1;", self.html)
        self.assertIn('panel.setAttribute("role", "tabpanel");', self.html)

    def test_fallback_audio_is_set_once_in_settings_not_in_each_stream(self) -> None:
        settings = view_markup(self.html, "settings")
        stream_settings = view_markup(self.html, "stream_settings")

        for element in ("fallback_loop_delay", "fallback-result"):
            self.assertIn(f'id="{element}"', settings)
            self.assertNotIn(f'id="{element}"', stream_settings)
        for element in ("fallback_enabled", "fallback_delay"):
            self.assertNotIn(f'id="{element}"', self.html)
        self.assertIn("Plays on streams whose fallback action is Play fallback audio.", settings)

    def test_settings_has_its_own_heading_and_each_setting_a_heading_below_it(self) -> None:
        settings = view_markup(self.html, "settings")

        self.assertEqual(re.findall(r"<h2>([^<]+)</h2>", settings), ["Settings"])
        self.assertEqual(
            re.findall(r"<h3>([^<]+)</h3>", settings),
            ["General", "Accounts", "Create account", "Change account password", "Account", "SDR sharing", "Pair a device", "Find devices", "Fallback audio", "Record an announcement", "Advanced", "Notifications"],
        )
        self.assertEqual(
            re.findall(r"<h4>([^<]+)</h4>", settings),
            ["Sharing", "Pair with another NWR Stream Manager", "Paired devices", "Upload an announcement", "Record an announcement"],
        )

    def test_dark_mode_includes_the_top_bar(self) -> None:
        dark = self.html[self.html.index("@media (prefers-color-scheme: dark) {"):]
        dark = dark[:dark.index("\n}\n")]

        self.assertIn("header, section, select, input, button, nav a, .button-link { background: #181d24;", dark)
        self.assertIn('nav a[aria-current="page"], nav button[aria-current="page"] { border-color: #6f9be0;', dark)

    def test_phones_show_one_screen_at_a_time_in_either_orientation(self) -> None:
        query = "(max-width: 680px), (max-width: 900px) and (orientation: landscape), (orientation: landscape) and (max-height: 500px)"

        self.assertIn(f'const SETTINGS_SINGLE_PANE_QUERY = "{query}";', self.html)
        self.assertIn(f"@media {query} {{\n  .settings-layout {{ display: block; }}", self.html)


class SdrPairingPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = load_web_control_module().INDEX_HTML
        cls.settings = view_markup(cls.html, "settings")

    def test_pairing_has_its_own_screens_instead_of_sharing_the_device_lists(self) -> None:
        sharing = self.settings[self.settings.index('<div id="settings_sdr_sharing_page">'):self.settings.index('<div id="settings_pair_device_page"')]
        pair = self.settings[self.settings.index('<div id="settings_pair_device_page" hidden>'):self.settings.index('<div id="settings_find_devices_page"')]
        find = self.settings[self.settings.index('<div id="settings_find_devices_page" hidden>'):self.settings.index('<div id="settings_panel_notifications"')]

        self.assertIn('<button id="start_remote_pairing" type="submit" data-baseline-submit>Pair a device</button>', sharing)
        self.assertIn('<button id="find_remote_devices" class="needs-app-inline" type="button">Find devices</button>', sharing)
        self.assertIn('id="paired-devices-body"', sharing)
        for element in ("remote_pairing_code", "copy_remote_pairing_code", "cancel_remote_pairing"):
            self.assertIn(f'id="{element}"', pair)
            self.assertNotIn(f'id="{element}"', sharing)
        for element in ("remote_pair_search_status", "remote_pair_device", "remote_pair_code", "pair_remote_device", "cancel_find_devices"):
            self.assertIn(f'id="{element}"', find)
            self.assertNotIn(f'id="{element}"', sharing)
        self.assertNotIn("remote_pairing_dialog", self.html)

    def test_leaving_or_closing_the_tab_while_pairing_asks_first_and_stops_pairing(self) -> None:
        self.assertIn("return Boolean(wizardHasUnsavedChanges() || outputFormHasUnsavedChanges() || remotePairingPage || fallbackRecordingUnsaved());", self.html)
        self.assertIn('if (!window.confirm("Leave this page? Pairing will stop.")) return false;', self.html)
        self.assertIn('navigator.sendBeacon("/api/remote-sdr/pairing/cancel"', self.html)
        self.assertIn('remoteSdrAction("/api/remote-sdr/pairing/keepalive")', self.html)

    def test_leaving_the_recording_page_with_an_unsaved_recording_asks_first(self) -> None:
        self.assertIn('if (!window.confirm("Leave this page? Your recording hasn\'t been saved.")) return false;', self.html)
        self.assertIn("if (fallbackRecordingUnsaved()) return confirmLeavingFallbackRecording();", self.html)

    def test_uploading_an_announcement_is_one_button_that_opens_the_file_dialog(self) -> None:
        self.assertIn('<button id="fallback_upload" type="button" aria-describedby="fallback_audio_file_hint">Upload announcement</button>', self.html)
        self.assertRegex(self.html, r'<input id="fallback_audio_file" type="file" [^>]*hidden>')
        self.assertIn('document.getElementById("fallback_audio_file").addEventListener("change", uploadFallbackAnnouncementFile);', self.html)

    def test_one_button_plays_and_pauses_the_chosen_announcement_on_its_page_only(self) -> None:
        self.assertIn('<button id="fallback_play" class="needs-app-inline" type="button">Play announcement</button>', self.html)
        self.assertNotIn("fallback_play_default", self.html)
        self.assertNotIn("fallback_play_custom", self.html)
        self.assertIn('fallback_play: {play: "Play announcement", pause: "Pause announcement", page: "fallback_audio"', self.html)
        self.assertIn('if (name !== "settings") stopAnnouncementPlaybackOffPage(name, null);', self.html)
        self.assertIn("stopAnnouncementPlaybackOffPage(currentViewName(), section);", self.html)

    def test_changes_made_on_another_page_show_without_losing_typing_or_playing_the_wrong_announcement(self) -> None:
        self.assertIn('if (!numberFieldIsBeingTyped(document.getElementById("fallback_loop_delay"))) {', self.html)
        self.assertIn('if (!numberFieldIsBeingTyped(document.getElementById("stream_fallback_delay"))) {', self.html)
        self.assertIn('setFallbackAudioResult("Playback stopped because the announcement was changed.");', self.html)

    def test_the_recording_page_has_cancel_instead_of_back_or_discard(self) -> None:
        self.assertIn('<button id="fallback_recording_cancel" type="button">Cancel</button>', self.html)
        self.assertIn('setHidden("settings_back", section === "record_announcement");', self.html)
        self.assertNotIn("fallback_recording_discard", self.html)


class SettingsPermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def handler_for(self, role: str):
        web_control = self.web_control
        handler = object.__new__(web_control.RtlControlHandler)
        sent = []
        handler._send_json = lambda payload, status=None: sent.append((payload, status))
        account = web_control.AccountRecord(
            id=1, username="someone", role=role, must_change_password=False, created_at=0.0, last_accessed_at=None
        )
        return handler, account, sent

    def test_read_only_accounts_cannot_reach_any_setting(self) -> None:
        handler, account, sent = self.handler_for(self.web_control.ACCOUNT_ROLE_READ_ONLY)
        for method, path in (
            ("GET", "/api/notification-settings"),
            ("PATCH", "/api/notification-settings"),
            ("GET", "/api/notification-access-urls"),
            ("POST", "/api/notification-test"),
            ("GET", "/api/remote-sdr"),
            ("PATCH", "/api/remote-sdr"),
            ("POST", "/api/remote-sdr/discover"),
            ("POST", "/api/remote-sdr/pair"),
            ("POST", "/api/remote-sdr/pairing/start"),
            ("POST", "/api/remote-sdr/pairing/keepalive"),
            ("POST", "/api/remote-sdr/pairing/cancel"),
            ("POST", "/api/remote-sdr/unpair"),
            ("GET", "/api/accounts"),
            ("POST", "/api/accounts"),
            ("POST", "/api/account-reset-password"),
            ("PATCH", "/api/account-read-only"),
        ):
            with self.subTest(method=method, path=path):
                sent.clear()
                self.assertFalse(handler._account_authorized_or_response(path, method, account))
                self.assertEqual(sent[0][1], self.web_control.HTTPStatus.FORBIDDEN)

    def test_read_only_accounts_can_still_change_their_password(self) -> None:
        handler, account, _sent = self.handler_for(self.web_control.ACCOUNT_ROLE_READ_ONLY)

        self.assertTrue(handler._account_authorized_or_response("/api/account/password", "POST", account))

    def test_only_the_owner_manages_accounts(self) -> None:
        for role, allowed in (
            (self.web_control.ACCOUNT_ROLE_OWNER, True),
            (self.web_control.ACCOUNT_ROLE_ADMIN, False),
        ):
            handler, account, _sent = self.handler_for(role)
            for method, path in (("GET", "/api/accounts"), ("POST", "/api/accounts"), ("POST", "/api/account-reset-password")):
                with self.subTest(role=role, method=method, path=path):
                    self.assertEqual(handler._account_authorized_or_response(path, method, account), allowed)

    def test_administrators_can_reach_sharing_and_notifications(self) -> None:
        handler, account, _sent = self.handler_for(self.web_control.ACCOUNT_ROLE_ADMIN)
        for method, path in (("GET", "/api/notification-settings"), ("POST", "/api/remote-sdr/pair"), ("POST", "/api/account/password")):
            with self.subTest(method=method, path=path):
                self.assertTrue(handler._account_authorized_or_response(path, method, account))

    def test_notifications_never_open_settings_for_read_only_accounts(self) -> None:
        restricted = self.web_control.notification_target_restricted_for_read_only

        self.assertTrue(restricted("/?view=settings&section=notifications"))
        self.assertTrue(restricted("/?view=notifications"))
        self.assertFalse(restricted("/?view=change_password"))


# The page's own rules for which settings an account has and where Back goes,
# run with a pretend account and address.
PAGE_FUNCTIONS = (
    "settingsSectionsForAccount",
    "normalizeSettingsSection",
    "settingsTabSection",
    "settingsParentSection",
    "routeState",
    "routeFromLocation",
)
HARNESS = r"""
let role = "owner";
const accountIsOwner = () => role === "owner";
const accountIsReadOnly = () => role === "read_only";
const window = {location: {search: ""}};
const results = {};
for (const name of ["owner", "administrator", "read_only"]) {
  role = name;
  results[name] = {
    sections: settingsSectionsForAccount(),
    normalized: Object.fromEntries(["accounts", "create_account", "account", "sdr_sharing", "notifications", "bogus", ""].map(section => [section, normalizeSettingsSection(section)])),
    list_entry: Object.fromEntries(["accounts", "create_account", "account", "sdr_sharing", "pair_device", "find_devices", "notifications"].map(section => [section, settingsTabSection(section)])),
    back_to: Object.fromEntries(["accounts", "create_account", "account", "sdr_sharing", "pair_device", "find_devices", "notifications"].map(section => [section, settingsParentSection(section)]))
  };
}
results.routes = {};
for (const search of ["?view=notifications", "?view=accounts", "?view=create_account", "?view=settings&section=sdr_sharing", "?view=settings", "?view=change_password"]) {
  window.location.search = search;
  const route = routeFromLocation();
  results.routes[search] = [route.view, route.section || ""];
}
console.log(JSON.stringify(results));
"""


class SettingsPageRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node is not installed")
        page = load_web_control_module().INDEX_HTML
        start = page.index("const SETTINGS_SECTIONS_FROM_OLD_VIEWS")
        parts = [page[start : page.index(";\n", start) + 2]]
        for name in PAGE_FUNCTIONS:
            start = page.index(f"function {name}(")
            parts.append(page[start : page.index("\n}\n", start) + 2])
        script = "\n".join(parts) + HARNESS
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "settings.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([cls.node, str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.results = json.loads(result.stdout)

    def test_the_owner_has_accounts_and_the_others_have_their_own_account(self) -> None:
        self.assertEqual(
            self.results["owner"]["sections"],
            ["general", "accounts", "create_account", "account", "sdr_sharing", "pair_device", "find_devices", "notifications", "fallback_audio", "record_announcement", "advanced"],
        )
        self.assertEqual(
            self.results["administrator"]["sections"],
            ["general", "account", "sdr_sharing", "pair_device", "find_devices", "notifications", "fallback_audio", "record_announcement"],
        )
        self.assertEqual(self.results["read_only"]["sections"], [])

    def test_administrators_asking_for_accounts_get_their_own_account(self) -> None:
        normalized = self.results["administrator"]["normalized"]

        self.assertEqual(normalized["accounts"], "account")
        self.assertEqual(normalized["create_account"], "account")
        self.assertEqual(normalized["bogus"], "")
        self.assertEqual(set(self.results["read_only"]["normalized"].values()), {""})

    def test_the_owners_own_password_and_new_accounts_are_pages_within_accounts(self) -> None:
        owner = self.results["owner"]

        self.assertEqual(owner["list_entry"]["create_account"], "accounts")
        self.assertEqual(owner["list_entry"]["account"], "accounts")
        self.assertEqual(owner["back_to"]["create_account"], "accounts")
        self.assertEqual(owner["back_to"]["account"], "accounts")
        self.assertEqual(owner["back_to"]["notifications"], "")
        self.assertEqual(self.results["administrator"]["back_to"]["account"], "")

    def test_pairing_pages_are_within_sdr_sharing(self) -> None:
        for role in ("owner", "administrator"):
            rules = self.results[role]
            for page in ("pair_device", "find_devices"):
                with self.subTest(role=role, page=page):
                    self.assertEqual(rules["list_entry"][page], "sdr_sharing")
                    self.assertEqual(rules["back_to"][page], "sdr_sharing")

    def test_older_links_open_the_matching_setting(self) -> None:
        self.assertEqual(
            self.results["routes"],
            {
                "?view=notifications": ["settings", "notifications"],
                "?view=accounts": ["settings", "accounts"],
                "?view=create_account": ["settings", "create_account"],
                "?view=settings&section=sdr_sharing": ["settings", "sdr_sharing"],
                "?view=settings": ["settings", ""],
                "?view=change_password": ["change_password", ""],
            },
        )


if __name__ == "__main__":
    unittest.main()
