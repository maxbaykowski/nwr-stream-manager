"""Pages for browsers without the page script: the page's views and forms, setup, notices, accounts, EAS alerts and the I/Q recorder."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from . import __version__ as APP_VERSION
from . import page_rendering, pages
from .constants import IQ_RECORDER_MODE_STREAM


HELP_PATH = "/help"
HELP_FILE_NAME = "help.html"
# The deemphasis examples in help, by the address the help page plays them from.
HELP_AUDIO_FILES = {
    "/help/with-deemphasis.mp3": "help_with_deemphasis.mp3",
    "/help/without-deemphasis.mp3": "help_without_deemphasis.mp3",
}
# Forms that work without the page script submit to these.
SETUP_FORM_PATH = "/setup"
CHANGE_PASSWORD_FORM_PATH = "/change-password"
# The page's titles and which menu item each view is under, as the page script has them.
VIEW_TITLES = {
    "dashboard": "Dashboard",
    "rtl": "Configure RTL-SDR",
    "receiver": "Weather Radio Receiver",
    "iq_recorder": "I/Q Recorder",
    "iq_recorder_start": "New I/Q Recording",
    "iq_recording_download": "Download I/Q Recording",
    "logs": "Logs",
    "settings": "Settings",
    "change_password": "Change Account Password",
    "streams": "Manage Streams",
    "add_stream": "Add Stream",
    "stream_settings": "Stream Settings",
    "stream_output": "Icecast Output",
    "eas_alerts": "EAS Alerts",
    "eas_alert_export": "Export Alerts",
    "eas_alert_delete": "Delete Alerts",
    "eas_alert_detail": "EAS Alert Details",
    "restart": "Restart NWR Stream Manager?",
}
VIEW_NAV_ITEMS = {
    "add_stream": "streams",
    "stream_settings": "streams",
    "stream_output": "streams",
    "iq_recorder_start": "iq_recorder",
    "iq_recording_download": "iq_recorder",
    "eas_alert_export": "eas_alerts",
    "eas_alert_delete": "eas_alerts",
    "eas_alert_detail": "eas_alerts",
}
READ_ONLY_RESTRICTED_VIEWS = {
    "rtl", "add_stream", "stream_settings", "stream_output", "iq_recorder_start", "eas_alert_delete", "settings", "restart",
}
# Settings, as the page script has them.
SETTINGS_SECTION_TITLES = {
    "accounts": "Accounts",
    "create_account": "Create account",
    "account": "Account",
    "sdr_sharing": "SDR sharing",
    "pair_device": "Pair a device",
    "find_devices": "Find devices",
    "notifications": "Notifications",
    "fallback_audio": "Fallback audio",
    "record_announcement": "Record an announcement",
    "advanced": "Advanced",
}
SETTINGS_BACK_NAMES = {"accounts": "accounts", "sdr_sharing": "SDR sharing", "fallback_audio": "fallback audio"}
SETTINGS_TABS = ("accounts", "account", "sdr_sharing", "notifications", "fallback_audio", "advanced")
SETTINGS_PAGE_PARENTS = {
    "create_account": "accounts",
    "pair_device": "sdr_sharing",
    "find_devices": "sdr_sharing",
    "record_announcement": "fallback_audio",
}
# The settings that work without the page script.
BASELINE_SETTINGS_SECTIONS = {"accounts", "create_account", "account", "fallback_audio", "notifications", "sdr_sharing", "pair_device", "find_devices", "advanced"}
ACCOUNT_FORM_PATHS = {"/accounts/create", "/accounts/reset-password", "/accounts/read-only", "/accounts/delete"}
EAS_ALERT_EXPORT_PATH = "/eas-alerts/export"
EAS_ALERT_FORM_PATHS = {"/eas-alerts/delete", "/eas-alerts/remove"}
EAS_ALERT_VIEWS = {"eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail"}
EAS_ALERTS_PER_PAGE = 25
IQ_RECORDER_FORM_PATHS = {"/iq-recorder/start", "/iq-recorder/stop", "/iq-recordings/remove"}
IQ_RECORDING_DOWNLOAD_PATH = "/iq-recordings/download"
IQ_RECORDER_VIEWS = {"iq_recorder", "iq_recorder_start", "iq_recording_download"}
RTL_FORM_PATHS = {"/rtl-settings", "/rtl-settings/reset"}
STREAM_FORM_PATHS = {"/streams/settings", "/streams/enabled", "/streams/remove"}
OUTPUT_FORM_PATHS = {"/streams/outputs/save", "/streams/outputs/enabled", "/streams/outputs/reset-soundcard", "/streams/outputs/remove"}
WIZARD_FORM_PATH = "/streams/wizard"
NOTIFICATION_FORM_PATHS = {"/notifications/setup", "/notifications/settings", "/notifications/test", "/notifications/regenerate"}
NOTIFICATION_SETUP_STEPS = ("server_choice", "server_url", "app_install", "topic", "access_url", "test")
NOTIFICATION_RESULTS = {
    "saved": "Notification settings saved.",
    "regenerated": "Topic regenerated. Subscribe your devices to the new topic.",
    "tested": "Test notification sent.",
}
NTFY_PUBLIC_SERVER_URL = "https://ntfy.sh"
DASHBOARD_PARTS_PATH = "/dashboard/parts"
SDR_SHARING_PATHS = {"/sdr-sharing/settings", "/sdr-sharing/pairing/start", "/sdr-sharing/pairing/stop", "/sdr-sharing/pair", "/sdr-sharing/unpair"}
NOTIFICATIONS_SET_UP_HTML = (
    '<p>To get notifications for a stream, go to <a href="/?view=streams">Manage Streams</a>, '
    "select Edit stream settings for the stream, and open the Notifications tab.</p>"
    '<p>To get notifications when the RTL-SDR needs attention, go to <a href="/?view=rtl">Configure RTL-SDR</a> '
    "and turn on Notify when the RTL-SDR requires attention.</p>"
)
ADVANCED_SETTINGS_PATH = "/advanced-settings"
RESTART_PATH = "/restart"
FALLBACK_SETTINGS_PATH = "/fallback-audio/settings"
FALLBACK_UPLOAD_PATH = "/fallback-audio/upload"
# What fallback audio says once one of its forms has gone back to it: where, and what.
FALLBACK_RESULTS = {
    "saved": ("fallback-result", "Fallback audio settings saved."),
    "custom-needed": ("fallback-result", "There's no custom announcement yet. Upload one below, and fallback audio switches to it."),
    "uploaded": ("fallback-audio-result", "Announcement uploaded. Fallback audio now plays the custom announcement."),
}
WIZARD_STEPS = ("station", "type", "service", "codec", "credentials", "quality", "device", "controls")
WIZARD_STEP_PANELS = {
    "station": "wizard_step_station",
    "type": "wizard_step_output_type",
    "service": "wizard_step_service",
    "codec": "wizard_step_codec",
    "credentials": "wizard_step_credentials",
    "quality": "wizard_step_quality",
    "device": "wizard_step_soundcard_device",
    "controls": "wizard_step_soundcard_controls",
}
# What the stream settings say once an output form has gone back to them.
OUTPUT_RESULTS = {
    "output-saved": "Stream output settings saved.",
    "output-removed": "Output removed.",
    "icecast-added": "Icecast output added.",
    "soundcard-added": "Soundcard output added.",
}
# The stream settings tabs that work without the page script, in order.
STREAM_SETTINGS_TABS = {
    "outputs": "Outputs",
    "eas": "EAS recording",
    "fallback_action": "Fallback action",
    "audio": "Audio effects",
    "notifications": "Notifications",
}
STREAM_RESULTS = {"started": "Stream started.", "stopped": "Stream stopped.", "removed": "Stream removed.", "created": "Stream created."}
# The Active SDR list's shared RTL-SDRs, as the page script names them.
REMOTE_SDR_CHOICE_PREFIX = "remote:"
# What a finished form says once it has gone back to the I/Q recorder.
IQ_RECORDER_RESULTS = {
    "started": "I/Q recording started.",
    "stopped": "I/Q recording stopped.",
    "removed": "I/Q recording removed.",
}


def settings_sections_for(owner: bool) -> list[str]:
    sections = ["account", "sdr_sharing", "pair_device", "find_devices", "notifications", "fallback_audio", "record_announcement"]
    # Only the owner chooses where NWR Stream Manager can be reached.
    return ["accounts", "create_account", *sections, "advanced"] if owner else sections


def settings_tab_section(section: str, owner: bool) -> str:
    """The entry in the list of settings that a page of Settings belongs to."""
    if section == "account" and owner:
        # The owner's own password is a page within Accounts.
        return "accounts"
    return SETTINGS_PAGE_PARENTS.get(section, section)


def format_account_date(epoch_seconds: Any) -> str:
    try:
        seconds = float(epoch_seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    if not seconds:
        return "Never"
    moment = datetime.fromtimestamp(seconds)
    return f"{moment:%B} {moment.day}, {moment.year}"


SIGN_IN_REQUIRED_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in required - NWR Stream Manager</title>
</head>
<body>
<h1>Sign in required</h1>
<p>Sign in with your NWR Stream Manager username and password to continue. <a href="/">Try again</a></p>
<p><a href="/help">Help</a></p>
</body>
</html>
"""


