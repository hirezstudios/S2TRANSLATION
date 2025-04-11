import unittest
import os
import sys
import io
import shutil
from unittest.mock import patch, MagicMock

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app # Import the factory function
from src import config, db_manager

TEST_DB_PATH = 'test_app_integration.db'
TEST_UPLOAD_FOLDER = 'test_uploads'

class TestAppAdminPrepare(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Set up test client and config once for the class."""
        # Configure for testing
        config.DATABASE_FILE = TEST_DB_PATH
        config.UPLOAD_FOLDER = TEST_UPLOAD_FOLDER

        # Create a test app instance
        cls.app = create_app() # Use the factory
        cls.app.config['TESTING'] = True
        cls.app.config['WTF_CSRF_ENABLED'] = False # Disable CSRF for testing forms
        cls.app.config['DEBUG'] = False
        cls.client = cls.app.test_client()

        # Ensure clean test environment
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        if os.path.exists(TEST_UPLOAD_FOLDER):
            shutil.rmtree(TEST_UPLOAD_FOLDER) # Remove dir and contents

        # Initialize DB and folders needed by the app create process
        db_manager.initialize_database(TEST_DB_PATH)
        os.makedirs(TEST_UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(config.USER_PROMPT_DIR, exist_ok=True)
        os.makedirs(config.USER_ARCHIVE_DIR, exist_ok=True)


    @classmethod
    def tearDownClass(cls):
        """Clean up test environment after all tests."""
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        if os.path.exists(TEST_UPLOAD_FOLDER):
            shutil.rmtree(TEST_UPLOAD_FOLDER)
        # Clean up other dirs created if necessary
        if os.path.exists(config.USER_PROMPT_DIR):
            shutil.rmtree(config.USER_PROMPT_DIR)

    def setUp(self):
        """Clear DB tables before each test."""
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM VectorStoreMappings")
        cursor.execute("DELETE FROM VectorStoreSets")
        cursor.execute("DELETE FROM sqlite_sequence WHERE name='VectorStoreSets'")
        cursor.execute("DELETE FROM sqlite_sequence WHERE name='VectorStoreMappings'")
        conn.commit()
        conn.close()
        # Clear upload folder before each test
        if os.path.exists(TEST_UPLOAD_FOLDER):
            shutil.rmtree(TEST_UPLOAD_FOLDER)
        os.makedirs(TEST_UPLOAD_FOLDER, exist_ok=True)


    def tearDown(self):
        """Clean up after each test (e.g., uploaded files)."""
        # Already handled in setUp generally, but can add specific cleanup if needed
        pass

    # Use patch to mock dependencies for this test
    @patch('app.db_manager') # Mock the entire db_manager module used in app.py
    @patch('app.threading.Thread') # Mock the Thread class
    def test_prepare_vs_success(self, mock_thread_class, mock_db):
        """Test successful CSV upload and background task start."""
        # Configure mocks
        mock_db.add_vector_store_set.return_value = 1 # Simulate successful set creation returning ID 1
        mock_db.add_vector_store_mapping.return_value = 101 # Simulate successful mapping creation
        mock_thread_instance = MagicMock()
        mock_thread_class.return_value = mock_thread_instance # Return a mock instance when Thread() is called

        # Prepare mock file data
        file_content = b"src_enUS,tg_frFR,tg_esLA\nhello,bonjour,hola\nworld,monde,mundo"
        mock_file = io.BytesIO(file_content)

        # Simulate POST request with file
        response = self.client.post('/admin/prepare_vs',
                                     content_type='multipart/form-data',
                                     data={'full_translation_csv': (mock_file, 'translations.csv'),
                                            'notes': 'Test upload notes'},
                                     follow_redirects=True) # Follow redirect to admin page

        self.assertEqual(response.status_code, 200) # Should redirect successfully
        self.assertIn(b'Admin - Vector Store Management', response.data) # Should be back on admin page
        self.assertIn(b'Successfully uploaded translations.csv', response.data) # Check for success flash message
        self.assertIn(b'Vector Store creation started in the background for Set ID 1', response.data)

        # Assertions: Check if mocks were called correctly
        mock_db.add_vector_store_set.assert_called_once_with(TEST_DB_PATH, 'translations.csv', 'Test upload notes')
        # Check mapping calls - order isn't guaranteed, so check calls individually
        mock_db.add_vector_store_mapping.assert_any_call(TEST_DB_PATH, 1, 'frFR', 'tg_frFR')
        mock_db.add_vector_store_mapping.assert_any_call(TEST_DB_PATH, 1, 'esLA', 'tg_esLA')
        self.assertEqual(mock_db.add_vector_store_mapping.call_count, 2)

        # Assert background thread was started
        mock_thread_class.assert_called_once() # Check if Thread() was called
        mock_thread_instance.start.assert_called_once() # Check if thread.start() was called

        # Verify the file was actually saved (optional but good)
        expected_path = os.path.join(TEST_UPLOAD_FOLDER, 'translations.csv')
        self.assertTrue(os.path.exists(expected_path))

    def test_prepare_vs_no_file_part(self):
        """Test submitting the form without the file input field."""
        response = self.client.post('/admin/prepare_vs',
                                     content_type='multipart/form-data',
                                     data={'notes': 'No file part test'},
                                     follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        self.assertIn(b'No file part in the request', response.data)

    def test_prepare_vs_no_file_selected(self):
        """Test submitting the form with the file field but no file chosen."""
        response = self.client.post('/admin/prepare_vs',
                                     content_type='multipart/form-data',
                                     data={'full_translation_csv': (io.BytesIO(b""), ''), # Empty filename
                                            'notes': 'No file selected test'},
                                     follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        self.assertIn(b'No selected file', response.data)

    def test_prepare_vs_invalid_extension(self):
        """Test uploading a file with an invalid extension."""
        mock_file = io.BytesIO(b"this is not a csv")
        response = self.client.post('/admin/prepare_vs',
                                     content_type='multipart/form-data',
                                     data={'full_translation_csv': (mock_file, 'test.txt'),
                                            'notes': 'Invalid extension test'},
                                     follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        self.assertIn(b'Invalid file type. Only CSV files are allowed', response.data)
        # Check that the invalid file was not saved
        expected_path = os.path.join(TEST_UPLOAD_FOLDER, 'test.txt')
        self.assertFalse(os.path.exists(expected_path))

    @patch('app.db_manager')
    def test_prepare_vs_db_error_on_set(self, mock_db):
        """Test DB error when trying to add the VectorStoreSet record."""
        # Configure mocks
        mock_db.add_vector_store_set.return_value = None # Simulate failure

        # Prepare mock file data
        file_content = b"src_enUS,tg_frFR\nhello,bonjour"
        mock_file = io.BytesIO(file_content)

        response = self.client.post('/admin/prepare_vs',
                                     content_type='multipart/form-data',
                                     data={'full_translation_csv': (mock_file, 'db_error.csv'),
                                            'notes': 'DB error test'},
                                     follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        self.assertIn(b'Database error creating vector store set entry', response.data)

        # Assertions
        mock_db.add_vector_store_set.assert_called_once_with(TEST_DB_PATH, 'db_error.csv', 'DB error test')
        mock_db.add_vector_store_mapping.assert_not_called() # Should not proceed to mappings
        mock_db.delete_vector_store_set.assert_not_called() # delete shouldn't be called if set wasn't created

        # Verify the file was cleaned up (since the DB add failed)
        expected_path = os.path.join(TEST_UPLOAD_FOLDER, 'db_error.csv')
        self.assertFalse(os.path.exists(expected_path), "File should be cleaned up on DB set error")

    @patch('app.db_manager')
    @patch('app.threading.Thread') # Still need to patch thread even if not used
    def test_prepare_vs_csv_header_error(self, mock_thread_class, mock_db):
        """Test uploading a CSV with no valid target language columns."""
        # Configure mocks
        mock_db.add_vector_store_set.return_value = 2 # Simulate successful set creation
        # We expect delete to be called for cleanup
        mock_db.delete_vector_store_set.return_value = True

        # Prepare mock file data (missing tg_ prefix)
        file_content = b"src_enUS,frFR,esLA\nhello,bonjour,hola\nworld,monde,mundo"
        mock_file = io.BytesIO(file_content)

        response = self.client.post('/admin/prepare_vs',
                                     content_type='multipart/form-data',
                                     data={'full_translation_csv': (mock_file, 'bad_header.csv'),
                                            'notes': 'Bad header test'},
                                     follow_redirects=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        self.assertIn(b'No target language columns (e.g., tg_frFR) found in CSV header', response.data)

        # Assertions
        mock_db.add_vector_store_set.assert_called_once_with(TEST_DB_PATH, 'bad_header.csv', 'Bad header test')
        mock_db.add_vector_store_mapping.assert_not_called() # No valid columns found
        mock_db.delete_vector_store_set.assert_called_once_with(TEST_DB_PATH, 2) # Should clean up the created set ID 2
        mock_thread_class.assert_not_called() # Thread should not start

        # Verify the file was cleaned up
        expected_path = os.path.join(TEST_UPLOAD_FOLDER, 'bad_header.csv')
        self.assertFalse(os.path.exists(expected_path), "File should be cleaned up on header error")

    # --- Tests for /admin/activate_vs --- #

    def test_activate_vs_success(self):
        """Test successfully activating a completed set."""
        # 1. Add a set and completed mappings directly to DB for testing
        set_id = db_manager.add_vector_store_set(TEST_DB_PATH, "activate_ok.csv", "Ready to Activate")
        self.assertIsNotNone(set_id)
        map_id1 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "frFR", "tg_frFR")
        map_id2 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "esLA", "tg_esLA")
        db_manager.update_mapping_status(TEST_DB_PATH, map_id1, 'completed', '/path/to/vs1')
        db_manager.update_mapping_status(TEST_DB_PATH, map_id2, 'completed', '/path/to/vs2')

        # 2. Add another set to ensure it gets deactivated
        set_id_other = db_manager.add_vector_store_set(TEST_DB_PATH, "other.csv", "Should be deactivated")
        db_manager.activate_set(TEST_DB_PATH, set_id_other) # Make it active initially

        # 3. Make the POST request to activate the first set
        response = self.client.post(f'/admin/activate_vs/{set_id}', follow_redirects=True)

        # 4. Assertions
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        self.assertIn(f'Vector Store Set {set_id} activated successfully.'.encode('utf-8'), response.data)

        # Verify in DB
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM VectorStoreSets WHERE set_id = ?", (set_id,))
        self.assertEqual(cursor.fetchone()[0], 1, "Target set should be active")
        cursor.execute("SELECT is_active FROM VectorStoreSets WHERE set_id = ?", (set_id_other,))
        self.assertEqual(cursor.fetchone()[0], 0, "Other set should be inactive")
        conn.close()

    def test_activate_vs_incomplete_set(self):
        """Test activating a set that is not fully completed."""
        # 1. Add a set with one pending mapping
        set_id = db_manager.add_vector_store_set(TEST_DB_PATH, "activate_incomplete.csv", "Not Ready")
        self.assertIsNotNone(set_id)
        map_id1 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "frFR", "tg_frFR")
        map_id2 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "esLA", "tg_esLA")
        db_manager.update_mapping_status(TEST_DB_PATH, map_id1, 'completed', '/path/to/vs1')
        # map_id2 remains 'pending'

        # 2. Make the POST request
        response = self.client.post(f'/admin/activate_vs/{set_id}', follow_redirects=True)

        # 3. Assertions
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        self.assertIn(f'Cannot activate Set {set_id}: Not all language stores are complete.'.encode('utf-8'), response.data)
        self.assertIn(b'Failed/Pending: [\'esLA\']', response.data) # Check specific language listed

        # Verify in DB (should still be inactive)
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM VectorStoreSets WHERE set_id = ?", (set_id,))
        self.assertEqual(cursor.fetchone()[0], 0, "Incomplete set should remain inactive")
        conn.close()

    def test_activate_vs_failed_set(self):
        """Test activating a set where one mapping failed."""
        # 1. Add a set with one failed mapping
        set_id = db_manager.add_vector_store_set(TEST_DB_PATH, "activate_failed.csv", "Failed")
        self.assertIsNotNone(set_id)
        map_id1 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "frFR", "tg_frFR")
        map_id2 = db_manager.add_vector_store_mapping(TEST_DB_PATH, set_id, "esLA", "tg_esLA")
        db_manager.update_mapping_status(TEST_DB_PATH, map_id1, 'completed', '/path/to/vs1')
        db_manager.update_mapping_status(TEST_DB_PATH, map_id2, 'failed') # Mark as failed

        # 2. Make the POST request
        response = self.client.post(f'/admin/activate_vs/{set_id}', follow_redirects=True)

        # 3. Assertions
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        self.assertIn(f'Cannot activate Set {set_id}: Not all language stores are complete.'.encode('utf-8'), response.data)
        self.assertIn(b'Failed/Pending: [\'esLA\']', response.data) # Check specific language listed

        # Verify in DB (should still be inactive)
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT is_active FROM VectorStoreSets WHERE set_id = ?", (set_id,))
        self.assertEqual(cursor.fetchone()[0], 0, "Failed set should remain inactive")
        conn.close()

    def test_activate_vs_nonexistent_set(self):
        """Test activating a set ID that does not exist."""
        non_existent_id = 999
        # Make the POST request
        response = self.client.post(f'/admin/activate_vs/{non_existent_id}', follow_redirects=True)

        # Assertions
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Admin - Vector Store Management', response.data)
        # Check for the specific message for non-existent/no mappings case
        self.assertIn(f'Cannot activate Set {non_existent_id}: No language mappings found.'.encode('utf-8'), response.data)

# Add more test cases here (missing file, bad extension, header error, db error etc.)

if __name__ == '__main__':
    # Need to import sqlite3 here for setUp/tearDown
    import sqlite3
    unittest.main() 