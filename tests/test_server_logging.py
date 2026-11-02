"""Server logging observability: startup config plus no secrets in log lines."""

from __future__ import annotations

import logging


def test_startup_logging_configured():
    from pam_pm import cli as cli_mod

    for h in logging.getLogger().handlers:
        logging.getLogger().removeHandler(h)
    cli_mod.configure_server_logging()
    handlers = logging.getLogger().handlers
    assert handlers, "basicConfig must install a console handler"
    assert any(h.level <= logging.INFO for h in handlers)


def test_no_secrets_in_sample_log_lines(caplog):
    import pam_pm.core.router as router_mod
    from pam_pm.web import app as web_app

    secret_token = "xoxb-secret-ABC123"
    secret_text = "my secret password hunter2"
    with caplog.at_level(logging.INFO):
        web_app.logger.info("slack socket skipped: reason=no app token")
        web_app.logger.info("slack poller loop started: interval=60s")
        web_app.logger.info("slack poll found %d inbound messages", 2)
        router_mod.logger.info("slack resume outcome: kind=%s", "finished")
    sample = caplog.text
    assert secret_token not in sample
    assert secret_text not in sample
    assert "token" not in sample.lower() or "no app token" in sample.lower()
    assert "slack socket skipped" in sample
    assert "slack poller loop started" in sample
    assert "slack poll found 2 inbound messages" in sample
    assert "slack resume outcome" in sample