SETUP_HTML = pages.setup_page()


MUST_CHANGE_PASSWORD_HTML = pages.change_password_page()


NOTICE_PAGE_HTML = pages.notice_page()
INDEX_HTML = pages.index_page(APP_VERSION)
INDEX_VIEWS = page_rendering.page_views(INDEX_HTML)


def form_message(message: str) -> str:
    """An error from the server, written as a sentence for showing on a page."""
    text = str(message).strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else f"{text}."


def show_form_error(page: str, message_id: str, message: str, title: str) -> str:
    # In the page title too, so it's the first thing read when the page loads.
    page = page_rendering.set_text(page, message_id, message)
    page = page_rendering.set_attribute(page, message_id, "class", "message error")
    page = page_rendering.set_attribute(page, message_id, "role", "alert")
    return page_rendering.set_title(page, f"Error: {message} - {title}")


def render_setup_page(step: str, form_token: str, message: str = "", username: str = "") -> str:
    page = SETUP_HTML.replace("__FORM_TOKEN_INPUT__", page_rendering.form_token_input(form_token))
    page = page_rendering.set_hidden(page, "setup_welcome", step != "welcome")
    page = page_rendering.set_hidden(page, "setup_form", step != "account")
    page = page_rendering.set_hidden(page, "setup_done", step != "done")
    if username:
        page = page_rendering.set_attribute(page, "setup_username", "value", username)
    if message:
        page = show_form_error(page, "setup_message", message, "NWR Stream Manager Setup")
    return page


