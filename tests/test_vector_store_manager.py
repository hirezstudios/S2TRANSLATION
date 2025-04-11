import unittest
import os
import sys
import tempfile
import pandas as pd
from unittest.mock import patch, MagicMock, mock_open, ANY
from openai import APIError

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock config before importing the module under test
with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake_api_key'}):
    from src import vector_store_manager
    from src import config # For potentially accessing config constants if needed

# --- Mock OpenAI Objects --- #
# Create mock objects that mimic the structure OpenAI client returns
# We need these because MagicMock doesn't automatically handle attribute access deeply
class MockOpenAIObject:
    def __init__(self, id, status='completed', last_error=None):
        self.id = id
        self.status = status
        self.last_error = last_error

class MockErrorObject:
     def __init__(self, code='server_error', message='Something went wrong'):
          self.code = code
          self.message = message

# --- Test Class --- #
class TestVectorStoreManagerOpenAI(unittest.TestCase):

    def setUp(self):
        # Ensure a clean slate for mocks if necessary
        pass

    def tearDown(self):
        # Clean up any created files if tests failed mid-way (though mocks should prevent this)
        pass

    # --- Test _create_lang_pair_txt --- #
    @patch('pandas.read_csv')
    @patch('tempfile.NamedTemporaryFile', new_callable=mock_open)
    def test_create_lang_pair_txt_success(self, mock_tempfile, mock_read_csv):
        """Test successful creation of temporary language pair TXT file."""
        # Setup mock DataFrame
        mock_df = pd.DataFrame({
            'src_enUS': ['Hello', 'World', '', 'EmptyTarget' ],
            'tg_frFR': ['Bonjour', 'Monde', 'EmptySource', '']
        })
        mock_read_csv.return_value = mock_df
        mock_tempfile.return_value.name = "/tmp/fake_temp_file.txt"

        # Call the function
        result_path = vector_store_manager._create_lang_pair_txt(
            'fake.csv', 'src_enUS', 'tg_frFR', 'frFR'
        )

        # Assertions
        self.assertEqual(result_path, "/tmp/fake_temp_file.txt")
        mock_read_csv.assert_called_once_with('fake.csv', usecols=['src_enUS', 'tg_frFR'], encoding='utf-8')
        mock_tempfile.assert_called_once_with(mode='w', encoding='utf-8', delete=False, suffix='.txt')
        # Check content written (ignoring intermediate spaces/newlines)
        mock_file_handle = mock_tempfile() # Get the file handle mock
        expected_calls = [
            unittest.mock.call('src_enUS: Hello | tg_frFR: Bonjour\n'),
            unittest.mock.call('src_enUS: World | tg_frFR: Monde\n')
        ]
        mock_file_handle.write.assert_has_calls(expected_calls, any_order=False)
        # Ensure rows with empty source or target were filtered out
        self.assertEqual(mock_file_handle.write.call_count, 2)

    @patch('pandas.read_csv')
    def test_create_lang_pair_txt_empty_df(self, mock_read_csv):
        """Test TXT creation when filtered DataFrame is empty."""
        mock_df = pd.DataFrame({'src_enUS': ['', ' '], 'tg_frFR': ['', 'Rien']})
        mock_read_csv.return_value = mock_df
        result_path = vector_store_manager._create_lang_pair_txt('fake.csv', 'src_enUS', 'tg_frFR', 'frFR')
        self.assertIsNone(result_path)

    @patch('pandas.read_csv', side_effect=FileNotFoundError)
    def test_create_lang_pair_txt_file_not_found(self, mock_read_csv):
        """Test TXT creation when source CSV is not found."""
        result_path = vector_store_manager._create_lang_pair_txt('fake.csv', 'src_enUS', 'tg_frFR', 'frFR')
        self.assertIsNone(result_path)
        
    @patch('pandas.read_csv', side_effect=KeyError("tg_frFR"))
    def test_create_lang_pair_txt_key_error(self, mock_read_csv):
        """Test TXT creation when a column is missing."""
        result_path = vector_store_manager._create_lang_pair_txt('fake.csv', 'src_enUS', 'tg_frFR', 'frFR')
        self.assertIsNone(result_path)

    # --- Test _monitor_file_processing --- #
    @patch('openai.OpenAI') # Patch the client init if needed, or assume client is passed
    def test_monitor_success(self, mock_openai_class):
        """Test successful monitoring loop."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        # Simulate retrieve calls: first in_progress, then completed
        mock_client.vector_stores.files.retrieve.side_effect = [
            MockOpenAIObject('file_abc', status='in_progress'),
            MockOpenAIObject('file_abc', status='completed')
        ]
        result = vector_store_manager._monitor_file_processing(mock_client, 'vs_xyz', 'file_abc', timeout=30)
        self.assertTrue(result)
        self.assertEqual(mock_client.vector_stores.files.retrieve.call_count, 2)
        mock_client.vector_stores.files.retrieve.assert_called_with(vector_store_id='vs_xyz', file_id='file_abc')

    @patch('openai.OpenAI')
    def test_monitor_failed(self, mock_openai_class):
        """Test monitoring when processing fails."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        mock_client.vector_stores.files.retrieve.side_effect = [
            MockOpenAIObject('file_abc', status='in_progress'),
            MockOpenAIObject('file_abc', status='failed', last_error=MockErrorObject(code='bad_file', message='Invalid format'))
        ]
        result = vector_store_manager._monitor_file_processing(mock_client, 'vs_xyz', 'file_abc', timeout=30)
        self.assertFalse(result)
        self.assertEqual(mock_client.vector_stores.files.retrieve.call_count, 2)

    @patch('openai.OpenAI')
    @patch('time.sleep', return_value=None) # Speed up test by mocking sleep
    def test_monitor_timeout(self, mock_sleep, mock_openai_class):
        """Test monitoring when processing times out."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        # Always return in_progress
        mock_client.vector_stores.files.retrieve.return_value = MockOpenAIObject('file_abc', status='in_progress')
        result = vector_store_manager._monitor_file_processing(mock_client, 'vs_xyz', 'file_abc', timeout=5) # Short timeout
        self.assertIsNone(result)
        # Check retrieve was called multiple times (depends on mocked sleep and timeout)
        self.assertGreater(mock_client.vector_stores.files.retrieve.call_count, 0)

    # --- Test create_openai_vector_store_for_language (Main Function) --- #

    @patch('src.vector_store_manager._create_lang_pair_txt')
    @patch('src.vector_store_manager.OpenAI')
    @patch('src.vector_store_manager._monitor_file_processing')
    @patch('src.vector_store_manager.os.remove') # Correct patch target
    @patch('src.vector_store_manager.os.path.exists', return_value=True) # Add patch for exists
    @patch('builtins.open', new_callable=mock_open, read_data=b'data') # Mock file open for upload
    def test_create_openai_vs_success(self, mock_file_open, mock_os_exists, mock_os_remove, mock_monitor, mock_openai_class, mock_create_txt):
        """Test the successful end-to-end creation of an OpenAI Vector Store."""
        # Arrange Mocks
        mock_create_txt.return_value = '/tmp/fake_temp_file.txt'
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        mock_monitor.return_value = True # Simulate successful processing
        
        # Mock OpenAI API return values
        mock_client.files.create.return_value = MockOpenAIObject('file_success123')
        mock_client.vector_stores.create.return_value = MockOpenAIObject('vs_success456')
        # files.create within vector_stores doesn't return much, mock it simply
        mock_client.vector_stores.files.create.return_value = MagicMock()

        # Act
        result = vector_store_manager.create_openai_vector_store_for_language(
            csv_path='real.csv', 
            source_column='src_enUS', 
            target_column='tg_frFR', 
            language_code='frFR', 
            set_id=1, 
            mapping_id=101
        )

        # Assert Results
        self.assertEqual(result, ('vs_success456', 'file_success123'))

        # Assert Mock Calls
        mock_create_txt.assert_called_once_with('real.csv', 'src_enUS', 'tg_frFR', 'frFR')
        mock_file_open.assert_called_once_with('/tmp/fake_temp_file.txt', 'rb') # Check file upload opens correctly
        mock_client.files.create.assert_called_once_with(file=ANY, purpose='assistants')
        mock_client.vector_stores.create.assert_called_once_with(name='TranslationAssist Set 1 - frFR')
        mock_client.vector_stores.files.create.assert_called_once_with(vector_store_id='vs_success456', file_id='file_success123')
        mock_monitor.assert_called_once_with(mock_client, 'vs_success456', 'file_success123')
        mock_os_exists.assert_called_once_with('/tmp/fake_temp_file.txt') # Check exists call
        mock_os_remove.assert_called_once_with('/tmp/fake_temp_file.txt') # Check temp file cleanup
        # Ensure cleanup functions for OpenAI resources were NOT called on success
        mock_client.vector_stores.delete.assert_not_called()
        mock_client.files.delete.assert_not_called()

    @patch('src.vector_store_manager._create_lang_pair_txt', return_value=None)
    @patch('src.vector_store_manager.OpenAI')
    @patch('src.vector_store_manager.os.remove') # Correct patch target
    @patch('src.vector_store_manager.os.path.exists') # Mock exists, but don't force True here
    def test_create_openai_vs_txt_fail(self, mock_os_exists, mock_os_remove, mock_openai_class, mock_create_txt):
        """Test failure during temporary TXT file creation."""
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        
        result = vector_store_manager.create_openai_vector_store_for_language(
            'real.csv', 'src', 'tgt', 'lang', 1, 101
        )
        
        self.assertIsNone(result)
        mock_openai_class.assert_called_once() # Client might init
        mock_client.files.create.assert_not_called()
        mock_client.vector_stores.create.assert_not_called()
        mock_os_exists.assert_not_called() # Should not be called as temp_txt_path is None
        mock_os_remove.assert_not_called() # No temp file path to remove

    @patch('src.vector_store_manager._create_lang_pair_txt')
    @patch('src.vector_store_manager.OpenAI')
    @patch('src.vector_store_manager.os.remove') # Correct patch target
    @patch('src.vector_store_manager.os.path.exists', return_value=True) # Add patch for exists
    @patch('builtins.open', new_callable=mock_open, read_data=b'data')
    def test_create_openai_vs_upload_fail(self, mock_file_open, mock_os_exists, mock_os_remove, mock_openai_class, mock_create_txt):
        """Test failure during OpenAI file upload."""
        mock_create_txt.return_value = '/tmp/upload_fail.txt'
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        mock_client.files.create.side_effect = APIError("Upload failed", request=None, body=None)

        result = vector_store_manager.create_openai_vector_store_for_language(
            'real.csv', 'src', 'tgt', 'lang', 1, 101
        )

        self.assertIsNone(result)
        mock_client.files.create.assert_called_once()
        mock_client.vector_stores.create.assert_not_called() # Should fail before this
        mock_client.vector_stores.delete.assert_not_called() # No VS created
        mock_client.files.delete.assert_not_called() # No file ID to delete if create failed
        mock_os_exists.assert_called_once_with('/tmp/upload_fail.txt') # Should check existence
        mock_os_remove.assert_called_once_with('/tmp/upload_fail.txt') # Temp file should still be cleaned
        
    @patch('src.vector_store_manager._create_lang_pair_txt')
    @patch('src.vector_store_manager.OpenAI')
    @patch('src.vector_store_manager.os.remove') # Correct patch target
    @patch('src.vector_store_manager.os.path.exists', return_value=True) # Add patch for exists
    @patch('builtins.open', new_callable=mock_open, read_data=b'data')
    def test_create_openai_vs_create_vs_fail(self, mock_file_open, mock_os_exists, mock_os_remove, mock_openai_class, mock_create_txt):
        """Test failure during OpenAI Vector Store creation."""
        mock_create_txt.return_value = '/tmp/vs_create_fail.txt'
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        mock_client.files.create.return_value = MockOpenAIObject('file_temp123') # Upload succeeds
        mock_client.vector_stores.create.side_effect = APIError("VS creation failed", request=None, body=None)

        result = vector_store_manager.create_openai_vector_store_for_language(
            'real.csv', 'src', 'tgt', 'lang', 1, 101
        )

        self.assertIsNone(result)
        mock_client.files.create.assert_called_once()
        mock_client.vector_stores.create.assert_called_once()
        mock_client.vector_stores.files.create.assert_not_called() # Should fail before attaching file
        # Check cleanup
        mock_client.vector_stores.delete.assert_not_called() # No VS ID to delete
        mock_client.files.delete.assert_called_once_with('file_temp123') # Uploaded file should be deleted
        mock_os_exists.assert_called_once_with('/tmp/vs_create_fail.txt')
        mock_os_remove.assert_called_once_with('/tmp/vs_create_fail.txt')

    @patch('src.vector_store_manager._create_lang_pair_txt')
    @patch('src.vector_store_manager.OpenAI')
    @patch('src.vector_store_manager.os.remove') # Correct patch target
    @patch('src.vector_store_manager.os.path.exists', return_value=True) # Add patch for exists
    @patch('builtins.open', new_callable=mock_open, read_data=b'data')
    def test_create_openai_vs_attach_fail(self, mock_file_open, mock_os_exists, mock_os_remove, mock_openai_class, mock_create_txt):
        """Test failure when attaching OpenAI file to Vector Store."""
        mock_create_txt.return_value = '/tmp/attach_fail.txt'
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        mock_client.files.create.return_value = MockOpenAIObject('file_attach123')
        mock_client.vector_stores.create.return_value = MockOpenAIObject('vs_attach456')
        mock_client.vector_stores.files.create.side_effect = APIError("Attach failed", request=None, body=None)

        result = vector_store_manager.create_openai_vector_store_for_language(
            'real.csv', 'src', 'tgt', 'lang', 1, 101
        )

        self.assertIsNone(result)
        mock_client.files.create.assert_called_once()
        mock_client.vector_stores.create.assert_called_once()
        mock_client.vector_stores.files.create.assert_called_once() # This call failed
        # Check cleanup
        mock_client.vector_stores.delete.assert_called_once_with('vs_attach456') # VS should be deleted
        mock_client.files.delete.assert_called_once_with('file_attach123') # File should be deleted
        mock_os_exists.assert_called_once_with('/tmp/attach_fail.txt')
        mock_os_remove.assert_called_once_with('/tmp/attach_fail.txt')

    @patch('src.vector_store_manager._create_lang_pair_txt')
    @patch('src.vector_store_manager.OpenAI')
    @patch('src.vector_store_manager._monitor_file_processing', return_value=False) # Monitor returns failure
    @patch('src.vector_store_manager.os.remove') # Correct patch target
    @patch('src.vector_store_manager.os.path.exists', return_value=True) # Add patch for exists
    @patch('builtins.open', new_callable=mock_open, read_data=b'data')
    def test_create_openai_vs_monitor_fail(self, mock_file_open, mock_os_exists, mock_os_remove, mock_monitor, mock_openai_class, mock_create_txt):
        """Test failure during OpenAI file processing monitoring."""
        mock_create_txt.return_value = '/tmp/monitor_fail.txt'
        mock_client = MagicMock()
        mock_openai_class.return_value = mock_client
        mock_client.files.create.return_value = MockOpenAIObject('file_monitor123')
        mock_client.vector_stores.create.return_value = MockOpenAIObject('vs_monitor456')
        mock_client.vector_stores.files.create.return_value = MagicMock()

        result = vector_store_manager.create_openai_vector_store_for_language(
            'real.csv', 'src', 'tgt', 'lang', 1, 101
        )

        self.assertIsNone(result)
        mock_monitor.assert_called_once_with(mock_client, 'vs_monitor456', 'file_monitor123')
        # Check cleanup
        mock_client.vector_stores.delete.assert_called_once_with('vs_monitor456') # VS should be deleted
        mock_client.files.delete.assert_called_once_with('file_monitor123') # File should be deleted
        mock_os_exists.assert_called_once_with('/tmp/monitor_fail.txt')
        mock_os_remove.assert_called_once_with('/tmp/monitor_fail.txt')


if __name__ == '__main__':
    unittest.main() 