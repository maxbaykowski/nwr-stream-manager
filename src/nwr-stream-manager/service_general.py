"""The service's General settings: the instance name and location."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from .general_settings import save_general_settings, validate_general_settings_payload


LOG = logging.getLogger(__name__)


class GeneralServiceMixin:
    """Mixed into RtlControlService."""

    @property
    def instance_name(self) -> str:
        return self.general.instance_name

    @property
    def location(self) -> str:
        return self.general.location

    def general_settings(self) -> dict[str, Any]:
        with self.lock:
            return asdict(self.general)

    def update_general_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            current = self.general
            settings = validate_general_settings_payload(payload, current)
            if settings != current:
                save_general_settings(self.general_state_path, settings)
                self.general = settings
        if settings != current:
            if settings.instance_name != current.instance_name:
                LOG.info("instance name changed to %r", settings.instance_name)
            if settings.location != current.location:
                LOG.info("location changed to %r", settings.location)
            # Every open page shows the change at once.
            self.announce_settings_changed()
        return self.general_settings()
