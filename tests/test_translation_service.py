# tests/test_translation_service.py
import pytest
from unittest.mock import MagicMock, patch, ANY
import openai # Import openai for exception types
import unittest
from unittest.mock import call
import json
import os

# Assuming your service file can be imported like this:
# Adjust the import path based on your project structure
from src import translation_service
from src import config
# Ensure api_clients can be imported and mocked correctly
# If get_openai_client is directly in api_clients:
# from src.api_clients import get_openai_client 
# If it's structured differently, adjust the import path
# For now, assume it's accessible via api_clients module itself for patching
from src import api_clients 

# Mock config before importing the module under test
from unittest.mock import Mock
sys_modules_mock = {
    'src.config': Mock(DATABASE_FILE=':memory:',
                        MAX_WORKER_THREADS=1,
                        API_MAX_RETRIES=0,
                        API_INITIAL_RETRY_DELAY=0.1,
                        API_MAX_RETRY_DELAY=0.5,
                        TARGET_COLUMN_TPL='tg_{lang_code}',
                        LANGUAGE_NAME_MAP={'esLA': 'Spanish', 'frFR': 'French'},
                        OPENAI_MODEL='test-oai-model',
                        DEFAULT_API='OPENAI'), # Set default to OpenAI for easier testing
    'src.api_clients': Mock()
}

with patch.dict(sys.modules, sys_modules_mock):
    from src import db_manager
    from src import prompt_manager

# --- Mocking Setup ---

# Mock the config values needed (replace with actual defaults if different)
@pytest.fixture(autouse=True)
def mock_config(mocker):
    # Using setattr to patch values directly onto the imported config module
    mocker.patch.object(config, 'API_MAX_RETRIES', 1) # Reduce retries for tests
    mocker.patch.object(config, 'API_INITIAL_RETRY_DELAY', 0.1)
    mocker.patch.object(config, 'API_MAX_RETRY_DELAY', 0.2)
    mocker.patch.object(config, 'OPENAI_MODEL', 'gpt-4o-mini-test') # Default test model

# Mock the OpenAI client fetching
@pytest.fixture
def mock_openai_client():
    client = MagicMock(spec=openai.OpenAI)
    # Mock the responses.create structure
    client.responses = MagicMock()
    client.responses.create = MagicMock()
    return client

@pytest.fixture(autouse=True)
def patch_get_openai_client(mocker, mock_openai_client):
    # Patch the function that returns the client instance
    # Adjust the patching target if get_openai_client is imported differently
    mocker.patch('src.api_clients.get_openai_client', return_value=mock_openai_client)

# Mock print_lock if necessary (if tests run in parallel or cause issues)
@pytest.fixture(autouse=True)
def mock_print_lock(mocker):
    mocker.patch('src.translation_service.print_lock', MagicMock())

# --- Tests for call_openai_api (Responses API Version) ---

def test_call_openai_api_success(mock_openai_client):
    """Test successful call and response parsing with Responses API"""
    # Mock the successful response structure
    mock_response = MagicMock()
    mock_output_text = MagicMock()
    mock_output_text.type = 'output_text'
    mock_output_text.text = '  Test Translation  ' # Include whitespace for strip test
    mock_content_block = MagicMock()
    mock_content_block.type = 'message'
    mock_content_block.role = 'assistant'
    mock_content_block.content = [mock_output_text]
    mock_response.output = [mock_content_block]
    mock_openai_client.responses.create.return_value = mock_response

    system_instructions = "Translate to French."
    source_text = "Hello world"
    model = "gpt-4o-mini-test"

    result = translation_service.call_openai_api(
        mock_openai_client, system_instructions, source_text, "test-1", model
    )

    assert result == "Test Translation"
    mock_openai_client.responses.create.assert_called_once()
    call_args = mock_openai_client.responses.create.call_args[1] # Get keyword args
    assert call_args['model'] == model
    # Check that the input prompt was constructed correctly (basic check)
    assert system_instructions in call_args['input']
    assert source_text in call_args['input']
    assert "Output ONLY the final translated string" in call_args['input']

