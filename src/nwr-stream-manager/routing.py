"""Which method of the request handler answers each request.

Each part of the handler marks the methods that answer requests with ``@route``, so the
addresses each part answers are listed next to the code that answers them.
"""

from __future__ import annotations

from typing import Any, Callable


def route(method: str, *paths: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a handler method as answering ``method`` requests for each of ``paths``.

    The method is called with the parsed request address.
    """

    def mark(function: Callable[..., Any]) -> Callable[..., Any]:
        function.routes = (*getattr(function, "routes", ()), *((method, path) for path in paths))  # type: ignore[attr-defined]
        return function

    return mark


def route_table(handler_class: type) -> dict[tuple[str, str], str]:
    """(method, path) -> the name of the handler method that answers it, from every part of the handler."""
    table: dict[tuple[str, str], str] = {}
    for part in handler_class.__mro__:
        for name, value in vars(part).items():
            for key in getattr(value, "routes", ()):
                if key in table and table[key] != name:
                    raise RuntimeError(f"{key[0]} {key[1]} is answered by both {table[key]} and {name}")
                table[key] = name
    return table
