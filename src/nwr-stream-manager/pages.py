"""NWR Stream Manager's web pages, put together from the files in assets.

Each page is a file in assets. The main page's styles and scripts have files of
their own and are put into it here, so it is still sent as a single page.
"""

from __future__ import annotations

import html
from pathlib import Path

from . import page_rendering

ASSETS_DIRECTORY = Path(__file__).resolve().parent / "assets"


def read_asset(name: str) -> str:
    return (ASSETS_DIRECTORY / name).read_text(encoding="utf-8")


def assemble(name: str, parts: dict[str, str] | None = None) -> str:
    """A page from assets, with its own parts put in, then the parts every page shares."""
    page = read_asset(name)
    for key, value in (parts or {}).items():
        page = page.replace(f"__{key}__", value)
    for key, value in (
        ("PAGE_SCRIPT_LOADER", page_rendering.PAGE_SCRIPT_LOADER),
        ("PAGE_SCRIPT_STARTER", page_rendering.PAGE_SCRIPT_STARTER),
        ("BASE_PAGE_STYLES", page_rendering.BASE_PAGE_STYLES),
    ):
        page = page.replace(f"__{key}__", value)
    return page


def index_page(version: str) -> str:
    """The main page, with every view in it."""
    return assemble(
        "index.html",
        {
            "INDEX_STYLES": read_asset("index.css"),
            "BASIC_SCRIPT": read_asset("basic.js"),
            "APP_SCRIPT": read_asset("app.js"),
            "APP_VERSION": html.escape(version),
        },
    )


def setup_page() -> str:
    """First-time setup, for creating the owner account."""
    return assemble("setup.html")


def change_password_page() -> str:
    """For an account that has to change its temporary password first."""
    return assemble("change_password.html")


def notice_page() -> str:
    """A message on a page of its own, where the page script would show a notice."""
    return assemble("notice.html")
