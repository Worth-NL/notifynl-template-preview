import logging
from unittest.mock import MagicMock

from app import configure_global_logging


class TestPDFLibraryErrors:
    def test_weasyprint_svg_warning_is_routed_to_kibana(self, mocker, app):
        fake_kibana_handler = MagicMock()
        fake_kibana_handler.level = logging.NOTSET

        mocker.patch.object(app.logger, "handlers", [fake_kibana_handler])
        # configure_global_logging changes the real root logger; give it a throwaway handler list and level so the
        # mock handler isn't left attached for the rest of the session, which segfaults the test run at interpreter
        # exit once later tests have logged through it.
        root_logger = logging.getLogger()
        mocker.patch.object(root_logger, "handlers", [])
        mocker.patch.object(root_logger, "level", root_logger.level)

        configure_global_logging(app)

        weasyprint_logger = logging.getLogger("weasyprint")
        weasyprint_logger.warning("Failed to render SVG image https://static-logos.../qr1.svg")

        assert fake_kibana_handler.handle.called

        log_record = fake_kibana_handler.handle.call_args[0][0]
        assert "Failed to render SVG image" in log_record.getMessage()
        assert log_record.levelno == logging.WARNING
