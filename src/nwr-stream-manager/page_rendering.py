"""Pages the server fills in, so NWR Stream Manager works in any browser.

Every page works as plain HTML first: the server sends it complete, links go to
real addresses and forms submit and reload. The page script then improves the
page, but only in a browser that a small check, written for even the oldest
browsers, finds can run it. See PAGE_SCRIPT_LOADER.
"""

from __future__ import annotations

import email.parser
import email.policy
import hmac
import html
import re
import secrets
from urllib.parse import parse_qs


# The newest syntax and browser features the page scripts use. The check is
# written so that browsers too old to understand modern JavaScript can still run
# it, and it marks the page "js-app" only when everything is there. Until then
# the page scripts aren't run at all, so an old browser never meets code it can't
# read, and the page stays a plain one that works without them.
PAGE_SCRIPT_LOADER = """<script>
(function () {
  var w = window, d = document, root = d.documentElement;
  function supported() {
    try {
      new Function("class A {}; var o = {...{a: 1}}; return async (b) => `${b ?? o.a}`;");
    } catch (error) {
      return false;
    }
    var element = w.Element && w.Element.prototype;
    return !!(w.fetch && w.Promise && w.Map && w.Set && w.URL && w.URLSearchParams &&
      w.AbortController && w.matchMedia && w.requestAnimationFrame && w.history &&
      w.history.pushState && Object.fromEntries && element && element.closest &&
      element.replaceChildren && root.classList && root.dataset);
  }
  if (supported()) root.className += " js-app";
})();
</script>
<!--[if lt IE 9]><script>
(function () {
  var names = ["header", "nav", "main", "section", "footer"];
  for (var i = 0; i < names.length; i++) document.createElement(names[i]);
})();
</script><![endif]-->"""

# Page scripts are kept in <script type="text/x-nwr-app"> elements, which no
# browser runs by itself; this runs them where the check above passed.
PAGE_SCRIPT_TYPE = "text/x-nwr-app"
PAGE_SCRIPT_STARTER = """<script>
(function () {
  var d = document;
  if (!/(^| )js-app( |$)/.test(d.documentElement.className)) return;
  var scripts = d.getElementsByTagName("script");
  var sources = [];
  for (var i = 0; i < scripts.length; i++) {
    if (scripts[i].getAttribute("type") === "%s") sources.push(scripts[i].text);
  }
  for (var j = 0; j < sources.length; j++) {
    var script = d.createElement("script");
    script.text = sources[j];
    d.body.appendChild(script);
  }
})();
</script>""" % PAGE_SCRIPT_TYPE

# Styles every page shares: older browsers don't know the newer elements, and
# what only applies with or without the page script.
BASE_PAGE_STYLES = """header, nav, main, section, footer { display: block; }
.js-app .baseline-only { display: none !important; }
.needs-app { display: none; }
.js-app .needs-app { display: block; }"""

FORM_TOKEN_COOKIE_NAME = "nwrstmgr_form"
FORM_TOKEN_FIELD = "form_token"
MAX_FORM_REQUEST_BYTES = 64 * 1024
MAX_FORM_FIELDS = 100


def new_form_token() -> str:
    return secrets.token_urlsafe(32)


def form_token_cookie_header(name: str, token: str) -> str:
    return f"{name}={token}; Path=/; HttpOnly; SameSite=Lax"


def form_token_valid(cookie_token: str, submitted_token: str) -> bool:
    """Whether a form came from one of this server's own pages.

    Each browser gets a random token in a cookie, and every form carries the same
    token. Another website can make a browser submit a form here, but can't read
    the cookie to put the right token in it. Old browsers don't support the newer
    cookie protections, so forms rely on this.
    """
    if not cookie_token or not submitted_token:
        return False
    return hmac.compare_digest(cookie_token.encode("utf-8"), submitted_token.encode("utf-8"))


def parse_form(data: bytes) -> dict[str, str]:
    """The fields of a submitted form, the first value of each."""
    fields = parse_qs(
        data.decode("utf-8", errors="replace"),
        keep_blank_values=True,
        max_num_fields=MAX_FORM_FIELDS,
    )
    return {name: values[0] for name, values in fields.items()}


def parse_multipart_form(data: bytes, content_type: str) -> tuple[dict[str, str], dict[str, bytes]]:
    """The fields and files of a form sent as multipart/form-data, the first of each name.

    Browsers send forms with a file this way, from the oldest to the newest.
    """
    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + data
    )
    if not message.is_multipart():
        raise ValueError("the form was not sent as multipart/form-data")
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name or len(fields) + len(files) >= MAX_FORM_FIELDS:
            continue
        content = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            files.setdefault(name, content)
        else:
            fields.setdefault(name, content.decode("utf-8", errors="replace"))
    return fields, files


def form_token_input(token: str) -> str:
    return f'<input type="hidden" name="{FORM_TOKEN_FIELD}" value="{html.escape(token)}">'


# Changes are only made to the page's markup, never to a script in it.
def _split_markup(page: str) -> tuple[str, str]:
    marker = page.find(f'<script type="{PAGE_SCRIPT_TYPE}"')
    if marker < 0:
        return page, ""
    return page[:marker], page[marker:]


def _opening_tag(markup: str, element_id: str) -> re.Match[str]:
    match = re.search(r'<([A-Za-z][A-Za-z0-9]*)\b[^<>]*\sid="%s"[^<>]*>' % re.escape(element_id), markup)
    if match is None:
        raise KeyError(f"page has no element with id {element_id}")
    return match


