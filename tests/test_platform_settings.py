import unittest

from app.config.settings import AppSettings

PRODUCTION = {
    "environment": "production", "dev_bearer_token": "not-the-default-token", "allowed_origins": ("https://guru.example.test",), "control_database_url": "postgresql://user:pass@localhost/guru",
    "oidc_issuer_url": "https://issuer.example.test/", "oidc_audience": "guru-api", "oidc_jwks_url": "https://issuer.example.test/.well-known/jwks.json", "demo_data_enabled": False,
    "pdp_mode": "cerbos", "cerbos_url": "https://cerbos.internal", "connector_scope_attestation_required": True, "model_provider": "litellm", "litellm_model_id": "approved/model", "audit_fail_closed": True,
}


class PlatformSettingsTests(unittest.TestCase):
    def test_defaults_are_local_and_ephemeral_for_memory_control_plane(self):
        self.assertEqual(AppSettings(control_database_url=":memory:").resolved_institution_database_url(), ":memory:")
        self.assertEqual(AppSettings(control_database_url="postgresql://u:p@h/db").resolved_institution_database_url(), "postgresql://u:p@h/db")
        self.assertEqual(AppSettings().resolved_institution_database_url(), "sqlite:///./data/institution_data.db")
        AppSettings().ensure_safe_for_production()

    def test_platform_validation_rules(self):
        for kwargs, message in (
            ({"object_store_backend": "s3"}, "GURU_S3_BUCKET"),
            ({"job_queue": "sqs"}, "GURU_SQS_QUEUE_URL"),
            ({"ocr_engine": "magic"}, "GURU_OCR_ENGINE"),
            ({"email_provider": "smtp"}, "GURU_EMAIL_SENDER"),
            ({"email_provider": "smtp", "email_sender": "a@b.c"}, "GURU_SMTP_HOST"),
            ({"embedding_provider": "ollama"}, "GURU_EMBEDDING_BASE_URL"),
            ({"intelligence_search_provider": "tavily"}, "GURU_WEB_SEARCH_API_KEY"),
            ({"agent_planner": "model"}, "real model provider"),
            ({"mapping_confidence_threshold": 0.2}, "GURU_MAPPING_CONFIDENCE_THRESHOLD"),
            ({"voice_agent_mode": "shout"}, "GURU_VOICE_AGENT_MODE"),
        ):
            with self.assertRaisesRegex(ValueError, message, msg=str(kwargs)):
                AppSettings(**kwargs).ensure_safe_for_production()

    def test_production_accepts_a_platform_only_deployment_and_rejects_partial_ones(self):
        platform = AppSettings(**PRODUCTION, institution_database_url="postgresql://u:p@h/db", object_store_backend="s3", s3_bucket="bucket", job_queue="sqs", sqs_queue_url="https://sqs.example/q")
        platform.ensure_safe_for_production()
        self.assertTrue(platform.platform_production_ready())
        with self.assertRaisesRegex(ValueError, "GURU_INSTITUTION_CONNECTOR_BASE_URL"):
            AppSettings(**PRODUCTION).ensure_safe_for_production()
        with_connector = dict(PRODUCTION, institution_connector_base_url="https://connector.example.test", institution_connector_auth_token="secret")
        with self.assertRaisesRegex(ValueError, "INSTITUTION_DATABASE_URL"):
            AppSettings(**with_connector, institution_database_url="sqlite:///./data/institution_data.db").ensure_safe_for_production()
        with self.assertRaisesRegex(ValueError, "GURU_OBJECT_STORE=s3"):
            AppSettings(**with_connector, institution_database_url="postgresql://u:p@h/db").ensure_safe_for_production()
        with self.assertRaisesRegex(ValueError, "GURU_JOB_QUEUE"):
            AppSettings(**with_connector, institution_database_url="postgresql://u:p@h/db", object_store_backend="s3", s3_bucket="b").ensure_safe_for_production()
        AppSettings(**with_connector, platform_enabled=False).ensure_safe_for_production()


if __name__ == "__main__":
    unittest.main()