def test_call_openai_api_empty_response(mock_openai_client):
    """Test handling when API returns an empty or non-text response"""
    mock_response = MagicMock()
    mock_output_other = MagicMock()
    mock_output_other.type = 'other_type' # Not output_text
    mock_content_block = MagicMock()
    mock_content_block.type = 'message'
    mock_content_block.role = 'assistant'
    mock_content_block.content = [mock_output_other]
    mock_response.output = [mock_content_block]
    mock_openai_client.responses.create.return_value = mock_response

    result = translation_service.call_openai_api(
        mock_openai_client, "Translate", "Source", "test-2", "gpt-4o-mini-test"
    )
    assert result == "" # Should return empty string, not None

def test_call_openai_api_no_output(mock_openai_client):
    """Test handling when API response has no output field"""
    mock_response = MagicMock()
    mock_response.output = None # Simulate missing output
    mock_openai_client.responses.create.return_value = mock_response

    result = translation_service.call_openai_api(
        mock_openai_client, "Translate", "Source", "test-3", "gpt-4o-mini-test"
    )
    assert result == "" # Should return empty string

def test_call_openai_api_model_does_not_support(mock_openai_client):
    """Test specific error handling for model incompatibility"""
    # Simulate the APIStatusError for unsupported model
    mock_error_body = {"message": "The model `gpt-3.5-turbo` does not support the Responses API."}
    # Note: Adjust exception type and structure if OpenAI library raises differently for Responses API
    mock_api_error = openai.APIStatusError("Bad Request", response=MagicMock(status_code=400), body=mock_error_body)
    mock_openai_client.responses.create.side_effect = mock_api_error

    result = translation_service.call_openai_api(
        mock_openai_client, "Translate", "Source", "test-4", "gpt-3.5-turbo"
    )
    assert result == "ERROR:API:Model does not support Responses API"

def test_call_openai_api_rate_limit(mock_openai_client):
    """Test retry logic on RateLimitError"""
    # Simulate RateLimitError on first call, success on second
    mock_response_success = MagicMock()
    mock_output_text = MagicMock()
    mock_output_text.type = 'output_text'
    mock_output_text.text = 'Success After Retry'
    mock_content_block = MagicMock()
    mock_content_block.type = 'message'
    mock_content_block.role = 'assistant'
    mock_content_block.content = [mock_output_text]
    mock_response_success.output = [mock_content_block]

    mock_openai_client.responses.create.side_effect = [
        openai.RateLimitError("Rate limit exceeded", response=MagicMock(), body=None),
        mock_response_success
    ]

    result = translation_service.call_openai_api(
        mock_openai_client, "Translate", "Source", "test-5", "gpt-4o-mini-test"
    )
    assert result == "Success After Retry"
    assert mock_openai_client.responses.create.call_count == 2 # Ensure it retried

def test_call_openai_api_max_retries_fail(mock_openai_client):
    """Test failure after max retries"""
    # Simulate timeout error repeatedly
    mock_openai_client.responses.create.side_effect = openai.APITimeoutError("Timeout")

    result = translation_service.call_openai_api(
        mock_openai_client, "Translate", "Source", "test-6", "gpt-4o-mini-test"
    )
    assert result is None # Should return None after max retries (configured to 1 retry in fixture)
    # Initial call + 1 retry = 2 calls
    assert mock_openai_client.responses.create.call_count == config.API_MAX_RETRIES + 1 

# --- Tests for call_active_api (Focus on OpenAI path) ---