def set_hidden(page: str, element_id: str, hidden: bool) -> str:
    markup, scripts = _split_markup(page)
    match = _opening_tag(markup, element_id)
    tag = re.sub(r"\shidden(?=[\s>])", "", match.group(0))
    if hidden:
        tag = tag[:-1] + " hidden>"
    return markup[: match.start()] + tag + markup[match.end() :] + scripts


def set_attribute(page: str, element_id: str, name: str, value: str | None) -> str:
    """Sets, or with None removes, an attribute of an element."""
    markup, scripts = _split_markup(page)
    match = _opening_tag(markup, element_id)
    tag = re.sub(r'\s%s(="[^"]*")?(?=[\s>])' % re.escape(name), "", match.group(0))
    if value is not None:
        tag = f'{tag[:-1]} {name}="{html.escape(value)}">'
    return markup[: match.start()] + tag + markup[match.end() :] + scripts


def _closing_tag_start(markup: str, match: re.Match[str]) -> int:
    """Where an element's closing tag starts, allowing for elements of its kind inside it."""
    name = match.group(1).lower()
    tags = re.compile(r"<(/?)%s\b[^<>]*>" % re.escape(name), re.IGNORECASE)
    depth = 1
    for tag in tags.finditer(markup, match.end()):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return tag.start()
    raise KeyError(f"element {match.group(0)} is not closed")


def set_content(page: str, element_id: str, content_html: str) -> str:
    """Replaces what is inside an element."""
    markup, scripts = _split_markup(page)
    match = _opening_tag(markup, element_id)
    closing = _closing_tag_start(markup, match)
    return markup[: match.end()] + content_html + markup[closing:] + scripts


def append_content(page: str, element_id: str, content_html: str) -> str:
    """Adds to the end of what is inside an element."""
    markup, scripts = _split_markup(page)
    closing = _closing_tag_start(markup, _opening_tag(markup, element_id))
    return markup[:closing] + content_html + markup[closing:] + scripts


def move_element(page: str, element_id: str, target_id: str) -> str:
    """Moves an element to the end of another one."""
    markup, scripts = _split_markup(page)
    match = _opening_tag(markup, element_id)
    closing = _closing_tag_start(markup, match)
    end = markup.index(">", closing) + 1
    element = markup[match.start() : end]
    return append_content(markup[: match.start()] + markup[end:] + scripts, target_id, element)


def set_text(page: str, element_id: str, text: str) -> str:
    return set_content(page, element_id, html.escape(text))


def set_title(page: str, title: str) -> str:
    return re.sub(r"<title>.*?</title>", f"<title>{html.escape(title)}</title>", page, count=1)


def set_site_name(page: str, name: str) -> str:
    """Names this NWR Stream Manager by its instance name in the title and the heading at the top.

    "Dashboard - NWR Stream Manager" becomes "Dashboard - " and the instance name. The
    page script, where a page has one, finds the name in the instance_name_meta tag.
    """
    page = re.sub(
        r"<title>((?:[^<]*? - )?)NWR Stream Manager</title>",
        lambda match: f"<title>{match.group(1)}{html.escape(name)}</title>",
        page,
        count=1,
    )
    if 'id="instance_name_meta"' in page:
        page = set_attribute(page, "instance_name_meta", "content", name)
    if 'id="site_heading"' in page:
        page = set_text(page, "site_heading", name)
    return page


def show_view(page: str, view: str) -> str:
    """Shows one of the main page's views and hides the rest."""
    markup, scripts = _split_markup(page)

    def replace(match: re.Match[str]) -> str:
        hidden = "" if match.group(1) == view else " hidden"
        return f'<div id="view_{match.group(1)}" class="{match.group(2)}"{hidden}>'

    markup = re.sub(r'<div id="view_(\w+)" class="([^"]*\bview\b[^"]*)"(?: hidden)?>', replace, markup)
    return markup + scripts


def view_needs_app(page: str, view: str) -> bool:
    markup, _ = _split_markup(page)
    match = re.search(r'<div id="view_%s" class="([^"]*)"' % re.escape(view), markup)
    return match is None or "needs-app" in match.group(1).split()


def page_views(page: str) -> set[str]:
    markup, _ = _split_markup(page)
    return set(re.findall(r'<div id="view_(\w+)" class="[^"]*\bview\b', markup))


def old_internet_explorer(user_agent: str) -> bool:
    """Internet Explorer 6 and older, which can't hide elements marked hidden."""
    return re.search(r"MSIE [1-6]\.", user_agent or "") is not None


def mark_hidden_for_old_internet_explorer(page: str) -> str:
    """Gives hidden elements a class that Internet Explorer 6 and older can hide."""
    markup, scripts = _split_markup(page)

    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        if re.search(r'\sclass="', tag):
            return re.sub(r'\sclass="', ' class="old-ie-hidden ', tag, count=1)
        return tag[:-1] + ' class="old-ie-hidden">'

    markup = re.sub(r"<[A-Za-z][A-Za-z0-9]*\b[^<>]*\shidden(?=[\s>])[^<>]*>", replace, markup)
    style = "<style>.old-ie-hidden { display: none; }</style>"
    markup = markup.replace("</head>", f"{style}\n</head>", 1)
    return markup + scripts
