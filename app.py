import streamlit as st
import os
import pandas as pd
import logging
import threading
import uuid
import json
from datetime import datetime

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

# --- UI Sections ---
st.header("1. Upload & Configure")

uploaded_file = st.file_uploader("Upload Input CSV File", type=["csv"])

if uploaded_file:
    st.success(f"Uploaded: {uploaded_file.name}")
    # TODO: Add file parsing and language detection here
else:
    st.info("Please upload a CSV file to begin.")
    st.stop()

# Placeholder for language selection
st.subheader("Select Languages")
# TODO: Populate dynamically
available_langs_ui = ["esLA", "frFR"] 
selected_languages = st.multiselect("Languages to Translate (must exist in file & prompts)", available_langs_ui, default=available_langs_ui)

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
    if mode == "ONE_STAGE":
        # Simple API select for ONE_STAGE
        one_stage_api = st.selectbox(
            "API for Translation", 
            options=config.VALID_APIS,
            index=config.VALID_APIS.index(config.DEFAULT_API), # Default
            key="one_stage_api"
        )
        # TODO: Add model selection based on API?
    
    elif mode == "THREE_STAGE":
        # Per-stage API selection
        s1_api = st.selectbox("Stage 1 API (Translate)", options=config.VALID_APIS, index=config.VALID_APIS.index(config.STAGE1_API), key="s1_api")
        s2_api = st.selectbox("Stage 2 API (Evaluate)", options=config.VALID_APIS, index=config.VALID_APIS.index(config.STAGE2_API), key="s2_api")
        s3_api = st.selectbox("Stage 3 API (Refine)", options=config.VALID_APIS, index=config.VALID_APIS.index(config.STAGE3_API), key="s3_api")
        # TODO: Add optional model override inputs?

# --- Job Control & Monitoring --- #
st.header("2. Run Translation")

def run_translation_thread(file_path, selected_langs, mode_config):
    """Target function for the background processing thread."""
    st.session_state['batch_status'] = 'preparing'
    try:
        # 1. Prepare Batch
        batch_id = translation_service.prepare_batch(file_path, selected_langs)
        if batch_id:
            st.session_state['current_batch_id'] = batch_id
            st.session_state['batch_status'] = 'processing'
            logger.info(f"Starting processing for batch {batch_id} in background...")
            
            # 2. Process Batch (blocking within this thread)
            processed_data, success = translation_service.process_batch(batch_id)
            
            if success:
                st.session_state['batch_status'] = 'completed'
                st.session_state['export_data'] = processed_data # Store data for export
                st.session_state['export_filename'] = f"output_{os.path.splitext(uploaded_file.name)[0]}_{mode_config.get('default_api', 'unknown')}_batch_{batch_id[:8]}.csv"
                logger.info(f"Batch {batch_id} completed successfully.")
            else:
                st.session_state['batch_status'] = 'completed_with_errors'
                st.session_state['export_data'] = processed_data # Store partial data?
                st.session_state['export_filename'] = f"output_{os.path.splitext(uploaded_file.name)[0]}_{mode_config.get('default_api', 'unknown')}_batch_{batch_id[:8]}_ERRORS.csv"
                logger.warning(f"Batch {batch_id} completed with errors.")
        else:
            st.session_state['batch_status'] = 'failed'
            logger.error("Batch preparation failed.")

    except Exception as e:
        st.session_state['batch_status'] = 'failed'
        logger.exception(f"Error during background translation thread: {e}")
    finally:
        # Clean up thread object in session state?
        # st.session_state['processing_thread'] = None # Careful with race conditions
        st.experimental_rerun() # Trigger UI update

# Disable button if processing
disable_start = st.session_state['batch_status'] in ['preparing', 'processing']

if st.button("Start Translation Job", disabled=disable_start):
    if not selected_languages:
        st.warning("Please select at least one language to translate.")
    else:
        # Save uploaded file temporarily (Streamlit handles this somewhat)
        # Using uploaded_file.name might be okay if running locally, but need a robust path
        # For simplicity now, assume backend can read from the uploaded file object path if possible
        # A safer way is to save it explicitly: 
        temp_dir = "temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        file_path = os.path.join(temp_dir, uploaded_file.name)
        with open(file_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        logger.info(f"Saved uploaded file temporarily to: {file_path}")

        # Collect config from UI
        mode_config = {
            "mode": st.session_state.translation_mode,
            "default_api": st.session_state.get("one_stage_api", config.DEFAULT_API), # Get value if exists
            "s1_api": st.session_state.get("s1_api", config.STAGE1_API),
            "s2_api": st.session_state.get("s2_api", config.STAGE2_API),
            "s3_api": st.session_state.get("s3_api", config.STAGE3_API),
            # Add models later
        }

        # Start background thread
        thread = threading.Thread(
            target=run_translation_thread, 
            args=(file_path, selected_languages, mode_config)
        )
        st.session_state['processing_thread'] = thread
        st.session_state['batch_status'] = 'preparing' # Set status immediately
        st.session_state['export_data'] = None # Clear previous export data
        st.session_state['export_filename'] = None
        thread.start()
        st.experimental_rerun() # Rerun to update UI (show spinner etc.)

# Display Status / Progress
if st.session_state['batch_status'] == 'preparing':
    st.info(f"Preparing batch...")
elif st.session_state['batch_status'] == 'processing':
    st.info(f"Processing Batch ID: {st.session_state['current_batch_id']}...")
    with st.spinner("Translating..."): 
        # We just show spinner; actual progress requires polling DB or thread communication
        pass 
elif st.session_state['batch_status'] == 'completed':
    st.success(f"Batch {st.session_state['current_batch_id']} completed successfully!")
elif st.session_state['batch_status'] == 'completed_with_errors':
    st.warning(f"Batch {st.session_state['current_batch_id']} completed with errors. Check logs and Audit file.")
elif st.session_state['batch_status'] == 'failed':
    st.error(f"Batch preparation or processing failed. Check logs.")

# --- Export --- #
st.header("3. Export Results")

# Enable download only if completed (with or without errors) and data exists
enable_download = st.session_state['batch_status'] in ['completed', 'completed_with_errors'] and st.session_state['export_data'] is not None

if enable_download:
    # Convert processed data (list of dicts) to CSV string for download
    try:
        df = pd.DataFrame(st.session_state['export_data'])
        # Reorder columns based on expected header if possible (needs header info)
        # For now, use DataFrame's default order
        csv_data = df.to_csv(index=False).encode('utf-8')
        
        st.download_button(
            label="Download Output CSV",
            data=csv_data,
            file_name=st.session_state.get('export_filename', 'translation_output.csv'),
            mime='text/csv',
        )
    except Exception as e:
        st.error(f"Failed to prepare data for download: {e}")
else:
    st.info("Complete a translation job to enable export.")


# --- TODO Sections --- #
# - Results Review/Edit
# - Rules Editor
