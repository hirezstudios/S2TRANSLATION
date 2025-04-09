import streamlit as st
import os
import pandas as pd
import logging
import threading
import uuid
import json
from datetime import datetime
import time

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# Import backend modules (assuming app.py is in the root or PYTHONPATH is set)
# If app.py is in root, use 'from src import ...'
# If running with `streamlit run src/app.py`, use relative imports or adjust sys.path

# TEMPORARY: Add src to path for dev run from root `streamlit run app.py`
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))
try:
    from src import config
    from src import db_manager
    from src import prompt_manager
    from src import translation_service
    # Ensure API clients are initialized if needed by config
    from src import api_clients 
except ImportError as e:
    st.error(f"Failed to import backend modules: {e}. Ensure PYTHONPATH is set or run from root.")
    st.stop()

# Initialize session state variables
if 'current_batch_id' not in st.session_state:
    st.session_state['current_batch_id'] = None
if 'processing_thread' not in st.session_state:
    st.session_state['processing_thread'] = None
if 'batch_status' not in st.session_state:
    st.session_state['batch_status'] = 'idle' # idle, preparing, processing, completed, failed
if 'export_data' not in st.session_state:
    st.session_state['export_data'] = None
if 'export_filename' not in st.session_state:
    st.session_state['export_filename'] = None

st.set_page_config(layout="wide")
st.title("SMITE 2 Translation Helper")

