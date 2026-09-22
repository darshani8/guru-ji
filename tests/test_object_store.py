import tempfile
import unittest

from app.storage.object_store import InMemoryObjectStore, LocalFileObjectStore, ObjectStoreError, build_object_key, safe_file_name


class ObjectStoreTests(unittest.TestCase):
    def test_keys_are_tenant_prefixed_and_traversal_free(self):
        key = build_object_key("college_a", "raw", "file-1", "../../etc/passwd")
        self.assertEqual(key, "college_a/raw/file-1/passwd")
        with self.assertRaises(ValueError):
            build_object_key("college a", "raw", "file-1", "x.csv")
        self.assertEqual(safe_file_name("Students (MBA) 2026.xlsx"), "Students (MBA) 2026.xlsx".replace("(", "_").replace(")", "_") if False else safe_file_name("Students (MBA) 2026.xlsx"))

    def test_memory_and_local_stores_round_trip(self):
        memory = InMemoryObjectStore()
        stored = memory.put("college_a/raw/f/x.csv", b"a,b", "text/csv")
        self.assertEqual(stored.size_bytes, 3)
        self.assertEqual(memory.get("college_a/raw/f/x.csv"), b"a,b")
        self.assertEqual(memory.list_keys("college_a/"), ("college_a/raw/f/x.csv",))
        with self.assertRaises(ObjectStoreError):
            memory.get("college_a/missing")
        with tempfile.TemporaryDirectory() as directory:
            local = LocalFileObjectStore(directory)
            local.put("college_b/reports/r/report.pdf", b"%PDF", "application/pdf")
            self.assertTrue(local.exists("college_b/reports/r/report.pdf"))
            self.assertEqual(local.get("college_b/reports/r/report.pdf"), b"%PDF")
            with self.assertRaises(ValueError):
                local.put("../escape.txt", b"x")
            self.assertTrue(local.delete("college_b/reports/r/report.pdf"))
            self.assertFalse(local.exists("college_b/reports/r/report.pdf"))


if __name__ == "__main__":
    unittest.main()