def render_notice_page(
    heading: str,
    body_html: str,
    continue_to: dict[str, str],
    *,
    copy_label: str = "",
    copy_value: str = "",
    title: str = "",
) -> str:
    """A message on a page of its own, for browsers without the page script.

    The page script shows these messages as notices at the edge of the screen,
    which not every browser can show. Done goes to the main page with continue_to
    as its address, and copy_value is shown in a field it can be copied from.
    """
    page = page_rendering.set_text(NOTICE_PAGE_HTML, "notice_title", heading)
    page = page_rendering.set_content(page, "notice_body", body_html)
    fields = "".join(
        f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">' for name, value in continue_to.items()
    )
    page = page_rendering.set_content(
        page,
        "notice_continue",
        fields
        + '<button id="notice_copy_button" type="button" hidden>Copy to clipboard</button>'
        + '<button id="notice_done" class="primary" type="submit">Done</button>',
    )
    if copy_value:
        page = page_rendering.set_hidden(page, "notice_copy", False)
        page = page_rendering.set_text(page, "notice_copy_label", copy_label)
        page = page_rendering.set_attribute(page, "notice_copy_value", "value", copy_value)
    return page_rendering.set_title(page, f"{title or heading} - NWR Stream Manager")


def render_account_secret_page(summary: str, secret: str) -> str:
    """A temporary password, which the page script shows in a notice."""
    return render_notice_page(
        "Temporary password",
        f"<p>{html.escape(summary)}</p><p>Copy the temporary password and give it to the user. You won't be able to see it again.</p>",
        {"view": "settings", "section": "accounts"},
        copy_label="Temporary password",
        copy_value=secret,
        title=f"{summary} - Temporary password",
    )