# We need to mock the call_openai_api function itself now
@patch('src.translation_service.call_openai_api')
def test_call_active_api_openai_path(mock_call_openai, mock_openai_client):
    """Verify call_active_api passes correct args to call_openai_api"""
    mock_call_openai.return_value = "Mocked Translation" # Simulate success

    target_api = "OPENAI"
    system_prompt = "System Rules"
    user_content = "User Text"
    model_override = "gpt-4o-override"

    result = translation_service.call_active_api(
        target_api, system_prompt, user_content, "active-test-1", model_override
    )

    assert result == "Mocked Translation"
    mock_call_openai.assert_called_once_with(
        openai_client_obj=mock_openai_client,
        system_instructions=system_prompt,
        source_text=user_content,
        row_identifier="active-test-1",
        model_to_use=model_override # Should use override
    )

@patch('src.translation_service.call_openai_api')
def test_call_active_api_openai_path_default_model(mock_call_openai, mock_openai_client, mock_config):
    """Verify call_active_api uses default model when override is None"""
    mock_call_openai.return_value = "Mocked Translation"

    target_api = "OPENAI"
    system_prompt = "System Rules"
    user_content = "User Text"
    model_override = None # No override

    result = translation_service.call_active_api(
        target_api, system_prompt, user_content, "active-test-2", model_override
    )

    assert result == "Mocked Translation"
    mock_call_openai.assert_called_once_with(
        openai_client_obj=mock_openai_client,
        system_instructions=system_prompt,
        source_text=user_content,
        row_identifier="active-test-2",
        model_to_use=config.OPENAI_MODEL # Should use default from mock_config
    )

@patch('src.translation_service.call_openai_api')
def test_call_active_api_openai_no_model_config(mock_call_openai, mock_openai_client, mocker):
    """Verify error return if override and default model are missing"""
    # Patch config.OPENAI_MODEL to be None for this test
    mocker.patch.object(config, 'OPENAI_MODEL', None)
    mock_call_openai.return_value = "Should not be called"

    target_api = "OPENAI"
    system_prompt = "System Rules"
    user_content = "User Text"
    model_override = None # No override

    result = translation_service.call_active_api(
        target_api, system_prompt, user_content, "active-test-3", model_override
    )

    assert result == "ERROR:CONFIG:OpenAI Model Missing"
    mock_call_openai.assert_not_called() # Function should exit before calling

# (Add similar tests for PERPLEXITY and GEMINI paths in call_active_api if desired) 

# --- Test Class --- #

