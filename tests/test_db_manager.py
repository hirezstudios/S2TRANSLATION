import unittest
import sqlite3
import os
import sys

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src import db_manager
from src import config

TEST_DB_PATH = 'test_app.db'

class TestDbManager(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Set up the database once for all tests in this class."""
        # Ensure any old test DB is removed
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        # Initialize a fresh database
        db_manager.initialize_database(TEST_DB_PATH)

    @classmethod
    def tearDownClass(cls):
        """Remove the test database once after all tests run."""
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def setUp(self):
        """Set up a connection and cursor before each test."""
        self.conn = sqlite3.connect(TEST_DB_PATH)
        self.cursor = self.conn.cursor()
        # Clear relevant tables before each test to ensure isolation
        self.cursor.execute("DELETE FROM VectorStoreMappings")
        self.cursor.execute("DELETE FROM VectorStoreSets")
        self.cursor.execute("DELETE FROM sqlite_sequence WHERE name='VectorStoreSets'") # Reset autoincrement
        self.cursor.execute("DELETE FROM sqlite_sequence WHERE name='VectorStoreMappings'") # Reset autoincrement
        self.conn.commit()

    def tearDown(self):
        """Close the connection after each test."""
        self.conn.close()

    def test_add_vector_store_set(self):
        """Test adding a single vector store set."""
        filename = "test_file.csv"
        notes = "Test notes"
        set_id = db_manager.add_vector_store_set(TEST_DB_PATH, filename, notes)
        self.assertIsNotNone(set_id, "Should return a set ID")
        self.assertEqual(set_id, 1, "First set ID should be 1")

        # Verify the record in the database
        self.cursor.execute("SELECT set_id, source_csv_filename, notes, is_active FROM VectorStoreSets WHERE set_id = ?", (set_id,))
        record = self.cursor.fetchone()
        self.assertIsNotNone(record, "Record should exist in DB")
        self.assertEqual(record[0], set_id)
        self.assertEqual(record[1], filename)
        self.assertEqual(record[2], notes)
        self.assertEqual(record[3], 0, "New set should be inactive by default") # is_active should be 0 (False)

    def test_add_vector_store_mapping(self):
        """Test adding a vector store mapping linked to a set."""
        # First, add a set to link to
        set_id = db_manager.add_vector_store_set(TEST_DB_PATH, "mapping_test.csv", "Mapping test")
        self.assertIsNotNone(set_id)

        lang_code = "frFR"
        col_name = "tg_frFR"
        mapping_id = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, lang_code, col_name)
        self.assertIsNotNone(mapping_id, "Should return a mapping ID")
        self.assertEqual(mapping_id, 1, "First mapping ID should be 1")

        # Verify the record in the database
        self.cursor.execute("SELECT mapping_id, set_id, language_code, column_name, status FROM VectorStoreMappings WHERE mapping_id = ?", (mapping_id,))
        record = self.cursor.fetchone()
        self.assertIsNotNone(record, "Record should exist in DB")
        self.assertEqual(record[0], mapping_id)
        self.assertEqual(record[1], set_id)
        self.assertEqual(record[2], lang_code)
        self.assertEqual(record[3], col_name)
        self.assertEqual(record[4], 'pending', "Initial status should be 'pending'")

    def test_get_vector_store_sets(self):
        """Test retrieving multiple vector store sets."""
        # Add some sets
        set_id1 = db_manager.add_vector_store_set(TEST_DB_PATH, "file1.csv", "Notes 1")
        set_id2 = db_manager.add_vector_store_set(TEST_DB_PATH, "file2.csv", "Notes 2")

        # Retrieve sets
        sets = db_manager.get_vector_store_sets(TEST_DB_PATH)

        self.assertIsNotNone(sets)
        self.assertEqual(len(sets), 2, "Should retrieve two sets")

        # Check order (should be newest first based on insertion ID/timestamp proxy)
        # Note: get_vector_store_sets orders by upload_timestamp DESC
        # Since we insert sequentially, higher ID = later timestamp
        self.assertEqual(sets[0]['set_id'], set_id2)
        self.assertEqual(sets[0]['source_csv_filename'], "file2.csv")
        self.assertEqual(sets[0]['notes'], "Notes 2")
        self.assertEqual(sets[0]['is_active'], 0)

        self.assertEqual(sets[1]['set_id'], set_id1)
        self.assertEqual(sets[1]['source_csv_filename'], "file1.csv")
        self.assertEqual(sets[1]['notes'], "Notes 1")
        self.assertEqual(sets[1]['is_active'], 0)

    def test_get_vector_store_sets_empty(self):
        """Test retrieving sets when none exist."""
        sets = db_manager.get_vector_store_sets(TEST_DB_PATH)
        self.assertIsNotNone(sets)
        self.assertEqual(len(sets), 0, "Should return an empty list when no sets exist")

    def test_delete_vector_store_set(self):
        """Test deleting a vector store set and its mappings."""
        # Add a set and some mappings
        set_id = db_manager.add_vector_store_set(TEST_DB_PATH, "delete_test.csv", "To be deleted")
        self.assertIsNotNone(set_id)
        mapping_id1 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "frFR", "tg_frFR")
        mapping_id2 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "esLA", "tg_esLA")
        self.assertIsNotNone(mapping_id1)
        self.assertIsNotNone(mapping_id2)

        # Verify they exist
        self.cursor.execute("SELECT COUNT(*) FROM VectorStoreSets WHERE set_id = ?", (set_id,))
        self.assertEqual(self.cursor.fetchone()[0], 1)
        self.cursor.execute("SELECT COUNT(*) FROM VectorStoreMappings WHERE set_id = ?", (set_id,))
        self.assertEqual(self.cursor.fetchone()[0], 2)

        # Delete the set
        deleted = db_manager.delete_vector_store_set(TEST_DB_PATH, set_id)
        self.assertTrue(deleted, "Deletion should return True")

        # Verify they are gone
        self.cursor.execute("SELECT COUNT(*) FROM VectorStoreSets WHERE set_id = ?", (set_id,))
        self.assertEqual(self.cursor.fetchone()[0], 0, "Set should be deleted")
        self.cursor.execute("SELECT COUNT(*) FROM VectorStoreMappings WHERE set_id = ?", (set_id,))
        self.assertEqual(self.cursor.fetchone()[0], 0, "Mappings should be deleted")

    def test_delete_nonexistent_set(self):
        """Test deleting a set ID that doesn't exist."""
        deleted = db_manager.delete_vector_store_set(TEST_DB_PATH, 999)
        self.assertTrue(deleted, "Deleting a non-existent set should still return True (idempotent)")

    def test_get_mappings_for_set(self):
        """Test retrieving mappings for a specific set."""
        set_id1 = db_manager.add_vector_store_set(TEST_DB_PATH, "set1.csv", "Set 1")
        set_id2 = db_manager.add_vector_store_set(TEST_DB_PATH, "set2.csv", "Set 2")
        map1_1 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id1, "frFR", "tg_frFR")
        map1_2 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id1, "esLA", "tg_esLA")
        map2_1 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id2, "deDE", "tg_deDE")

        mappings = db_manager.get_mappings_for_set(TEST_DB_PATH, set_id1)
        self.assertIsNotNone(mappings)
        self.assertEqual(len(mappings), 2)
        # Check contents (order is defined by query ORDER BY language_code)
        self.assertEqual(mappings[0]['mapping_id'], map1_2) # esLA comes before frFR
        self.assertEqual(mappings[0]['language_code'], 'esLA')
        self.assertEqual(mappings[0]['column_name'], 'tg_esLA')
        self.assertEqual(mappings[0]['status'], 'pending')
        self.assertIsNone(mappings[0]['vector_store_path'])

        self.assertEqual(mappings[1]['mapping_id'], map1_1)
        self.assertEqual(mappings[1]['language_code'], 'frFR')
        self.assertEqual(mappings[1]['column_name'], 'tg_frFR')
        self.assertEqual(mappings[1]['status'], 'pending')
        self.assertIsNone(mappings[1]['vector_store_path'])

        # Test retrieving for a set with no mappings
        set_id3 = db_manager.add_vector_store_set(TEST_DB_PATH, "set3.csv", "Set 3 No Mappings")
        mappings3 = db_manager.get_mappings_for_set(TEST_DB_PATH, set_id3)
        self.assertIsNotNone(mappings3)
        self.assertEqual(len(mappings3), 0)

        # Test retrieving for non-existent set
        mappings_none = db_manager.get_mappings_for_set(TEST_DB_PATH, 999)
        self.assertIsNotNone(mappings_none) # Function should return empty list, not None
        self.assertEqual(len(mappings_none), 0)

    def test_update_mapping_status(self):
        """Test updating the status and OpenAI IDs of a mapping."""
        set_id = db_manager.add_vector_store_set(TEST_DB_PATH, "update.csv", "Update Test")
        mapping_id = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "jaJP", "tg_jaJP")
        self.assertIsNotNone(mapping_id)

        # Initial state check
        self.cursor.execute("SELECT status, openai_vs_id, openai_file_id FROM VectorStoreMappings WHERE mapping_id=?", (mapping_id,))
        record_initial = self.cursor.fetchone()
        self.assertEqual(record_initial[0], 'pending')
        self.assertIsNone(record_initial[1])
        self.assertIsNone(record_initial[2])

        # Update status to processing
        updated1 = db_manager.update_mapping_status(TEST_DB_PATH, mapping_id, 'processing')
        self.assertTrue(updated1)
        self.cursor.execute("SELECT status, openai_vs_id, openai_file_id FROM VectorStoreMappings WHERE mapping_id=?", (mapping_id,))
        record1 = self.cursor.fetchone()
        self.assertEqual(record1[0], 'processing')
        self.assertIsNone(record1[1]) # IDs should remain None
        self.assertIsNone(record1[2])

        # Update status to completed with IDs
        vs_id = "vs_abc123"
        file_id = "file_xyz789"
        updated2 = db_manager.update_mapping_status(TEST_DB_PATH, mapping_id, 'completed', openai_vs_id=vs_id, openai_file_id=file_id)
        self.assertTrue(updated2)
        self.cursor.execute("SELECT status, openai_vs_id, openai_file_id FROM VectorStoreMappings WHERE mapping_id=?", (mapping_id,))
        record2 = self.cursor.fetchone()
        self.assertEqual(record2[0], 'completed')
        self.assertEqual(record2[1], vs_id)
        self.assertEqual(record2[2], file_id)

        # Update status to failed (IDs should persist)
        updated3 = db_manager.update_mapping_status(TEST_DB_PATH, mapping_id, 'failed')
        self.assertTrue(updated3)
        self.cursor.execute("SELECT status, openai_vs_id, openai_file_id FROM VectorStoreMappings WHERE mapping_id=?", (mapping_id,))
        record3 = self.cursor.fetchone()
        self.assertEqual(record3[0], 'failed')
        self.assertEqual(record3[1], vs_id) # IDs should still be there
        self.assertEqual(record3[2], file_id)

        # Test updating non-existent mapping
        updated_bad = db_manager.update_mapping_status(TEST_DB_PATH, 999, 'failed')
        self.assertFalse(updated_bad, "Updating non-existent mapping should return False")

    def test_activate_set(self):
        """Test activating a set and deactivating others."""
        set_id1 = db_manager.add_vector_store_set(TEST_DB_PATH, "set1.csv", "Set 1")
        set_id2 = db_manager.add_vector_store_set(TEST_DB_PATH, "set2.csv", "Set 2")
        set_id3 = db_manager.add_vector_store_set(TEST_DB_PATH, "set3.csv", "Set 3")

        # Initially, none are active
        self.cursor.execute("SELECT set_id FROM VectorStoreSets WHERE is_active = 1")
        self.assertIsNone(self.cursor.fetchone())

        # Activate set 2
        activated1 = db_manager.activate_set(TEST_DB_PATH, set_id2)
        self.assertTrue(activated1)
        self.cursor.execute("SELECT set_id FROM VectorStoreSets WHERE is_active = 1")
        active_set = self.cursor.fetchone()
        self.assertIsNotNone(active_set)
        self.assertEqual(active_set[0], set_id2)
        self.cursor.execute("SELECT COUNT(*) FROM VectorStoreSets WHERE is_active = 0")
        self.assertEqual(self.cursor.fetchone()[0], 2) # Sets 1 and 3 should be inactive

        # Activate set 1
        activated2 = db_manager.activate_set(TEST_DB_PATH, set_id1)
        self.assertTrue(activated2)
        self.cursor.execute("SELECT set_id FROM VectorStoreSets WHERE is_active = 1")
        active_set = self.cursor.fetchone()
        self.assertIsNotNone(active_set)
        self.assertEqual(active_set[0], set_id1)
        self.cursor.execute("SELECT COUNT(*) FROM VectorStoreSets WHERE is_active = 0")
        self.assertEqual(self.cursor.fetchone()[0], 2) # Sets 2 and 3 should be inactive

        # Activate set 1 again (should be idempotent)
        activated3 = db_manager.activate_set(TEST_DB_PATH, set_id1)
        self.assertTrue(activated3)
        self.cursor.execute("SELECT set_id FROM VectorStoreSets WHERE is_active = 1")
        self.assertEqual(self.cursor.fetchone()[0], set_id1)
        self.cursor.execute("SELECT COUNT(*) FROM VectorStoreSets WHERE is_active = 0")
        self.assertEqual(self.cursor.fetchone()[0], 2)

        # Try activating a non-existent set
        activated_bad = db_manager.activate_set(TEST_DB_PATH, 999)
        self.assertFalse(activated_bad, "Activating non-existent set should return False")
        # Verify the previously active set (set 1) remains active
        self.cursor.execute("SELECT set_id FROM VectorStoreSets WHERE is_active = 1")
        self.assertEqual(self.cursor.fetchone()[0], set_id1)

if __name__ == '__main__':
    unittest.main() 