def render_change_password_page(form_token: str, message: str = "") -> str:
    page = MUST_CHANGE_PASSWORD_HTML.replace("__FORM_TOKEN_INPUT__", page_rendering.form_token_input(form_token))
    if message:
        page = show_form_error(page, "password_message", message, "Change Account Password")
    return page


FORM_EXPIRED_MESSAGE = "This page had expired, so nothing was changed. Please try again."
ACCOUNTS_PAGE_PATH = "/?view=settings&amp;section=accounts"
EMPTY_ACCOUNTS_ROW = '<tr data-row-key="empty" role="row"><td colspan="4" class="hint" role="cell">No accounts.</td></tr>'


def account_row_html(account: dict[str, Any], requester_id: int, token_input: str) -> str:
    """A row of the accounts table, with plain links and forms for its actions.

    Laid out and keyed as the page script lays out its rows, which have a menu instead.
    """
    account_id = int(account.get("id") or 0)
    name = html.escape(str(account.get("username") or ""))

    def cell(label: str, content: str, class_name: str = "") -> str:
        class_attribute = f' class="{class_name}"' if class_name else ""
        return f'<td{class_attribute} role="cell" data-label="{label}">{content}</td>'

    if account_id == requester_id:
        actions = '<a class="button-link" href="/?view=settings&amp;section=account">Change account password</a>'
    elif account.get("owner"):
        actions = ""
    else:
        read_only = bool(account.get("read_only"))
        change = (
            f'<button type="submit" aria-label="Make {name} an administrator">Make administrator</button>'
            if read_only
            else f'<button type="submit" aria-label="Make {name} read-only">Make read-only</button>'
        )
        actions = (
            f'<a class="button-link" href="{ACCOUNTS_PAGE_PATH}&amp;confirm=reset&amp;account={account_id}" '
            f'aria-label="Reset password for {name}">Reset password</a>'
            f'<form method="post" action="/accounts/read-only">{token_input}'
            f'<input type="hidden" name="account_id" value="{account_id}">'
            f'<input type="hidden" name="read_only" value="{"0" if read_only else "1"}">{change}</form>'
            f'<a class="button-link" href="{ACCOUNTS_PAGE_PATH}&amp;confirm=delete&amp;account={account_id}" '
            f'aria-label="Delete account {name}">Delete account</a>'
        )
    return (
        f'<tr data-row-key="account:{account_id}" role="row">'
        + cell("Username", name)
        + cell("Account type", html.escape(str(account.get("account_type") or "Administrator")))
        + cell("Last accessed", html.escape(format_account_date(account.get("last_accessed_at"))))
        + cell("Actions", f'<div class="row-actions">{actions}</div>' if actions else "")
        + "</tr>"
    )


def account_confirmation_html(kind: str, account: dict[str, Any], token_input: str) -> tuple[str, str]:
    """Asks before resetting a password or deleting an account; the page script uses a dialog."""
    username = str(account.get("username") or "")
    name = html.escape(username)
    if kind == "delete":
        question = f"Delete {username}?"
        explanation = f"{name} won't be able to sign in any more."
        action, button = "/accounts/delete", "Delete account"
    else:
        question = f"Reset the password for {username}?"
        explanation = f"{name} is signed out and given a temporary password, which you'll need to pass on to them."
        action, button = "/accounts/reset-password", "Reset password"
    content = (
        f"<h4>{html.escape(question)}</h4><p>{explanation}</p>"
        f'<form method="post" action="{action}">{token_input}'
        f'<input type="hidden" name="account_id" value="{int(account.get("id") or 0)}">'
        f'<div class="actions"><button type="submit" class="primary" autofocus>{button}</button>'
        f'<a class="button-link" href="{ACCOUNTS_PAGE_PATH}">Cancel</a></div></form>'
    )
    return content, question


def page_route(view: str, **params: Any) -> str:
    """A link to one of the EAS alert pages, as the page script makes them."""
    query = {"view": view}
    query.update({name: str(value) for name, value in params.items() if value})
    return "/?" + urlencode(query)