class TestTranslationServiceVectorStore(unittest.TestCase):

    def setUp(self):
        """Set up common test data and mocks."""
        self.db_path = ":memory:"
        self.test_batch_id = "test-batch-vs"
        self.test_task_id = 1
        self.lang_es = "esLA"
        self.lang_fr = "frFR"
        self.vs_id_es = "vs_spanish123"
        self.vs_id_fr = "vs_french456"
        self.active_vs_map = {self.lang_es: self.vs_id_es, self.lang_fr: self.vs_id_fr}

        self.task_data_es = {
            'task_id': self.test_task_id,
            'batch_id': self.test_batch_id,
            'row_index_in_file': 0,
            'language_code': self.lang_es,
            'source_text': "Hello world",
            'metadata_json': json.dumps({"Record ID": "REC001"})
        }
        self.task_data_fr = { # Task for a different language
            'task_id': self.test_task_id + 1,
            'batch_id': self.test_batch_id,
            'row_index_in_file': 1,
            'language_code': self.lang_fr,
            'source_text': "Goodbye world",
            'metadata_json': json.dumps({"Record ID": "REC002"})
        }

        # Basic worker config - modify per test case
        self.base_worker_config = {
            'db_path': self.db_path,
            'translation_mode': 'ONE_STAGE', # Default to ONE_STAGE
            'default_api': 'OPENAI',
            'stage1_api': 'OPENAI',
            'stage2_api': 'OPENAI',
            'stage3_api': 'OPENAI',
            'stage1_model': 'test-s1-model',
            'stage2_model': 'test-s2-model',
            'stage3_model': 'test-s3-model',
            'target_column_tpl': 'tg_{lang_code}',
            'openai_client': MagicMock(),
            'use_vs': False # Default VS to False
        }

        # Mock dependencies used by translate_row_worker
        self.mock_db_update_status = patch('src.db_manager.update_task_status').start()
        self.mock_db_update_results = patch('src.db_manager.update_task_results').start()
        self.mock_db_get_active_map = patch('src.db_manager.get_active_vector_store_map').start()
        self.mock_log_audit = patch('src.translation_service.log_audit_record').start()
        self.mock_get_full_prompt = patch('src.prompt_manager.get_full_prompt').start()
        self.mock_parse_evaluation = patch('src.prompt_manager.parse_evaluation').start()
        # Mock templates access if needed, assuming they are loaded
        patch.dict(prompt_manager.stage1_templates, {self.lang_es: "Spanish Rules <<TARGET_LANGUAGE_NAME>>", self.lang_fr: "French Rules <<TARGET_LANGUAGE_NAME>>"}).start()
        patch.object(prompt_manager, 'global_rules_content', "Global Rules").start()

        # Mock the central API calling function
        self.mock_call_active_api = patch('src.translation_service.call_active_api').start()

        # Ensure mocks are stopped after tests
        self.addCleanup(patch.stopall)

    def test_vs_one_stage_openai_enabled(self):
        """Test ONE_STAGE with OpenAI API and Vector Store enabled."""
        worker_config = {**self.base_worker_config, 'use_vs': True, 'translation_mode': 'ONE_STAGE', 'default_api': 'OPENAI'}
        self.mock_db_get_active_map.return_value = self.active_vs_map
        self.mock_call_active_api.return_value = "Hola mundo VS"

        result = translation_service.translate_row_worker(self.task_data_es, worker_config)

        self.assertTrue(result)
        self.mock_db_get_active_map.assert_called_once_with(self.db_path)
        # Verify call_active_api was called with the correct vs_id for Spanish
        self.mock_call_active_api.assert_called_once()
        call_args, call_kwargs = self.mock_call_active_api.call_args
        self.assertEqual(call_args[0], 'OPENAI') # api_to_use
        self.assertTrue(call_args[1].startswith("Spanish Rules Spanish")) # system_prompt
        self.assertEqual(call_args[2], "Hello world") # source_text
        self.assertEqual(call_kwargs.get('openai_vs_id'), self.vs_id_es)
        self.mock_db_update_results.assert_called_with(
            self.db_path, self.test_task_id, 'completed', initial_tx=None, score=None, 
            feedback=None, final_tx="Hola mundo VS", approved_tx="Hola mundo VS", 
            review_sts='approved_original', error_msg=None
        )
        # Check audit log includes vs_used: True
        self.mock_log_audit.assert_called_once()
        audit_call_args, _ = self.mock_log_audit.call_args
        audit_record = audit_call_args[0]
        self.assertTrue(audit_record.get('vs_used'))

    def test_vs_one_stage_openai_disabled(self):
        """Test ONE_STAGE with OpenAI API but Vector Store disabled."""
        worker_config = {**self.base_worker_config, 'use_vs': False, 'translation_mode': 'ONE_STAGE', 'default_api': 'OPENAI'}
        # No need to mock get_active_map as it shouldn't be called
        self.mock_call_active_api.return_value = "Hola mundo no VS"

        result = translation_service.translate_row_worker(self.task_data_es, worker_config)

        self.assertTrue(result)
        self.mock_db_get_active_map.assert_not_called()
        # Verify call_active_api was called *without* vs_id
        self.mock_call_active_api.assert_called_once()
        call_args, call_kwargs = self.mock_call_active_api.call_args
        self.assertEqual(call_args[0], 'OPENAI')
        self.assertEqual(call_kwargs.get('openai_vs_id'), None)
        self.mock_db_update_results.assert_called_with(
            self.db_path, self.test_task_id, 'completed', initial_tx=None, score=None, 
            feedback=None, final_tx="Hola mundo no VS", approved_tx="Hola mundo no VS", 
            review_sts='approved_original', error_msg=None
        )
        # Check audit log includes vs_used: False
        self.mock_log_audit.assert_called_once()
        audit_call_args, _ = self.mock_log_audit.call_args
        audit_record = audit_call_args[0]
        self.assertFalse(audit_record.get('vs_used'))

    def test_vs_one_stage_non_openai_enabled(self):
        """Test ONE_STAGE with non-OpenAI API and Vector Store enabled (should not use VS)."""
        worker_config = {**self.base_worker_config, 'use_vs': True, 'translation_mode': 'ONE_STAGE', 'default_api': 'GEMINI'}
        self.mock_db_get_active_map.return_value = self.active_vs_map # Map exists
        self.mock_call_active_api.return_value = "Hola mundo Gemini"

        result = translation_service.translate_row_worker(self.task_data_es, worker_config)

        self.assertTrue(result)
        # Should still check map, but not pass VS ID to non-OpenAI call
        self.mock_db_get_active_map.assert_called_once_with(self.db_path)
        # Verify call_active_api was called *without* vs_id
        self.mock_call_active_api.assert_called_once()
        call_args, call_kwargs = self.mock_call_active_api.call_args
        self.assertEqual(call_args[0], 'GEMINI')
        self.assertEqual(call_kwargs.get('openai_vs_id'), None)
        self.mock_db_update_results.assert_called_with(
            self.db_path, self.test_task_id, 'completed', initial_tx=None, score=None, 
            feedback=None, final_tx="Hola mundo Gemini", approved_tx="Hola mundo Gemini", 
            review_sts='approved_original', error_msg=None
        )
        # Check audit log includes vs_used: False (because API wasn't OpenAI)
        self.mock_log_audit.assert_called_once()
        audit_call_args, _ = self.mock_log_audit.call_args
        audit_record = audit_call_args[0]
        self.assertFalse(audit_record.get('vs_used'))

    def test_vs_three_stage_openai_enabled_s1_s3(self):
        """Test THREE_STAGE with OpenAI API and VS enabled (used in S1 and S3)."""
        worker_config = {**self.base_worker_config, 'use_vs': True, 'translation_mode': 'THREE_STAGE',
                         'stage1_api': 'OPENAI', 'stage2_api': 'OPENAI', 'stage3_api': 'OPENAI'}
        self.mock_db_get_active_map.return_value = self.active_vs_map
        # Mock return values for each stage
        self.mock_call_active_api.side_effect = ["Initial Bonjour VS", "[{"score": 8, "feedback": "Good"}]", "Final Bonjour VS S3"]
        self.mock_get_full_prompt.side_effect = ["Stage 2 Prompt", "Stage 3 Prompt"] # Mock prompt generation
        self.mock_parse_evaluation.return_value = (8, "Good")

        result = translation_service.translate_row_worker(self.task_data_fr, worker_config)

        self.assertTrue(result)
        self.mock_db_get_active_map.assert_called_once_with(self.db_path)
        
        # Check calls to call_active_api
        self.assertEqual(self.mock_call_active_api.call_count, 3)
        calls = self.mock_call_active_api.call_args_list
        
        # Stage 1 Call (French)
        s1_call_args, s1_call_kwargs = calls[0]
        self.assertEqual(s1_call_args[0], 'OPENAI')
        self.assertTrue(s1_call_args[1].startswith("French Rules French"))
        self.assertEqual(s1_call_args[2], "Goodbye world")
        self.assertEqual(s1_call_kwargs.get('openai_vs_id'), self.vs_id_fr)

        # Stage 2 Call (VS ID should be None)
        s2_call_args, s2_call_kwargs = calls[1]
        self.assertEqual(s2_call_args[0], 'OPENAI')
        self.assertEqual(s2_call_args[1], "Stage 2 Prompt") # Check prompt used
        self.assertEqual(s2_call_kwargs.get('openai_vs_id'), None)

        # Stage 3 Call (French)
        s3_call_args, s3_call_kwargs = calls[2]
        self.assertEqual(s3_call_args[0], 'OPENAI')
        self.assertEqual(s3_call_args[1], "Stage 3 Prompt") # Check prompt used
        self.assertEqual(s3_call_kwargs.get('openai_vs_id'), self.vs_id_fr)

        # Check final DB update
        self.mock_db_update_results.assert_called_with(
             self.db_path, self.task_data_fr['task_id'], 'completed', 
             initial_tx="Initial Bonjour VS", score=8, feedback="Good", 
             final_tx="Final Bonjour VS S3", approved_tx="Final Bonjour VS S3", 
             review_sts='approved_original', error_msg=None
         )
        # Check audit log includes vs_used: True
        self.mock_log_audit.assert_called_once()
        audit_call_args, _ = self.mock_log_audit.call_args
        audit_record = audit_call_args[0]
        self.assertTrue(audit_record.get('vs_used'))

    def test_vs_three_stage_openai_enabled_s1_only(self):
        """Test THREE_STAGE with VS enabled, but S3 API is not OpenAI."""
        worker_config = {**self.base_worker_config, 'use_vs': True, 'translation_mode': 'THREE_STAGE',
                         'stage1_api': 'OPENAI', 'stage2_api': 'GEMINI', 'stage3_api': 'GEMINI'}
        self.mock_db_get_active_map.return_value = self.active_vs_map
        self.mock_call_active_api.side_effect = ["Initial Bonjour VS", "[{"score": 7, "feedback": "OK"}]", "Final Bonjour Gemini S3"]
        self.mock_get_full_prompt.side_effect = ["Stage 2 Prompt G", "Stage 3 Prompt G"]
        self.mock_parse_evaluation.return_value = (7, "OK")

        result = translation_service.translate_row_worker(self.task_data_fr, worker_config)
        self.assertTrue(result)
        self.mock_db_get_active_map.assert_called_once_with(self.db_path)
        self.assertEqual(self.mock_call_active_api.call_count, 3)
        calls = self.mock_call_active_api.call_args_list
        
        # S1 should have VS ID
        self.assertEqual(calls[0][1].get('openai_vs_id'), self.vs_id_fr)
        # S2 (Gemini) should not
        self.assertEqual(calls[1][1].get('openai_vs_id'), None)
        # S3 (Gemini) should not
        self.assertEqual(calls[2][1].get('openai_vs_id'), None)
        
        # Check audit log includes vs_used: True (because S1 used it)
        self.mock_log_audit.assert_called_once()
        audit_call_args, _ = self.mock_log_audit.call_args
        audit_record = audit_call_args[0]
        self.assertTrue(audit_record.get('vs_used'))

    def test_vs_enabled_but_map_fetch_fails(self):
        """Test VS enabled, but fetching the active map fails."""
        worker_config = {**self.base_worker_config, 'use_vs': True, 'translation_mode': 'ONE_STAGE', 'default_api': 'OPENAI'}
        self.mock_db_get_active_map.side_effect = Exception("DB Connection Error")
        self.mock_call_active_api.return_value = "Hola mundo No VS Fallback"

        result = translation_service.translate_row_worker(self.task_data_es, worker_config)
        self.assertTrue(result) # Should still complete, just without VS
        self.mock_db_get_active_map.assert_called_once_with(self.db_path)
        # Verify call_active_api was called *without* vs_id
        self.mock_call_active_api.assert_called_once()
        call_args, call_kwargs = self.mock_call_active_api.call_args
        self.assertEqual(call_args[0], 'OPENAI')
        self.assertEqual(call_kwargs.get('openai_vs_id'), None)
        # Check audit log includes vs_used: False
        self.mock_log_audit.assert_called_once()
        audit_call_args, _ = self.mock_log_audit.call_args
        audit_record = audit_call_args[0]
        self.assertFalse(audit_record.get('vs_used'))

    def test_vs_enabled_but_no_active_map(self):
        """Test VS enabled, but no active set found in DB."""
        worker_config = {**self.base_worker_config, 'use_vs': True, 'translation_mode': 'ONE_STAGE', 'default_api': 'OPENAI'}
        self.mock_db_get_active_map.return_value = None # Simulate no active set
        self.mock_call_active_api.return_value = "Hola mundo No VS Fallback 2"

        result = translation_service.translate_row_worker(self.task_data_es, worker_config)
        self.assertTrue(result)
        self.mock_db_get_active_map.assert_called_once_with(self.db_path)
        self.mock_call_active_api.assert_called_once()
        call_args, call_kwargs = self.mock_call_active_api.call_args
        self.assertEqual(call_kwargs.get('openai_vs_id'), None)
        self.assertFalse(self.mock_log_audit.call_args[0][0].get('vs_used'))

    def test_vs_enabled_but_language_not_in_map(self):
        """Test VS enabled, active map found, but current language missing."""
        worker_config = {**self.base_worker_config, 'use_vs': True, 'translation_mode': 'ONE_STAGE', 'default_api': 'OPENAI'}
        active_map_missing_es = {self.lang_fr: self.vs_id_fr} # Map exists, but not for esLA
        self.mock_db_get_active_map.return_value = active_map_missing_es
        self.mock_call_active_api.return_value = "Hola mundo No VS Fallback 3"

        result = translation_service.translate_row_worker(self.task_data_es, worker_config)
        self.assertTrue(result)
        self.mock_db_get_active_map.assert_called_once_with(self.db_path)
        self.mock_call_active_api.assert_called_once()
        call_args, call_kwargs = self.mock_call_active_api.call_args
        self.assertEqual(call_kwargs.get('openai_vs_id'), None)
        self.assertFalse(self.mock_log_audit.call_args[0][0].get('vs_used'))

