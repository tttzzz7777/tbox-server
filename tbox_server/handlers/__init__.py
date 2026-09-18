"""Handler modules for each route group."""

from . import admin, command_ack, heartbeat, poll, report, upload, webui


def register_all(app, registry, config) -> None:
    """Wire up every handler module's routes onto ``app``."""
    heartbeat.register(app, registry, config)
    poll.register(app, registry, config)
    command_ack.register(app, registry, config)
    upload.register(app, registry, config)
    report.register(app, registry, config)
    admin.register(app, registry, config)
    webui.register(app, registry, config)


__all__ = ["register_all"]