def eas_alert_stream_label(stream: dict[str, Any]) -> str:
    frequency = f" {stream.get('frequency')} MHz" if stream.get("frequency") else ""
    count = int(stream.get("alert_count") or 0)
    return f"{stream.get('callsign') or 'Unknown'}{frequency} ({count} alert{'' if count == 1 else 's'})"


EAS_RANGE_PARTS = ("month", "day", "year", "hour", "minute", "ampm")


def eas_range_values(form: dict[str, str], kind: str, edge: str, default: dict[str, int]) -> dict[str, str]:
    """A date and time chosen for a range of alerts, as the form's lists have it."""
    hour = int(default["hour"])
    values = {
        "month": str(default["month"]),
        "day": str(default["day"]),
        "year": str(default["year"]),
        "hour": str(hour % 12 or 12),
        "minute": str(default["minute"]),
        "ampm": "PM" if hour >= 12 else "AM",
    }
    for part in EAS_RANGE_PARTS:
        value = form.get(f"eas_{kind}_{edge}_{part}", "")
        if value:
            values[part] = value
    return values


def eas_range_datetime(values: dict[str, str]) -> str:
    """The date and time in a range's lists, written as the page script sends it."""
    try:
        hour = int(values["hour"])
        month, day, year, minute = (int(values[part]) for part in ("month", "day", "year", "minute"))
    except (KeyError, ValueError):
        return "invalid"
    if values.get("ampm") == "AM" and hour == 12:
        hour = 0
    if values.get("ampm") == "PM" and hour != 12:
        hour += 12
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00"


def eas_range_control_html(kind: str, edge: str, legend: str, values: dict[str, str], current_year: int) -> str:
    """The lists for one end of a range of alerts, laid out as the page script lays them out."""
    choices = {
        "month": [(str(number), str(number)) for number in range(1, 13)],
        "day": [(str(number), str(number)) for number in range(1, 32)],
        "year": [(str(year), str(year)) for year in range(current_year, current_year - 11, -1)],
        "hour": [(str(number), str(number)) for number in range(1, 13)],
        "minute": [(str(number), f"{number:02d}") for number in range(60)],
        "ampm": [("AM", "AM"), ("PM", "PM")],
    }
    labels = {"month": "Month", "day": "Day", "year": "Year", "hour": "Hour", "minute": "Minute", "ampm": "AM/PM"}
    fields = []
    for part in EAS_RANGE_PARTS:
        name = f"eas_{kind}_{edge}_{part}"
        options = "".join(
            f'<option value="{value}"{" selected" if value == values.get(part) else ""}>{label}</option>'
            for value, label in choices[part]
        )
        fields.append(f'<label>{labels[part]}<select id="{name}" name="{name}">{options}</select></label>')
    now = f'<input type="submit" name="eas_{kind}_now" value="Now">' if edge == "end" else ""
    return f'<fieldset><legend>{legend}</legend><div class="grid">{"".join(fields)}</div>{now}</fieldset>'


def eas_bulk_options_html(kind: str, presets: list[dict[str, Any]], chosen: str) -> str:
    """The choices of which alerts to export or delete, as the page script lists them."""
    choices = [(str(preset.get("id", "")), f"{preset.get('label', '')} ({preset.get('count', 0)})") for preset in presets]
    choices.append(("manual", "Manually select date range"))
    if chosen not in {value for value, _label in choices}:
        chosen = choices[0][0]
    return "".join(
        f'<label class="checkbox-row"><input type="radio" name="eas_{kind}_mode" value="{html.escape(value)}"'
        f'{" checked" if value == chosen else ""}> {html.escape(label)}</label>'
        for value, label in choices
    )


def format_decimal_bytes(value: float) -> str:
    size = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1000 and index < len(units) - 1:
        size /= 1000
        index += 1
    return f"{round(size)} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"


def iq_recorder_source_text(recorder: dict[str, Any]) -> str:
    if recorder.get("mode") == IQ_RECORDER_MODE_STREAM:
        return str(recorder.get("stream_label") or "Stream channel")
    return "Entire spectrum at 162.475 MHz"