# --- Helper Functions --- #
def get_valid_languages(uploaded_file):
    """Determines valid languages based on file columns, config, and loaded prompts."""
    valid_languages = []
    if not uploaded_file:
        return []
        
    try:
        # Read just the header
        # Save temporary copy to read header
        temp_dir = "temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        temp_file_path = os.path.join(temp_dir, f"temp_header_{uploaded_file.name}")
        with open(temp_file_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
            
        df_header = pd.read_csv(temp_file_path, nrows=0)
        os.remove(temp_file_path) # Clean up temp file
        
        header = df_header.columns.tolist()
        logger.info(f"Input file header: {header}")
        
        potential_langs = set()
        for col in header:
            if col.startswith("tg_"):
                potential_langs.add(col[3:]) # Add the code, e.g., "esLA"
        
        logger.info(f"Potential languages from file: {potential_langs}")
        logger.info(f"Languages configured in .env: {config.AVAILABLE_LANGUAGES}")
        logger.info(f"Languages with loaded prompts: {list(prompt_manager.stage1_templates.keys())}")

        # Find intersection
        for lang_code in config.AVAILABLE_LANGUAGES:
            if lang_code in potential_langs and lang_code in prompt_manager.stage1_templates:
                valid_languages.append(lang_code)
                
        logger.info(f"Valid languages for selection: {valid_languages}")
        
    except Exception as e:
        st.error(f"Error reading CSV header: {e}")
        logger.error(f"Error reading CSV header: {e}")
        return []
        
    return sorted(valid_languages)

# --- UI Sections ---
st.header("1. Upload & Configure")

uploaded_file = st.file_uploader("Upload Input CSV File", type=["csv"])

if uploaded_file:
    st.success(f"Uploaded: {uploaded_file.name}")
    # Get valid languages based on the uploaded file
    valid_languages_for_ui = get_valid_languages(uploaded_file)
    
    if not valid_languages_for_ui:
        st.warning("No translatable languages found based on file columns, .env configuration, and available prompt files. Please check your input file and setup.")
        st.stop()
else:
    st.info("Please upload a CSV file to begin.")
    st.stop()

# Language Selection (Dynamic)
st.subheader("Select Languages")
select_all = st.checkbox("Select All Valid Languages")
if select_all:
    selected_languages = st.multiselect(
        "Languages to Translate", 
        valid_languages_for_ui, 
        default=valid_languages_for_ui, # Select all by default if checkbox is ticked
        key="lang_multiselect"
    )
else:
    selected_languages = st.multiselect(
        "Languages to Translate", 
        valid_languages_for_ui,
        default=None, # No default selection if checkbox is not ticked
        key="lang_multiselect"
    )

# Mode & API Configuration
st.subheader("Configuration")
col1, col2 = st.columns(2)

with col1:
    mode = st.radio(
        "Translation Mode",
        options=config.VALID_TRANSLATION_MODES, 
        index=config.VALID_TRANSLATION_MODES.index(config.TRANSLATION_MODE), # Set default from config
        key="translation_mode"
    )

with col2:
    st.write("API Configuration")
    # Store API selections directly in session state via keys
    if mode == "ONE_STAGE":
        one_stage_api = st.selectbox(
            "API for Translation", 
            options=config.VALID_APIS,
            index=config.VALID_APIS.index(config.DEFAULT_API),
            key="one_stage_api" # Key for session state
        )
        # Get default model for the selected API
        api_default_model = getattr(config, f"{one_stage_api}_MODEL", "")
        st.text_input(
            "Model Override (Optional)", 
            key="one_stage_model_override", # Key for session state
            placeholder=f"Default: {api_default_model}"
        )
        # Ensure stage-specific keys are None if mode is ONE_STAGE
        st.session_state.s1_api = None
        st.session_state.s2_api = None
        st.session_state.s3_api = None
        st.session_state.s1_model_override = None
        st.session_state.s2_model_override = None
        st.session_state.s3_model_override = None

    elif mode == "THREE_STAGE":
        s1_api_default_index = config.VALID_APIS.index(config.STAGE1_API) if config.STAGE1_API in config.VALID_APIS else 0
        s1_api = st.selectbox("Stage 1 API (Translate)", options=config.VALID_APIS, index=s1_api_default_index, key="s1_api")
        s1_default_model = getattr(config, f"{s1_api}_MODEL", "")
        st.text_input("S1 Model Override", key="s1_model_override", placeholder=f"Default: {s1_default_model}")
        
        s2_api_default_index = config.VALID_APIS.index(config.STAGE2_API) if config.STAGE2_API in config.VALID_APIS else 0
        s2_api = st.selectbox("Stage 2 API (Evaluate)", options=config.VALID_APIS, index=s2_api_default_index, key="s2_api")
        s2_default_model = getattr(config, f"{s2_api}_MODEL", "")
        st.text_input("S2 Model Override", key="s2_model_override", placeholder=f"Default: {s2_default_model}")

        s3_api_default_index = config.VALID_APIS.index(config.STAGE3_API) if config.STAGE3_API in config.VALID_APIS else 0
        s3_api = st.selectbox("Stage 3 API (Refine)", options=config.VALID_APIS, index=s3_api_default_index, key="s3_api")
        s3_default_model = getattr(config, f"{s3_api}_MODEL", "")
        st.text_input("S3 Model Override", key="s3_model_override", placeholder=f"Default: {s3_default_model}")
        
        # Ensure one-stage keys are None
        st.session_state.one_stage_api = None
        st.session_state.one_stage_model_override = None

# --- Job Control & Monitoring --- #
st.header("2. Run Translation")

def run_translation_thread(file_path, selected_langs, mode_config):
    """Target function for the background processing thread.
       Updates session state and triggers reruns at key points."""
    batch_id_local = None # Local var for batch ID
    try:
        logger.info(f"Background thread started for {file_path}")
        st.session_state['batch_status'] = 'preparing' # Ensure status starts as preparing
        # Note: It's generally unsafe to call st.rerun() directly here
        # but we need to signal the UI change.
        # Consider if a queue/callback from thread is better long-term.
        # For now, trying direct rerun after key state changes.
        
        # 1. Prepare Batch (Uses mode_config)
        logger.info("Calling prepare_batch...")
        batch_id_local = translation_service.prepare_batch(file_path, selected_langs, mode_config)
        
        if batch_id_local:
            st.session_state['current_batch_id'] = batch_id_local
            st.session_state['batch_status'] = 'processing' # Update status
            logger.info(f"Prepare batch SUCCESS. Batch ID: {batch_id_local}. Status -> processing.")
            # NO RERUN HERE

            # Get initialized clients *within the thread* from the api_clients module
            openai_client_thread = api_clients.get_openai_client()
            
            # 2. Process Batch (Pass clients)
            logger.info(f"Calling process_batch for {batch_id_local}...")
            processed_data, success = translation_service.process_batch(
                batch_id_local, 
                openai_client_obj=openai_client_thread 
            )
            logger.info(f"Process_batch finished for {batch_id_local}. Success: {success}")
            
            # 3. Update Final Status based on success
            if success:
                st.session_state['batch_status'] = 'completed'
                st.session_state['export_data'] = processed_data
                base_name = os.path.splitext(uploaded_file.name)[0]
                api_name = mode_config.get('default_api', 'unknown').lower()
                st.session_state['export_filename'] = f"output_{base_name}_{api_name}_batch_{batch_id_local[:8]}.csv"
                logger.info(f"Batch {batch_id_local} completed successfully. Status -> completed.")
            else:
                st.session_state['batch_status'] = 'completed_with_errors'
                st.session_state['export_data'] = processed_data 
                base_name = os.path.splitext(uploaded_file.name)[0]
                api_name = mode_config.get('default_api', 'unknown').lower()
                st.session_state['export_filename'] = f"output_{base_name}_{api_name}_batch_{batch_id_local[:8]}_ERRORS.csv"
                logger.warning(f"Batch {batch_id_local} completed with errors. Status -> completed_with_errors.")
        else:
            st.session_state['batch_status'] = 'failed'
            logger.error("Batch preparation failed. Status -> failed.")

    except Exception as e:
        st.session_state['batch_status'] = 'failed'
        logger.exception(f"Critical error during background thread for batch {batch_id_local or 'UNKNOWN'}: {e}")
        # Also set batch status in DB if possible
        if batch_id_local:
            try:
                db_manager.update_batch_status(config.DATABASE_FILE, batch_id_local, 'failed')
            except Exception as db_e:
                logger.error(f"Failed to update batch status to failed in DB: {db_e}")
    finally:
        logger.info("Background thread finished.")
        # NO FINAL RERUN HERE - Main thread handles UI updates via polling

# --- Callback Function --- #
def start_translation_job_callback():
    """Callback triggered by the 'Start Translation Job' button."""
    logger.info("'Start Translation Job' button clicked.")
    if not selected_languages: # Access selected_languages from the outer scope
        st.warning("Please select at least one language to translate.")
        return # Stop callback execution
        
    if not uploaded_file: # Access uploaded_file from outer scope
        st.warning("File upload seems to have been lost. Please re-upload.")
        return # Stop callback execution
        
    # Save uploaded file temporarily
    temp_dir = "temp_uploads"
    os.makedirs(temp_dir, exist_ok=True)
    temp_file_name = f"{uuid.uuid4()}_{uploaded_file.name}"
    file_path = os.path.join(temp_dir, temp_file_name)
    try:
        with open(file_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        logger.info(f"Saved uploaded file temporarily to: {file_path}")
    except Exception as e:
        st.error(f"Failed to save uploaded file: {e}")
        logger.error(f"Failed to save uploaded file: {e}")
        return

    # Collect config from UI state
    mode = st.session_state.translation_mode
    if mode == "ONE_STAGE":
        final_mode_config = {
            "mode": mode,
            "default_api": st.session_state.one_stage_api,
            "one_stage_model": st.session_state.one_stage_model_override or None, 
            "s1_api": st.session_state.one_stage_api, "s1_model": st.session_state.one_stage_model_override or None,
            "s2_api": None, "s2_model": None,
            "s3_api": None, "s3_model": None,
        }
    else: # THREE_STAGE
         final_mode_config = {
            "mode": mode,
            "default_api": config.DEFAULT_API,
            "s1_api": st.session_state.s1_api,
            "s2_api": st.session_state.s2_api,
            "s3_api": st.session_state.s3_api,
            "s1_model": st.session_state.s1_model_override or None, 
            "s2_model": st.session_state.s2_model_override or None,
            "s3_model": st.session_state.s3_model_override or None,
        }
    
    logger.info(f"Starting job with config: {final_mode_config}")

    # Start background thread
    thread = threading.Thread(
        target=run_translation_thread, 
        args=(file_path, selected_languages, final_mode_config) 
    )
    st.session_state['processing_thread'] = thread
    st.session_state['batch_status'] = 'preparing' # Set initial status
    st.session_state['export_data'] = None # Clear previous export data
    st.session_state['export_filename'] = None
    thread.start()
    logger.info("Background translation thread started.")
    # No st.rerun() here - Streamlit handles it after button click/callback

# --- Button --- #
# Disable button if processing
disable_start = st.session_state['batch_status'] in ['preparing', 'processing']
st.button(
    "Start Translation Job", 
    disabled=disable_start, 
    on_click=start_translation_job_callback # Attach the callback
)

# --- Status Display & Refresh Loop --- #

current_status = st.session_state.get('batch_status', 'idle')

# Display Status / Progress (includes spinners)
if current_status == 'preparing':
    st.info(f"Preparing batch...") 
    with st.spinner("Preparing..."): pass
elif current_status == 'processing':
    st.info(f"Processing Batch ID: {st.session_state.get('current_batch_id', 'N/A')}...") 
    with st.spinner("Translating..."): pass 
elif current_status == 'completed':
    st.success(f"Batch {st.session_state.get('current_batch_id', 'N/A')} completed successfully!")
elif current_status == 'completed_with_errors':
    st.warning(f"Batch {st.session_state.get('current_batch_id', 'N/A')} completed with errors. Check logs and Audit file.")
elif current_status == 'failed':
    st.error(f"Batch {st.session_state.get('current_batch_id', 'N/A')} preparation or processing failed. Check logs.")

# Refresh Loop: If preparing or processing, wait and rerun
if current_status in ['preparing', 'processing']:
    logger.debug(f"Current status is {current_status}, sleeping and rerunning...")
    time.sleep(2) # Wait a couple of seconds before refresh
    st.rerun()

# --- Export --- #
st.header("3. Export Results")

enable_download = st.session_state['batch_status'] in ['completed', 'completed_with_errors'] and st.session_state['export_data'] is not None

if enable_download:
    try:
        # Ensure export_data is a list of dicts
        if isinstance(st.session_state['export_data'], list) and len(st.session_state['export_data']) > 0:
            df = pd.DataFrame(st.session_state['export_data'])
            
            # Attempt to reconstruct header order (Source, Metadata sorted, Targets sorted)
            # This might not match original input exactly
            all_columns = set(df.columns)
            source_col = config.SOURCE_COLUMN
            metadata_cols = sorted([c for c in all_columns if not c.startswith("tg_") and c != source_col])
            target_cols = sorted([c for c in all_columns if c.startswith("tg_")])
            ordered_header = [source_col] + metadata_cols + target_cols
            
            # Reindex DataFrame columns
            df_ordered = df[ordered_header]
            
            csv_data = df_ordered.to_csv(index=False).encode('utf-8')
            
            st.download_button(
                label="Download Output CSV",
                data=csv_data,
                file_name=st.session_state.get('export_filename', 'translation_output.csv'), # Use filename set by thread
                mime='text/csv',
            )
        elif isinstance(st.session_state['export_data'], list) and len(st.session_state['export_data']) == 0:
            st.info("Processing resulted in no data to export.")
        else:
             st.error("Export data is in an unexpected format.")
             logger.error(f"Export data type mismatch: {type(st.session_state['export_data'])}")

    except Exception as e:
        st.error(f"Failed to prepare data for download: {e}")
        logger.exception("Error preparing download data:")
else:
    st.info("Complete a translation job to enable export.")


# --- TODO Sections --- #
# - Results Review/Edit
# - Rules Editor
