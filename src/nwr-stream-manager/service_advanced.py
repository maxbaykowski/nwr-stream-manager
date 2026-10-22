"""The service's Advanced settings: the port and address the web interface listens on."""

from __future__ import annotations

import logging
from typing import Any

from .web_address import (
    describe_web_host,
    load_web_address_settings,
    save_web_address_settings,
    validate_web_address_payload,
    WebAddressSettings,
)


LOG = logging.getLogger(__name__)


class AdvancedServiceMixin:
    """Mixed into RtlControlService."""

    def advanced_settings(self) -> dict[str, Any]:
        with self.lock:
            settings = load_web_address_settings(self.web_address_state_path)
        return self._advanced_settings_payload(settings)

    def update_advanced_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            current = load_web_address_settings(self.web_address_state_path)
            settings = validate_web_address_payload(payload, current=current)
            if settings != current:
                save_web_address_settings(self.web_address_state_path, settings)
        if settings != current:
            LOG.info(
                "saved the advanced settings for the next start: port %s, %s",
                settings.port,
                "this computer only" if settings.localhost_only else "every network address",
            )
        return self._advanced_settings_payload(settings)

    def _advanced_settings_payload(self, settings: WebAddressSettings) -> dict[str, Any]:
        address = self.web_address
        # What the next start will use, once the command line has had its say.
        next_host = settings.host if not address.host_flag else address.host
        next_port = settings.port if not address.port_flag else address.port
        return {
            "port": settings.port,
            "localhost_only": settings.localhost_only,
            "listening": {
                "host": address.host,
                "port": address.port,
                "description": describe_web_host(address.host),
            },
            "host_flag": address.host_flag,
            "port_flag": address.port_flag,
            "restart_needed": (next_host, next_port) != (address.host, address.port),
        }