# --- Test call_active_api passes VS ID --- #
# (We need to unpatch call_active_api for these tests)
class TestCallActiveApiVsPassing(unittest.TestCase):

    @patch('src.translation_service.call_openai_api')
    @patch('src.translation_service.call_perplexity_api')
    @patch('src.translation_service.call_gemini_api')
    @patch('src.api_clients.get_openai_client')
    def test_call_active_api_passes_vs_id_to_openai(self, mock_get_oai_client, mock_call_gemini, mock_call_pplx, mock_call_oai):
        """Verify call_active_api passes openai_vs_id ONLY to call_openai_api."""
        mock_oai_client = MagicMock()
        mock_get_oai_client.return_value = mock_oai_client
        test_vs_id = "vs_test123"
        system_prompt = "sys prompt"
        user_content = "user content"
        row_id = "R1"
        model = "gpt-4o"

        with patch.dict(sys.modules['src.config'].__dict__, {'OPENAI_MODEL': model}):
            # Call targeting OpenAI WITH vs_id
            translation_service.call_active_api("OPENAI", system_prompt, user_content, row_id, model_override=model, openai_vs_id=test_vs_id)
            mock_call_oai.assert_called_once_with(
                openai_client_obj=mock_oai_client,
                system_instructions=system_prompt,
                source_text=user_content,
                row_identifier=row_id,
                model_to_use=model,
                openai_vs_id=test_vs_id # Check VS ID is passed
            )
            mock_call_oai.reset_mock()

            # Call targeting OpenAI WITHOUT vs_id
            translation_service.call_active_api("OPENAI", system_prompt, user_content, row_id, model_override=model, openai_vs_id=None)
            mock_call_oai.assert_called_once_with(
                openai_client_obj=mock_oai_client,
                system_instructions=system_prompt,
                source_text=user_content,
                row_identifier=row_id,
                model_to_use=model,
                openai_vs_id=None # Check VS ID is None
            )
            mock_call_oai.reset_mock()

            # Call targeting Perplexity WITH vs_id (should be ignored)
            translation_service.call_active_api("PERPLEXITY", system_prompt, user_content, row_id, model_override="pplx-model", openai_vs_id=test_vs_id)
            mock_call_pplx.assert_called_once_with(system_prompt, user_content, row_id, "pplx-model")
            mock_call_oai.assert_not_called()
            mock_call_gemini.assert_not_called()
            mock_call_pplx.reset_mock()
            
            # Call targeting Gemini WITH vs_id (should be ignored)
            translation_service.call_active_api("GEMINI", system_prompt, user_content, row_id, model_override="gemini-model", openai_vs_id=test_vs_id)
            mock_call_gemini.assert_called_once_with(system_prompt, user_content, row_id, "gemini-model")
            mock_call_oai.assert_not_called()
            mock_call_pplx.assert_not_called()
            mock_call_gemini.reset_mock()

