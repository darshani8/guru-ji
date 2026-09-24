"""Deployments configured before the rename set GURU_* variables; they keep working."""

import os
import unittest
from unittest import mock

from app.config.legacy_env import adopt_legacy_names
from app.config.settings import AppSettings


class LegacyEnvironmentNameTests(unittest.TestCase):
    def test_legacy_names_fill_unset_new_names_only(self):
        environ = {"GURU_ENVIRONMENT": "production", "GURU_APP_NAME": "old", "SAFFRON_APP_NAME": "new", "OTHER": "x"}
        self.assertEqual(adopt_legacy_names(environ), ("GURU_ENVIRONMENT",))
        self.assertEqual(environ["SAFFRON_ENVIRONMENT"], "production")
        self.assertEqual(environ["SAFFRON_APP_NAME"], "new", "the new name always wins")
        self.assertEqual(environ["GURU_ENVIRONMENT"], "production", "the legacy variable stays for anything that names it")
        self.assertNotIn("SAFFRON_OTHER", environ)

    def test_settings_read_a_legacy_deployment(self):
        with mock.patch.dict(os.environ, {"GURU_APP_NAME": "legacy-task", "GURU_VOICE_TTS_PROVIDER": "polly"}, clear=False):
            os.environ.pop("SAFFRON_APP_NAME", None)
            os.environ.pop("SAFFRON_VOICE_TTS_PROVIDER", None)
            settings = AppSettings.from_env()
        self.assertEqual(settings.app_name, "legacy-task")
        self.assertEqual(settings.voice_tts_provider, "polly")

    def test_a_local_database_from_before_the_rename_keeps_being_used(self):
        import tempfile

        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONTROL_DATABASE_URL", None)
            try:
                os.chdir(folder)
                os.mkdir("data")
                open("data/guru_ji.db", "wb").close()
                self.assertEqual(AppSettings.from_env().control_database_url, "sqlite:///./data/guru_ji.db")
                self.assertEqual(AppSettings().control_database_url, "sqlite:///./data/guru_ji.db")
                open("data/agentic_saffron.db", "wb").close()
                self.assertEqual(AppSettings.from_env().control_database_url, "sqlite:///./data/agentic_saffron.db")
            finally:
                os.chdir(previous)

    def test_connector_reads_the_legacy_names_too(self):
        from connector.app.main import ConnectorSettings

        with mock.patch.dict(os.environ, {"GURU_CONNECTOR_SOURCE_ID": "legacy_source"}, clear=False):
            os.environ.pop("SAFFRON_CONNECTOR_SOURCE_ID", None)
            self.assertEqual(ConnectorSettings().source_id, "legacy_source")
        with mock.patch.dict(os.environ, {"GURU_CONNECTOR_SOURCE_ID": "legacy_source", "SAFFRON_CONNECTOR_SOURCE_ID": "new_source"}, clear=False):
            self.assertEqual(ConnectorSettings().source_id, "new_source")


if __name__ == "__main__":
    unittest.main()
