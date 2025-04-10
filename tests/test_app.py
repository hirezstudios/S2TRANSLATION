from streamlit.testing.v1 import AppTest
import pytest
import os

# Assuming app.py is in the root directory relative to tests/
APP_FILE = os.path.join(os.path.dirname(__file__), '..', 'app.py')

# Note: These tests are basic structure checks. 
# More complex tests involving backend interaction, file uploads,
# and state changes will require mocking or more setup.

@pytest.fixture
def at():
    """Fixture to create an AppTest instance."""
    # Change CWD temporarily if needed for backend imports in app.py
    # cwd = os.getcwd()
    # os.chdir(os.path.dirname(APP_FILE))
    # print(f"App file path: {APP_FILE}")
    # print(f"CWD for test: {os.getcwd()}")
    try:
        at = AppTest.from_file(APP_FILE)
        at.run(timeout=15)
        yield at
    finally:
        # os.chdir(cwd)
        pass # No CWD change needed if backend imports work

def test_smoke(at):
    """Basic smoke test: Does the app run without crashing?"""
    assert not at.exception

def test_title(at):
    """Test if the title is set correctly."""
    assert at.title[0].value == "SMITE 2 Translation Helper"

def test_initial_widgets(at):
    """Test if initial widgets (uploader info) are present *before* upload."""
    # Check for the initial info message indicating file upload is needed
    assert len(at.info) > 0
    assert at.info[0].value == "Please upload a CSV file to begin."
    # Config sections shouldn't render fully before upload
    assert len(at.radio) == 0 
    assert len(at.multiselect) == 0
    assert len(at.button) == 0

# Example of a test that might require file interaction (more complex)
# def test_upload_shows_success(at):
#     # This requires simulating a file upload, which AppTest might not fully support easily.
#     # Might need alternative testing approach (e.g., mocking st.file_uploader)
#     # at.file_uploader[0].set_value("tests/fixtures/test_input.csv") # Hypothetical API
#     # at.run()
#     # assert "Uploaded:" in at.success[0].value
#     pass

# Example testing config changes (interacts with session state)
# def test_mode_change_updates_api_widgets(at):
#     # Select THREE_STAGE mode
#     at.radio(key="translation_mode").set_value("THREE_STAGE").run()
#     assert len(at.selectbox(key="s1_api")) == 1
#     assert len(at.selectbox(key="s2_api")) == 1
#     assert len(at.selectbox(key="s3_api")) == 1
#     assert len(at.selectbox(key="one_stage_api")) == 0 # Should disappear
    
#     # Select ONE_STAGE mode
#     at.radio(key="translation_mode").set_value("ONE_STAGE").run()
#     assert len(at.selectbox(key="one_stage_api")) == 1
#     assert len(at.selectbox(key="s1_api")) == 0 # Should disappear
#     pass

# Cannot easily test post-upload state with AppTest file handling
# def test_widgets_after_upload(at):
#     # Needs mocking or a different approach
#     pass