# --- Test call_openai_api correctly constructs tools --- #
# (Need to unpatch call_openai_api for this)
class TestCallOpenaiApiTools(unittest.TestCase):
    
    def setUp(self):
        self.mock_openai_client = MagicMock()
        self.mock_responses_create = self.mock_openai_client.responses.create
        self.system_prompt = "System Instructions"
        self.source_text = "Translate this"
        self.row_id = "R1-OAI"
        self.model = "gpt-4o-mini"
        self.vs_id = "vs_abc789"

    @patch.dict(sys.modules['src.config'].__dict__, {'API_MAX_RETRIES': 0, 'API_INITIAL_RETRY_DELAY': 0.01})
    def test_call_openai_api_with_vs_id_uses_tools(self):
        """Test call_openai_api includes 'tools' arg when vs_id is provided."""
        mock_response = MagicMock()
        mock_response.output = [MagicMock(type='message', role='assistant', content=[MagicMock(type='output_text', text='Translation OK')])]        
        self.mock_responses_create.return_value = mock_response

        result = translation_service.call_openai_api(
            self.mock_openai_client, self.system_prompt, self.source_text, 
            self.row_id, self.model, self.vs_id
        )

        self.assertEqual(result, "Translation OK")
        self.mock_responses_create.assert_called_once()
        call_args, call_kwargs = self.mock_responses_create.call_args
        
        # Check the keyword arguments passed to responses.create
        expected_tools = [
            {
                "type": "file_search",
                "vector_store_ids": [self.vs_id]
            }
        ]
        self.assertEqual(call_kwargs.get('model'), self.model)
        self.assertTrue(call_kwargs.get('input', '').startswith(self.system_prompt))
        self.assertTrue(call_kwargs.get('input', '').endswith(self.source_text))
        self.assertEqual(call_kwargs.get('tools'), expected_tools)
        # Ensure tool_choice and tool_resources are NOT passed
        self.assertNotIn('tool_choice', call_kwargs)
        self.assertNotIn('tool_resources', call_kwargs)

    @patch.dict(sys.modules['src.config'].__dict__, {'API_MAX_RETRIES': 0, 'API_INITIAL_RETRY_DELAY': 0.01})
    def test_call_openai_api_without_vs_id_omits_tools(self):
        """Test call_openai_api does NOT include 'tools' arg when vs_id is None."""
        mock_response = MagicMock()
        mock_response.output = [MagicMock(type='message', role='assistant', content=[MagicMock(type='output_text', text='Translation OK No VS')])]        
        self.mock_responses_create.return_value = mock_response
        
        result = translation_service.call_openai_api(
            self.mock_openai_client, self.system_prompt, self.source_text, 
            self.row_id, self.model, None # No VS ID
        )

        self.assertEqual(result, "Translation OK No VS")
        self.mock_responses_create.assert_called_once()
        call_args, call_kwargs = self.mock_responses_create.call_args
        
        self.assertEqual(call_kwargs.get('model'), self.model)
        self.assertTrue(call_kwargs.get('input', '').startswith(self.system_prompt))
        self.assertNotIn('tools', call_kwargs) # Ensure 'tools' is not present


# --- Add existing test classes below --- #
# class TestTranslationServiceExisting(...):
#    ...


if __name__ == '__main__':
    unittest.main(argv=['first-arg-is-ignored'], exit=False) 