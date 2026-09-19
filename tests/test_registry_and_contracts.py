import unittest

from app.config.source_registry import SourceDefinition, SourceRegistry
from app.domain.principals import Capability
from app.policy.data_classification import DataClassification, DisclosureLevel, can_disclose
from app.tools.base import ToolDefinition, validate_tools_against_sources
from app.tools.registry import ToolRegistry


class RegistryAndContractTests(unittest.TestCase):
    def test_tool_registry_rejects_duplicate_names(self):
        source = SourceDefinition("s1", "c1", "Source", "demo", ("t1",))
        sources = SourceRegistry((source,))
        tool = ToolDefinition("t1", "read", Capability.ASK_READ_ONLY, ("s1",))
        registry = ToolRegistry((tool,))
        validate_tools_against_sources((tool,), sources)
        with self.assertRaises(ValueError):
            registry.register(tool)

    def test_restricted_data_is_never_disclosed(self):
        self.assertTrue(can_disclose(DataClassification.CONFIDENTIAL, DisclosureLevel.AGGREGATE))
        self.assertFalse(can_disclose(DataClassification.RESTRICTED, DisclosureLevel.AGGREGATE))


if __name__ == "__main__":
    unittest.main()
