import os
import logging
from flask import Flask, request, render_template, jsonify, send_file, flash, redirect, url_for
import uuid
import json
import threading
from werkzeug.utils import secure_filename
import pandas as pd # Make sure pandas is imported
import glob # Added for rule file scanning
from datetime import datetime # Added for archiving
import re # Added for parsing tg_ filenames
import sys # Add sys import
import io # <<< ADD io for StringIO >>>

# --- Add project root to sys.path --- #
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
# ------------------------------------ #

# Import backend modules
from src import config
from src import db_manager
from src import translation_service
from src import prompt_manager
from src import api_clients
from src import vector_store_manager # Now actually used

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s') # Changed level to DEBUG
logger = logging.getLogger(__name__)

# --- Explicitly set levels for module loggers --- #
logging.getLogger('src.translation_service').setLevel(logging.DEBUG)
logging.getLogger('src.db_manager').setLevel(logging.DEBUG)
logging.getLogger('src.prompt_manager').setLevel(logging.INFO) # Set prompt_manager to INFO for less noise
logging.getLogger('src.vector_store_manager').setLevel(logging.INFO) # Add level for vector store manager
logger.info("Log levels configured.")
# --- End Explicit level setting --- #

# --- Constants for Rules Feature ---
SYSTEM_PROMPT_DIR = config.SYSTEM_PROMPT_DIR # Use path from config
SYSTEM_ARCHIVE_DIR = config.ARCHIVE_DIR # Use path from config
USER_PROMPT_DIR = "user_prompts"
USER_ARCHIVE_DIR = os.path.join(USER_PROMPT_DIR, "archive")

# --- Constants for Admin Vector Store feature ---
ALLOWED_EXTENSIONS = {'csv'}
UPLOAD_FOLDER = config.UPLOAD_FOLDER
SOURCE_COLUMN_NAME = "src_enUS" # Define the expected source column name

# === Helper Functions ===
def get_available_rule_languages():
    """Scans the system prompt directory for language-specific rule files (tg_*.md)."""
    languages = set()
    try:
        prompt_dir = config.SYSTEM_PROMPT_DIR
        if not os.path.isdir(prompt_dir):
            logger.error(f"System prompt directory not found: {prompt_dir}")
            return []

        # Regex to match tg_{lang_code}.md and capture lang_code (e.g., esLA, frFR)
        # Allows 2 or 3 letter language code + 2 letter region code
        lang_pattern = re.compile(r'^tg_([a-zA-Z]{2,3}[A-Z]{2})\.md$')

        for filename in os.listdir(prompt_dir):
            match = lang_pattern.match(filename)
            if match:
                languages.add(match.group(1))
        
        logger.info(f"Found rule files for languages: {sorted(list(languages))}")
        return sorted(list(languages))
    except Exception as e:
        logger.exception(f"Error scanning for rule languages: {e}")
        return []

# === End Helper Functions ===

# --- Helper function to get rule files status ---
def get_rule_files():
    """Scans system and user prompt directories for rule files and their status."""
    all_files_status = {}

    # Define base patterns relative to SYSTEM_PROMPT_DIR
    patterns = {
        'global.md': 'Global Rules',
        'stage2_evaluate_template.md': 'Stage 2 Evaluate Template',
        'stage3_refine_template.md': 'Stage 3 Refine Template',
        config.STAGE4_TEMPLATE_FILE: 'Stage 4 Retranslate Template', # Added Stage 4
        'tg_*.md': 'Language Specific Rules' # Placeholder description
    }

    # Ensure base directories exist
    if not os.path.isdir(SYSTEM_PROMPT_DIR):
        logger.error(f"System prompt directory not found: {SYSTEM_PROMPT_DIR}")
        # Maybe raise an error or return empty with flash?
        return {}

    # 1. Scan SYSTEM_PROMPT_DIR for base files
    system_files = set()
    for filename_pattern_or_name, description in patterns.items():
        if '*' in filename_pattern_or_name: # Handle wildcard patterns (like tg_*.md)
             full_pattern = os.path.join(SYSTEM_PROMPT_DIR, filename_pattern_or_name)
             found_files = glob.glob(full_pattern)
        else: # Handle specific filenames
             file_path = os.path.join(SYSTEM_PROMPT_DIR, filename_pattern_or_name)
             found_files = [file_path] if os.path.isfile(file_path) else []
             
        for file_path in found_files:
            # Ensure it's directly in system_prompts, not archive
            if os.path.dirname(file_path) == SYSTEM_PROMPT_DIR:
                base_filename = os.path.basename(file_path)
                system_files.add(base_filename)
                # Store initial description based on system file
                current_description = description
                if filename_pattern_or_name == 'tg_*.md':
                    lang_code_match = re.match(r"tg_([a-zA-Z]{2}[A-Z]{2})\.md", base_filename)
                    if lang_code_match:
                        lang_code = lang_code_match.group(1)
                        current_description = f"{config.LANGUAGE_NAME_MAP.get(lang_code, lang_code)} Rules"
                all_files_status[base_filename] = {'description': current_description, 'has_override': False}
            else:
                # This case might catch the specific files if path wasn't directly in SYSTEM_PROMPT_DIR
                if not '*' in filename_pattern_or_name and not os.path.isfile(file_path):
                     logger.warning(f"Expected specific rule file not found: {file_path}")

    # 2. Scan USER_PROMPT_DIR for overrides
    if os.path.isdir(USER_PROMPT_DIR):
        user_files = set()
        pattern = os.path.join(USER_PROMPT_DIR, '*.md') # Look for any .md file
        found_files = glob.glob(pattern)
        for file_path in found_files:
             # Ensure it's directly in user_prompts, not archive
             if os.path.dirname(file_path) == USER_PROMPT_DIR:
                 base_filename = os.path.basename(file_path)
                 user_files.add(base_filename)
                 # If this file also exists in system, mark as override
                 if base_filename in all_files_status:
                     all_files_status[base_filename]['has_override'] = True
                 # If it only exists in user_prompts (maybe orphaned?), add it?
                 # For now, only show overrides for files that have a system base.
                 # else: 
                 #    all_files_status[base_filename] = {'description': f"User Only: {base_filename}", 'has_override': True} 
    else:
        # If user_prompts doesn't exist, no overrides are possible yet
        logger.info(f"User prompts directory not found: {USER_PROMPT_DIR}. No overrides loaded.")
        pass

    # Sort rules for consistent display: global, stages, then languages alphabetically
    sorted_rule_files = {}
    # Add specific files in desired order
    # Use os.path.basename for keys derived from config paths
    order = [
        os.path.basename(config.GLOBAL_RULES_FILE),
        os.path.basename(config.STAGE2_TEMPLATE_FILE),
        os.path.basename(config.STAGE3_TEMPLATE_FILE),
        os.path.basename(config.STAGE4_TEMPLATE_FILE) 
    ]
    for fname in order:
        if fname in all_files_status:
            sorted_rule_files[fname] = all_files_status.pop(fname)

    # Add remaining language files sorted alphabetically
    for fname in sorted(all_files_status.keys()):
         if fname.startswith('tg_'): # Ensure we only add language files here
             sorted_rule_files[fname] = all_files_status[fname]
         # Add any other non-standard files found at the end? Or ignore?
         # else: 
         #    sorted_rule_files[fname] = all_files_status[fname]

    return sorted_rule_files

# --- Helper for file uploads ---
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
# -----------------------------

# --- Background Processing Function for Vector Stores --- #
def _process_vector_store_set_background(app_context, set_id, csv_path):
    """Processes a CSV file to create OpenAI vector stores in a background thread."""
    with app_context:
        logger = logging.getLogger(__name__) # Get logger within app context
        logger.info(f"[BG Task Set {set_id}] Starting background processing for {csv_path}")
        final_statuses = {}

        try:
            # 1. Get mappings for this set_id
            mappings = db_manager.get_mappings_for_set(config.DATABASE_FILE, set_id)
            if mappings is None:
                logger.error(f"[BG Task Set {set_id}] Failed to retrieve mappings from database. Aborting.")
                return
            if not mappings:
                logger.warning(f"[BG Task Set {set_id}] No mappings found for this set. Nothing to process.")
                return

            logger.info(f"[BG Task Set {set_id}] Found {len(mappings)} mappings to process.")

            # 2. Check CSV header columns (Source + all Target columns from mappings)
            try:
                 df_cols = pd.read_csv(csv_path, nrows=0).columns
                 if SOURCE_COLUMN_NAME not in df_cols:
                     logger.error(f"[BG Task Set {set_id}] Source column '{SOURCE_COLUMN_NAME}' not found in {csv_path}. Aborting.")
                     for mapping in mappings:
                         if mapping['status'] == 'pending':
                            db_manager.update_mapping_status(config.DATABASE_FILE, mapping['mapping_id'], 'failed')
                            final_statuses[mapping['language_code']] = 'failed (Missing Source Col)'
                     return # Exit thread - no point continuing
                 
                 mapping_target_cols = {m['column_name'] for m in mappings}
                 missing_targets = mapping_target_cols - set(df_cols)
                 if missing_targets:
                     logger.error(f"[BG Task Set {set_id}] Target column(s) {missing_targets} from mappings not found in {csv_path}. Failing affected mappings.")
                     for mapping in mappings:
                         if mapping['column_name'] in missing_targets and mapping['status'] == 'pending':
                             db_manager.update_mapping_status(config.DATABASE_FILE, mapping['mapping_id'], 'failed')
                             final_statuses[mapping['language_code']] = 'failed (Missing Target Col)'
                     # Continue processing other valid mappings
            except Exception as e:
                logger.exception(f"[BG Task Set {set_id}] Error reading CSV columns from {csv_path}: {e}. Aborting.")
                for mapping in mappings:
                     if mapping['status'] == 'pending':
                         db_manager.update_mapping_status(config.DATABASE_FILE, mapping['mapping_id'], 'failed')
                         final_statuses[mapping['language_code']] = 'failed (CSV Read Error)'
                return # Exit thread

            # 3. Process each mapping
            for mapping in mappings:
                # Skip if failed during header check or already processed in a previous run (if applicable)
                if mapping['status'] != 'pending':
                    logger.info(f"[BG Task Set {set_id}/Map {mapping['mapping_id']}] Skipping mapping for {mapping['language_code']} (status: {mapping['status']})")
                    if mapping['status'] != 'completed': # Update final status if skipped but not complete
                        final_statuses[mapping['language_code']] = mapping['status']
                    continue
                    
                mapping_id = mapping['mapping_id']
                lang_code = mapping['language_code']
                col_name = mapping['column_name']
                logger.info(f"[BG Task Set {set_id}/Map {mapping_id}] Processing mapping for {lang_code} ({col_name})")

                # Update status to 'processing'
                db_manager.update_mapping_status(config.DATABASE_FILE, mapping_id, 'processing')

                # Call the OpenAI vector store creation function
                result_ids = vector_store_manager.create_openai_vector_store_for_language(
                    csv_path=csv_path,
                    source_column=SOURCE_COLUMN_NAME,
                    target_column=col_name,
                    language_code=lang_code,
                    set_id=set_id,
                    mapping_id=mapping_id
                )

                # Update status based on result
                if result_ids:
                    openai_vs_id, openai_file_id = result_ids
                    db_manager.update_mapping_status(config.DATABASE_FILE, mapping_id, 'completed', openai_vs_id=openai_vs_id, openai_file_id=openai_file_id)
                    final_statuses[lang_code] = 'completed'
                    logger.info(f"[BG Task Set {set_id}/Map {mapping_id}] Completed processing for {lang_code}")
                else:
                    # The create function handles logging the error, just update status
                    db_manager.update_mapping_status(config.DATABASE_FILE, mapping_id, 'failed')
                    final_statuses[lang_code] = 'failed'
                    logger.error(f"[BG Task Set {set_id}/Map {mapping_id}] Failed processing for {lang_code}")
                    # Continue processing other mappings

            # 4. Check overall success and activate if needed
            # Re-fetch mappings to get final statuses after loop
            final_mappings = db_manager.get_mappings_for_set(config.DATABASE_FILE, set_id)
            all_successful = False # Default to false
            if final_mappings:
                 all_successful = all(m['status'] == 'completed' for m in final_mappings)
                 if not final_mappings: # Double check if list is empty
                     all_successful = False 
                     logger.warning(f"[BG Task Set {set_id}] No mappings found after processing loop; set will not be activated.")
            else: 
                 logger.warning(f"[BG Task Set {set_id}] Failed to fetch final mappings; set will not be activated.")

            logger.info(f"[BG Task Set {set_id}] Finished processing mappings. Overall success: {all_successful}. Final statuses: {final_statuses}")
            if all_successful:
                logger.info(f"[BG Task Set {set_id}] All mappings completed successfully. Activating set.")
                activated = db_manager.activate_set(config.DATABASE_FILE, set_id)
                if activated:
                    logger.info(f"[BG Task Set {set_id}] Successfully activated.")
                else:
                    logger.error(f"[BG Task Set {set_id}] Failed to activate set after successful mapping completion.")
            else:
                logger.warning(f"[BG Task Set {set_id}] Not all mappings completed successfully. Set will remain inactive.")

        except Exception as e:
            logger.exception(f"[BG Task Set {set_id}] Unexpected error during background processing for {csv_path}: {e}")
            # Attempt to mark remaining pending/processing mappings as failed
            try:
                current_mappings = db_manager.get_mappings_for_set(config.DATABASE_FILE, set_id)
                if current_mappings:
                    for mapping in current_mappings:
                        if mapping['status'] in ['pending', 'processing']:
                            db_manager.update_mapping_status(config.DATABASE_FILE, mapping['mapping_id'], 'failed')
            except Exception as db_e:
                 logger.exception(f"[BG Task Set {set_id}] Error trying to mark mappings as failed during cleanup: {db_e}")

# --- Flask App Setup ---
def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.urandom(24) # Needed for session/flash
    app.config['UPLOAD_FOLDER'] = 'uploads' # Define upload folder config
    
    # Ensure necessary directories exist
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    # os.makedirs(config.INPUT_CSV_DIR, exist_ok=True) # Removed, using temp_uploads instead
    # os.makedirs(ARCHIVE_DIR, exist_ok=True) # Use constant - System archive handled via config?
    # Let's explicitly ensure User archive dir exists on startup too
    os.makedirs(USER_ARCHIVE_DIR, exist_ok=True)
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True) # Ensure upload folder exists
    db_manager.initialize_database(config.DATABASE_FILE)
    prompt_manager.load_prompts()

    @app.route('/')
    def index():
        # Determine default models based on default APIs AND stage-specific overrides in config
        def get_default_model_for_api(api_name):
            if api_name == 'OPENAI':
                return config.OPENAI_MODEL
            elif api_name == 'GEMINI':
                return config.GEMINI_MODEL
            elif api_name == 'PERPLEXITY':
                return config.PPLX_MODEL
            return None

        # Pass available APIs and the calculated default API selections and default model names
        default_apis = {
            'ONE_STAGE': config.STAGE1_API, # ONE_STAGE uses S1 config
            'S1': config.STAGE1_API,
            'S2': config.STAGE2_API,
            'S3': config.STAGE3_API
        }
        
        # Prioritize stage-specific model overrides from config, then fall back to API default
        default_models = {
            'S0': config.S0_MODEL, # S0 is explicitly configured
            'S1': config.STAGE1_MODEL_OVERRIDE if config.STAGE1_MODEL_OVERRIDE else get_default_model_for_api(config.STAGE1_API),
            'S2': config.STAGE2_MODEL_OVERRIDE if config.STAGE2_MODEL_OVERRIDE else get_default_model_for_api(config.STAGE2_API),
            'S3': config.STAGE3_MODEL_OVERRIDE if config.STAGE3_MODEL_OVERRIDE else get_default_model_for_api(config.STAGE3_API),
            # ONE_STAGE uses the same logic as S1
            'ONE_STAGE': config.STAGE1_MODEL_OVERRIDE if config.STAGE1_MODEL_OVERRIDE else get_default_model_for_api(config.STAGE1_API) 
        }
        
        return render_template('index.html', 
                               valid_apis=config.VALID_APIS, 
                               default_apis=default_apis,
                               default_models=default_models) # Pass models to template

    @app.route('/upload_temp_file', methods=['POST'])
    def upload_temp_file():
        """Temporarily saves uploaded file for header reading."""
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        file = request.files['file']
        if not file or not file.filename:
            return jsonify({"error": "No selected file or filename missing"}), 400
        
        filename = secure_filename(file.filename)
        temp_dir = "temp_uploads"
        os.makedirs(temp_dir, exist_ok=True)
        temp_filename = f"{uuid.uuid4()}_{filename}"
        file_path = os.path.join(temp_dir, temp_filename)
        try:
            file.save(file_path)
            logger.info(f"Temp file saved for header check: {file_path}")
            # We should probably delete this file after use in get_valid_languages
            # Or have a cleanup job
            return jsonify({"file_path": file_path}), 200
        except Exception as e:
            logger.error(f"Failed to save temp file: {e}", exc_info=True)
            return jsonify({"error": "Failed to save temporary file"}), 500

    @app.route('/get_valid_languages', methods=['POST'])
    def get_valid_languages_route():
        """Reads temp file header and returns valid languages."""
        logger.info("Received request to /get_valid_languages")
        data = request.get_json()
        temp_file_path = data.get('file_path')
        
        if not temp_file_path or not os.path.exists(temp_file_path):
            logger.error(f"File path missing or file not found: {temp_file_path}")
            return jsonify({"error": "File not found or path missing."}), 400
            
        valid_languages = []
        try:
            df_header = pd.read_csv(temp_file_path, nrows=0)
            header = df_header.columns.tolist()
            logger.info(f"Read header for validation: {header}")
            
            potential_langs = {col[3:] for col in header if col.startswith("tg_")}
            
            # Iterate over dynamically discovered languages with prompts
            for lang_code in prompt_manager.available_languages:
                if lang_code in potential_langs:
                    valid_languages.append(lang_code)
                    
            logger.info(f"Valid languages identified: {valid_languages}")
            return jsonify({"valid_languages": sorted(valid_languages)}), 200
            
        except Exception as e:
            logger.error(f"Error processing header for {temp_file_path}: {e}", exc_info=True)
            return jsonify({"error": "Failed to process file header."}), 500

    @app.route('/start_job', methods=['POST'])
    def start_job():
        logger.info("Received request to /start_job")
        try:
            # --- NEW: Determine Input Mode --- #
            input_mode = request.form.get('input_mode', 'csv') # Default to 'csv' if not provided
            source_phrase = request.form.get('source_phrase', '').strip()
            file = request.files.get('file') # Use .get() to handle missing file gracefully

            logger.info(f"Input mode: {input_mode}, File provided: {file is not None and file.filename != ''}, Phrase provided: {bool(source_phrase)}")

            # --- Get Common Form Data --- #
            selected_languages = request.form.getlist('languages') # For CSV mode, this comes from file; for phrase mode, it's from checkboxes
            mode = request.form.get('mode', 'FOUR_STAGE') # Update default based on UI
            one_stage_api = request.form.get('one_stage_api', config.DEFAULT_API)
            one_stage_model = request.form.get('one_stage_model') or None
            s1_api = request.form.get('s1_api', config.STAGE1_API)
            s1_model = request.form.get('s1_model') or None
            s2_api = request.form.get('s2_api', config.STAGE2_API)
            s2_model = request.form.get('s2_model') or None
            s3_api = request.form.get('s3_api', config.STAGE3_API)
            s3_model = request.form.get('s3_model') or None
            s0_model = request.form.get('s0_model') or config.S0_MODEL # For Four Stage
            s1_api_four = request.form.get('s1_api_four', config.STAGE1_API)
            s1_model_four = request.form.get('s1_model_four') or None
            s2_api_four = request.form.get('s2_api_four', config.STAGE2_API)
            s2_model_four = request.form.get('s2_model_four') or None
            s3_api_four = request.form.get('s3_api_four', config.STAGE3_API)
            s3_model_four = request.form.get('s3_model_four') or None

            use_vs = True if mode == 'FOUR_STAGE' else request.form.get('use_vector_store') == 'true'
            batch_prompt_text = request.form.get('batch_prompt', '').strip()
            update_strategy = request.form.get('update_strategy', 'update_existing') # Update default based on UI

            # --- Initialize variables for processing --- #
            file_path_to_process = None
            original_filename = "quick_translate.csv" # Default filename for phrase mode
            batch_languages = []
            cleanup_file_path = None # Path to delete after processing

            # --- Handle SINGLE PHRASE mode --- #
            if input_mode == 'phrase':
                logger.info("Processing in single-phrase mode.")
                if not source_phrase:
                    return jsonify({"error": "Source phrase cannot be empty for single phrase mode."}), 400
                if not selected_languages:
                     return jsonify({"error": "No languages selected for single phrase mode."}), 400

                # Force retranslate strategy for single phrase
                update_strategy = 'retranslate'
                logger.info(f"Forcing update_strategy to '{update_strategy}' for single phrase mode.")

                batch_languages = selected_languages # Use languages selected in the form

                # Create an in-memory CSV representation
                # Required columns: Record ID, Context, DeveloperNotes, src_enUS
                csv_data = {
                    'Record ID': ['quick_phrase_1'],
                    'Context': [''],
                    'DeveloperNotes': ['Quick Translate Feature'],
                    config.SOURCE_COLUMN: [source_phrase]
                }
                df = pd.DataFrame(csv_data)
                string_io = io.StringIO()
                df.to_csv(string_io, index=False, encoding='utf-8')
                string_io.seek(0)

                # Save this in-memory CSV to a temporary file for prepare_batch
                temp_dir = "temp_uploads"
                os.makedirs(temp_dir, exist_ok=True)
                temp_filename = f"{uuid.uuid4()}_quick_translate.csv"
                file_path_to_process = os.path.join(temp_dir, temp_filename)
                cleanup_file_path = file_path_to_process # Mark for cleanup

                with open(file_path_to_process, 'w', encoding='utf-8') as f:
                    f.write(string_io.getvalue())
                logger.info(f"Saved in-memory single-phrase CSV to temporary file: {file_path_to_process}")

            # --- Handle CSV UPLOAD mode --- #
            elif file and file.filename != '' and allowed_file(file.filename):
                logger.info("Processing in CSV upload mode.")
                original_filename = secure_filename(file.filename)
                temp_dir = "temp_uploads"
                os.makedirs(temp_dir, exist_ok=True)
                temp_filename = f"{uuid.uuid4()}_{original_filename}"
                file_path_to_process = os.path.join(temp_dir, temp_filename)
                cleanup_file_path = file_path_to_process # Mark for cleanup
                file.save(file_path_to_process)
                logger.info(f"Temporarily saved uploaded CSV file to {file_path_to_process}")

                # Determine languages from CSV header for batch processing
                try:
                    df_header = pd.read_csv(file_path_to_process, nrows=0)
                    header = df_header.columns.tolist()
                    potential_langs = {col[3:] for col in header if col.startswith("tg_")}
                    # Use only languages selected in UI that *also* exist in the file
                    batch_languages = [lang for lang in selected_languages if lang in potential_langs]
                    if not batch_languages:
                         # If user selected languages not in file, should we error or proceed with empty?
                         logger.warning(f"Selected languages {selected_languages} not found as columns in {original_filename}. Proceeding with empty language list (will likely result in 'completed_empty' status).")
                         # Returning error is safer
                         return jsonify({"error": f"Selected languages {selected_languages} not found as columns (tg_*) in the uploaded CSV."}), 400
                except Exception as e:
                    logger.exception(f"Error reading header from uploaded CSV {file_path_to_process}")
                    return jsonify({"error": "Failed to read header from uploaded CSV."}), 500
            else:
                # Invalid state - neither valid phrase nor valid file upload
                logger.warning("Invalid request to /start_job: Neither valid phrase nor valid file provided.")
                error_msg = "Invalid input: Provide either a source phrase or upload a valid CSV file."
                if file and not allowed_file(file.filename):
                    error_msg = "Invalid file type. Only CSV files are allowed."
                elif file and file.filename == '':
                    error_msg = "No file selected."
                return jsonify({"error": error_msg}), 400

            # --- Construct Mode Config (common to both modes) --- #
            logger.info(f"Constructing mode config - Mode: {mode}, Strategy: {update_strategy}, Languages: {batch_languages}, Use VS: {use_vs}")
            mode_config = {
                "mode": mode,
                "languages": batch_languages, # Use languages determined by mode
                "use_vs": use_vs,
                "batch_prompt": batch_prompt_text,
                "update_strategy": update_strategy,
                 # Add API/Model details based on actual mode selected
                "s0_model": s0_model if mode == "FOUR_STAGE" else None,
                "s1_api": one_stage_api if mode == "ONE_STAGE" else (s1_api_four if mode == "FOUR_STAGE" else s1_api),
                "s1_model": one_stage_model if mode == "ONE_STAGE" else (s1_model_four if mode == "FOUR_STAGE" else s1_model),
                "s2_api": s2_api_four if mode == "FOUR_STAGE" else (s2_api if mode == "THREE_STAGE" else None),
                "s2_model": s2_model_four if mode == "FOUR_STAGE" else (s2_model if mode == "THREE_STAGE" else None),
                "s3_api": s3_api_four if mode == "FOUR_STAGE" else (s3_api if mode == "THREE_STAGE" else None),
                "s3_model": s3_model_four if mode == "FOUR_STAGE" else (s3_model if mode == "THREE_STAGE" else None)
            }
            # Add input_type to config for tracking
            mode_config['input_type'] = input_mode
            logger.debug(f"Final mode_config: {mode_config}")

            # --- Call prepare_batch --- #
            if not file_path_to_process:
                 logger.error("Internal error: file_path_to_process is None before calling prepare_batch.")
                 return jsonify({"error": "Internal server error preparing job."}), 500

            # <<< ADD DETAILED DEBUGGING BEFORE PREPARE BATCH >>>
            logger.debug(f"DEBUG PRE-PREPARE - Mode: {mode}")
            logger.debug(f"DEBUG PRE-PREPARE - Selected Languages: {batch_languages}")
            logger.debug(f"DEBUG PRE-PREPARE - Form value one_stage_api: {request.form.get('one_stage_api')}")
            logger.debug(f"DEBUG PRE-PREPARE - Variable one_stage_api: {one_stage_api}")
            logger.debug(f"DEBUG PRE-PREPARE - config.DEFAULT_API: {config.DEFAULT_API}")
            logger.debug(f"DEBUG PRE-PREPARE - config.STAGE1_API: {config.STAGE1_API}")
            logger.debug(f"DEBUG PRE-PREPARE - Constructed mode_config: {mode_config}")
            # <<< END DEBUGGING >>>

            batch_id = translation_service.prepare_batch(
                 input_file_path=file_path_to_process,
                 original_filename=original_filename, # Pass original/generated filename
                 selected_languages=batch_languages, # Pass determined languages
                 mode_config=mode_config
            )

            if batch_id:
                logger.info(f"Batch {batch_id} prepared for {input_mode} input. Starting background thread...")
                thread = threading.Thread(
                    target=translation_service.run_batch_background,
                    args=(batch_id,)
                )
                thread.daemon = True
                thread.start()
                # Clear cleanup path *only* if thread started successfully
                cleanup_file_path = None 
                return jsonify({"message": "Job started successfully", "batch_id": batch_id}), 200
            else:
                logger.error(f"Batch preparation failed for {input_mode} input (prepare_batch returned None). File: {file_path_to_process}")
                # Cleanup handled in finally block
                return jsonify({"error": "Batch preparation failed. Check logs and input."}) , 500

        except Exception as e:
             logger.exception(f"Error in /start_job: {e}")
             # Cleanup potentially needed here too, handled by finally
             return jsonify({"error": "An unexpected error occurred processing the request."}), 500
        finally:
             # --- Cleanup temporary file --- #
             if cleanup_file_path and os.path.exists(cleanup_file_path):
                 logger.info(f"Cleaning up temporary file: {cleanup_file_path}")
                 try:
                     os.remove(cleanup_file_path)
                 except OSError as e:
                     logger.error(f"Could not remove temp file {cleanup_file_path}: {e}")

    @app.route('/status/<batch_id>')
    def get_batch_status(batch_id):
        db_path = config.DATABASE_FILE
        batch_info = db_manager.get_batch_info(db_path, batch_id)
        if not batch_info:
            return jsonify({"error": "Batch not found"}), 404

        total_tasks = db_manager.count_tasks_for_batch(db_path, batch_id)
        # Count tasks that are definitively finished (completed or failed/error)
        completed_tasks = db_manager.count_tasks_by_status(db_path, batch_id, 'completed')
        # Consider different error statuses if used (e.g., 'failed', 'error')
        error_tasks = db_manager.count_tasks_by_status(db_path, batch_id, 'failed') 
        error_tasks += db_manager.count_tasks_by_status(db_path, batch_id, 'error') # Add older 'error' status too?

        response_data = {
            "batch_id": batch_id,
            "batch_status": batch_info['status'],
            "total_tasks": total_tasks,
            "completed_tasks": completed_tasks,
            "error_tasks": error_tasks
        }

        # <<< ADD STAGE 0 SKIPPED COUNT for FOUR_STAGE >>>
        try:
            batch_config = json.loads(batch_info['config_details'] or '{}')
            if batch_config.get('mode') == 'FOUR_STAGE':
                skipped_s0_count = db_manager.count_tasks_with_stage0_status(db_path, batch_id, 'skipped_no_vs')
                if skipped_s0_count > 0:
                     response_data['skipped_stage0_count'] = skipped_s0_count
        except Exception as e:
            logger.warning(f"Could not check Stage 0 skipped status for batch {batch_id}: {e}")
        # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

        return jsonify(response_data)

    @app.route('/export/<batch_id>')
    def export_batch(batch_id):
        logger.info(f"Received export request for batch {batch_id}")
        db_path = config.DATABASE_FILE # Get DB path
        try:
            batch_info = db_manager.get_batch_info(db_path, batch_id)
            if not batch_info:
                 return jsonify({"error": "Batch not found"}), 404
            if not batch_info['config_details']:
                return jsonify({"error": "Batch configuration not found"}), 404
                
            batch_config = json.loads(batch_info['config_details'])
            original_filename = batch_info['upload_filename'] or "unknown_file.csv"
            base_name = os.path.splitext(original_filename)[0]
            api_name = batch_config.get('default_api', 'unknown').lower()
            error_suffix = "_ERRORS" if batch_info['status'] == 'completed_with_errors' else ""
            output_filename = f"output_{base_name}_{api_name}_batch_{batch_id[:8]}{error_suffix}.csv"
            output_file_path = os.path.join(config.OUTPUT_DIR, output_filename)
            
            # Ensure output directory exists before generating
            os.makedirs(config.OUTPUT_DIR, exist_ok=True)

            success = translation_service.generate_export(batch_id, output_file_path)

            if success and os.path.exists(output_file_path):
                # Use absolute path for send_file for robustness
                abs_output_path = os.path.abspath(output_file_path)
                logger.info(f"Sending file: {abs_output_path}")
                return send_file(abs_output_path, as_attachment=True, download_name=output_filename)
            else:
                 logger.error(f"Failed to generate or find export file for batch {batch_id} at {output_file_path}")
                 return jsonify({"error": "Failed to generate export file."}), 500

        except Exception as e:
            logger.exception(f"Error during export for batch {batch_id}: {e}") # Use exception
            return jsonify({"error": "An unexpected error occurred during export."}), 500
            
    @app.route('/export_stages_report/<batch_id>')
    def export_stages_report(batch_id):
        """Generates and serves the detailed stages report CSV."""
        logger.info(f"Received request to export stages report for batch: {batch_id}")
        db_path = config.DATABASE_FILE
        
        # Verify batch exists and mode is appropriate
        batch_info = db_manager.get_batch_info(db_path, batch_id)
        if not batch_info:
            return jsonify({"error": "Batch not found"}), 404
        try:
            batch_config = json.loads(batch_info['config_details'] or '{}')
            mode = batch_config.get('mode')
            if mode not in ['THREE_STAGE', 'FOUR_STAGE']:
                logger.warning(f"Stages report requested for batch {batch_id} with mode {mode}, which is not supported.")
                return jsonify({"error": "Stages report only available for THREE_STAGE or FOUR_STAGE batches"}), 400
        except Exception as e:
            logger.error(f"Error reading batch config for {batch_id} during stages export: {e}")
            return jsonify({"error": "Failed to read batch configuration"}), 500

        # Define output path
        output_dir = config.OUTPUT_DIR
        safe_batch_id = batch_id.replace("-", "")[:8] # Short, safe ID for filename
        original_filename = batch_info['upload_filename']
        filename_base = os.path.splitext(original_filename)[0]
        output_filename = f"{filename_base}_batch_{safe_batch_id}_STAGES.csv"
        output_file_path = os.path.join(output_dir, output_filename)
        
        # Generate the report file
        success = translation_service.generate_stages_report(batch_id, output_file_path)
        
        if success and os.path.exists(output_file_path):
            logger.info(f"Successfully generated stages report: {output_file_path}")
            return send_file(output_file_path, as_attachment=True)
        else:
            logger.error(f"Failed to generate or find stages report for batch {batch_id} at {output_file_path}")
            return jsonify({"error": "Failed to generate stages report file"}), 500

    @app.route('/results/<batch_id>')
    def view_results(batch_id):
        """Displays the results of a translation batch for review."""
        logger.info(f"Received request to view results for batch {batch_id}")
        # --- Force DEBUG level for this specific route --- #
        logger.setLevel(logging.DEBUG) 
        logger.debug("Logger level explicitly set to DEBUG for /results route.")
        # --- End Force DEBUG level --- #
        db_path = config.DATABASE_FILE
        try:
            batch_info = db_manager.get_batch_info(db_path, batch_id)
            if not batch_info:
                return "Batch not found", 404 
            
            # Parse batch config for use in template
            batch_config = None
            if batch_info['config_details']:
                try: 
                    batch_config = json.loads(batch_info['config_details'])
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse config_details for batch {batch_id}")
            
            tasks_raw = db_manager.get_tasks_for_review(db_path, batch_id)
            tasks_list = []
            for task in tasks_raw:
                task_dict = dict(task)
                record_id = "N/A" # Default value
                raw_metadata_json = task_dict.get('metadata_json') # Get raw JSON
                logger.debug(f"Task {task_dict.get('task_id')}: Raw metadata_json: {raw_metadata_json}") # Log raw JSON
                try:
                    metadata = json.loads(raw_metadata_json or '{}')
                    logger.debug(f"Task {task_dict.get('task_id')}: Parsed metadata: {metadata}") # Log parsed dict
                    record_id = metadata.get("Record ID", "N/A") # Extract Record ID
                    if record_id == "N/A" and "Record ID" in metadata:
                        logger.warning(f"Task {task_dict.get('task_id')}: 'Record ID' key exists but value might be empty/null.")
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse metadata_json for task {task_dict.get('task_id')}")
                except Exception as e:
                     logger.warning(f"Error processing metadata for task {task_dict.get('task_id')}: {e}")
                
                task_dict['record_id'] = record_id # Add to dictionary
                tasks_list.append(task_dict)

            return render_template('results.html', 
                                   batch=batch_info, 
                                   tasks=tasks_list, 
                                   batch_config=batch_config) # Pass config too

        except Exception as e:
            logger.exception(f"Error fetching results for batch {batch_id}: {e}")
            return "An error occurred while fetching results.", 500

    @app.route('/review_task/<int:task_id>', methods=['POST'])
    def review_task(task_id):
        """Handles updates to a task's review status and approved translation."""
        logger.info(f"Received review update for task_id: {task_id}")
        db_path = config.DATABASE_FILE
        try:
            data = request.get_json()
            if not data:
                return jsonify({"error": "Invalid request body"}), 400

            new_review_status = data.get('review_status')
            approved_translation = data.get('approved_translation') 
            user_id = "webapp" # Placeholder for user ID

            # --- Validation --- #
            allowed_statuses = ['pending_review', 'approved_original', 'approved_edited', 'denied']
            if not new_review_status or new_review_status not in allowed_statuses:
                logger.warning(f"Invalid review status received: {new_review_status}")
                return jsonify({"error": "Invalid review status provided"}), 400
            
            if new_review_status == 'approved_edited' and approved_translation is None:
                 logger.warning(f"Review status 'approved_edited' received for task {task_id} but approved_translation is missing.")
                 # Allow empty string, but not None if status is edited
                 # return jsonify({"error": "Approved translation text is required for edited status"}), 400
                 # Let's allow saving empty string if user explicitly clears it?

            if new_review_status == 'denied' and approved_translation is not None:
                 logger.warning(f"Review status 'denied' received for task {task_id} but approved_translation is not None. Setting to None.")
                 approved_translation = None # Ensure it's null if denied
                 
            # --- End Validation --- #
            
            # Call DB manager to update
            db_manager.update_review_status(
                db_path, 
                task_id, 
                new_review_status, 
                approved_translation, 
                user_id
            )
            
            return jsonify({"success": True, "message": "Review status updated successfully"}), 200

        except Exception as e:
            logger.exception(f"Error updating review status for task {task_id}: {e}")
            return jsonify({"error": "An internal error occurred while saving the review."}), 500

    @app.route('/retranslate_task/<int:task_id>', methods=['POST'])
    def retranslate_task(task_id):
        """Handles request to re-translate a specific task with user guidance."""
        logger.info(f"Received re-translate request for task_id: {task_id}")
        db_path = config.DATABASE_FILE
        try:
            data = request.get_json()
            if not data or 'refinement_prompt' not in data:
                return jsonify({"error": "Invalid request body, missing 'refinement_prompt'"}), 400
            
            refinement_instruction = data.get('refinement_prompt').strip()
            if not refinement_instruction:
                return jsonify({"error": "Refinement instruction cannot be empty"}), 400

            # 1. Fetch current task data
            conn = db_manager.get_db_connection(db_path)
            task_info = conn.execute("SELECT * FROM TranslationTasks WHERE task_id = ?", (task_id,)).fetchone()
            conn.close()
            if not task_info:
                return jsonify({"error": "Task not found"}), 404
                
            lang_code = task_info['language_code']
            source_text = task_info['source_text']
            # Use approved_translation if available and not empty, otherwise use LLM final
            current_translation = task_info['approved_translation'] or task_info['final_translation'] or ""
            
            # 2. Construct re-translate prompt (using prompt_manager which handles overrides)
            try:
                # Re-use get_full_prompt logic if suitable, or adapt Stage 4 logic here
                # For now, let's assume a dedicated Stage 4 template loading
                stage4_template = prompt_manager.load_single_prompt_file(config.STAGE4_TEMPLATE_FILE)
                if not stage4_template:
                    raise ValueError("Stage 4 (Retranslate) template not found or failed to load.")
                
                # Need full ruleset for the language
                global_rules = prompt_manager.global_rules_content # Already loaded (hopefully)
                lang_template = prompt_manager.stage1_templates.get(lang_code) # Already loaded
                if not lang_template:
                    raise ValueError(f"Stage 1 template for language {lang_code} not loaded.")
                if not global_rules:
                     raise ValueError("Global rules not loaded.")
                     
                lang_name = config.LANGUAGE_NAME_MAP.get(lang_code, lang_code)
                lang_template = lang_template.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)
                full_ruleset = f"{lang_template}\n\n{global_rules}"

                final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION:..." # Add final instruction consistent with prompt_manager

                system_prompt = stage4_template.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)\
                                               .replace("<<RULES>>", full_ruleset)\
                                               .replace("<<SOURCE_TEXT>>", source_text or "")\
                                               .replace("<<CURRENT_TRANSLATION>>", current_translation)\
                                               .replace("<<REFINEMENT_INSTRUCTION>>", refinement_instruction)
                system_prompt += final_instruction
                
            except Exception as prompt_e:
                 logger.exception(f"Error constructing re-translate prompt for task {task_id}: {prompt_e}")
                 return jsonify({"error": f"Failed to build re-translate prompt: {prompt_e}"}), 500

            # 3. Determine API/Model (Use batch defaults for now)
            batch_info = db_manager.get_batch_info(db_path, task_info['batch_id'])
            api_to_use = config.DEFAULT_API
            model_to_use = None # Default model
            if batch_info and batch_info['config_details']:
                try:
                    batch_config = json.loads(batch_info['config_details'])
                    # Use S1 API/Model config from batch for re-translate?
                    api_to_use = batch_config.get('s1_api', config.DEFAULT_API)
                    model_to_use = batch_config.get('s1_model')
                except Exception:
                    logger.warning(f"Could not parse batch config for retranslate API selection, using defaults.")

            # 4. Call API
            # Get necessary API clients (handle potential missing keys) - REMOVED this logic as call_active_api handles it
            # api_clients_dict = {} 
            # try:
            #      if 'openai' in api_to_use.lower(): api_clients_dict['openai'] = api_clients.get_openai_client()
            #      if 'gemini' in api_to_use.lower(): api_clients_dict['gemini'] = api_clients.get_gemini_client()
            #      if 'perplexity' in api_to_use.lower(): api_clients_dict['perplexity'] = api_clients.get_perplexity_client()
            # except Exception as client_e:
            #      logger.error(f\"Failed to initialize API client for {api_to_use}: {client_e}\")
            #      return jsonify({\"error\": f\"API client setup failed: {client_e}\"}), 500
                 
            new_translation = translation_service.call_active_api(
                api_to_use,
                system_prompt,
                "Apply the refinement instruction.", # Simple user content
                row_identifier=f"{task_id}-RETRANSLATE",
                model_override=model_to_use
                # **api_clients_dict # Pass initialized clients - REMOVED
            )

            # 5. Update DB and respond
            if new_translation is not None and not new_translation.startswith("ERROR:BLOCKED:"):
                new_review_status = 'approved_edited'
                db_manager.update_review_status(db_path, task_id, new_review_status, new_translation, "webapp_retranslate")
                return jsonify({"success": True, "new_translation": new_translation, "new_status": new_review_status}), 200
            else:
                error_msg = new_translation or "Retranslate API call failed"
                logger.error(f"Retranslate failed for task {task_id}: {error_msg}")
                return jsonify({"error": error_msg}), 500

        except Exception as e:
            logger.exception(f"Error retranslating task {task_id}: {e}")
            return jsonify({"error": "An internal error occurred during re-translation."}), 500
            
    # --- Batch History Route --- #
    @app.route('/history')
    def batch_history():
        """Displays a list of past translation batches."""
        logger.info("Received request for /history")
        db_path = config.DATABASE_FILE
        try:
            batches_raw = db_manager.get_all_batches_for_history(db_path)
            batches_list = [dict(batch) for batch in batches_raw]
            # Convert timestamp strings if needed for display formatting? 
            # Tabulator can handle ISO format though.
            return render_template('history.html', batches=batches_list)
        except Exception as e:
            logger.exception(f"Error fetching batch history: {e}")
            # Render history page with an error message? Or redirect?
            # Using flash for error message display
            flash(f'Error loading batch history: {e}', 'danger') 
            return render_template('history.html', batches=[]) # Pass empty list

    # --- Rules Routes --- 
    @app.route('/rules')
    def list_rules():
        """Displays a list of available rule files and their override status."""
        logger.info("Received request for /rules")
        try:
            rules_status = get_rule_files() # Now returns status dict
            return render_template('rules_list.html', rules_status=rules_status)
        except Exception as e:
            logger.exception("Error getting rule files list.")
            flash(f'Error loading rules list: {e}', 'danger')
            return render_template('rules_list.html', rules_status={}) # Pass empty dict on error
            
    @app.route('/rules/view/<path:filename>')
    def view_rule(filename):
        """Displays the effective content (user override or system base) of a rule file."""
        logger.info(f"Received request to view effective rule: {filename}")

        system_file_path = os.path.abspath(os.path.join(SYSTEM_PROMPT_DIR, filename))
        user_file_path = os.path.abspath(os.path.join(USER_PROMPT_DIR, filename))
        
        # Basic security check: Ensure the filename doesn't try to escape intended dirs
        if '..' in filename or filename.startswith('/'):
             logger.warning(f"Potentially insecure filename detected: {filename}")
             flash("Invalid filename.", "danger")
             return redirect(url_for('list_rules'))

        content = None
        status = "Not Found" # e.g., Base Only, Override Exists, User Only (if implemented), Not Found
        effective_path_type = None # 'user' or 'system'

        # Check user override first
        # Ensure user path is within the project's USER_PROMPT_DIR
        if os.path.exists(user_file_path) and user_file_path.startswith(os.path.abspath(USER_PROMPT_DIR)):
            try:
                with open(user_file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                status = "User Override" 
                effective_path_type = 'user'
                logger.info(f"Displaying user override for {filename}")
            except Exception as e:
                logger.exception(f"Error reading user override file {user_file_path}: {e}")
                flash(f"Error reading user override for '{filename}': {e}", "danger")
                return redirect(url_for('list_rules'))
        # Then check system base
        # Ensure system path is within the project's SYSTEM_PROMPT_DIR
        elif os.path.exists(system_file_path) and system_file_path.startswith(os.path.abspath(SYSTEM_PROMPT_DIR)):
             try:
                with open(system_file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                status = "System Base" 
                effective_path_type = 'system'
                logger.info(f"Displaying system base for {filename}")
             except Exception as e:
                logger.exception(f"Error reading system base file {system_file_path}: {e}")
                flash(f"Error reading system base file '{filename}': {e}", "danger")
                return redirect(url_for('list_rules'))
        # If neither exists (or path is invalid)
        else:
             logger.warning(f"Rule file '{filename}' not found in system or user directories, or invalid path.")
             flash(f"Rule file '{filename}' not found.", "warning")
             return redirect(url_for('list_rules'))

        # Get description (based on filename, could be refined)
        all_rules_status = get_rule_files()
        description = all_rules_status.get(filename, {}).get('description', filename)
        # Determine if a base version actually exists for linking purposes
        has_base_file = os.path.exists(system_file_path) and system_file_path.startswith(os.path.abspath(SYSTEM_PROMPT_DIR))

        return render_template('rules_view.html',
                               filename=filename,
                               description=description,
                               content=content,
                               status=status, # Pass 'User Override' or 'System Base'
                               has_base_file=has_base_file) # Indicate if system base exists

    @app.route('/rules/view_base/<path:filename>')
    def view_base_rule(filename):
        """Explicitly displays the system base version of a rule file."""
        logger.info(f"Received request to view BASE rule: {filename}")
        system_file_path = os.path.abspath(os.path.join(SYSTEM_PROMPT_DIR, filename))
        
        # Security check
        if '..' in filename or filename.startswith('/') or not system_file_path.startswith(os.path.abspath(SYSTEM_PROMPT_DIR)):
             logger.warning(f"Attempt to access invalid base file path: {filename}")
             flash("Invalid file path for base rule.", "danger")
             return redirect(url_for('list_rules'))
             
        content = None
        status = "System Base"
        try:
            if os.path.exists(system_file_path):
                 with open(system_file_path, 'r', encoding='utf-8') as f:
                     content = f.read()
            else:
                 flash(f"System base file '{filename}' not found.", "warning")
                 return redirect(url_for('list_rules'))
        except Exception as e:
             logger.exception(f"Error reading system base file {system_file_path} for view_base: {e}")
             flash(f"Error reading system base file '{filename}': {e}", "danger")
             return redirect(url_for('list_rules'))
             
        all_rules_status = get_rule_files()
        description = all_rules_status.get(filename, {}).get('description', filename)
        # Indicate this is the base view and that editing is not directly possible from here
        return render_template('rules_view.html',
                               filename=filename,
                               description=f"{description} (Base Version)",
                               content=content,
                               status=status, 
                               is_base_view=True) # Flag for template logic


    @app.route('/rules/edit/<path:filename>', methods=['GET', 'POST'])
    def edit_rule(filename):
        """Handles viewing the edit form (loads effective version) and saving changes (always to user_prompts/)."""
        logger.info(f"Received request to edit rule: {filename} (Method: {request.method})")

        system_file_path = os.path.abspath(os.path.join(SYSTEM_PROMPT_DIR, filename))
        user_file_path = os.path.abspath(os.path.join(USER_PROMPT_DIR, filename))
        target_save_path = user_file_path # Always save edits to the user directory
        target_archive_dir = USER_ARCHIVE_DIR

        # Basic security check on filename itself might be good?
        if '..' in filename or filename.startswith('/'):
             logger.warning(f"Potentially insecure filename detected for edit: {filename}")
             flash("Invalid filename.", "danger")
             return redirect(url_for('list_rules'))

        # Ensure save directory is within project bounds
        if not target_save_path.startswith(os.path.abspath(USER_PROMPT_DIR)):
            logger.error(f"Invalid save path generated for edit: {target_save_path}")
            flash("Error: Invalid target file path for saving.", "danger")
            return redirect(url_for('list_rules'))

        # --- Handle POST Request (Saving Changes to user_prompts/) --- 
        if request.method == 'POST':
            try:
                edited_content = request.form.get('edited_content')
                if edited_content is None: # Check if the form field exists
                    raise ValueError("Form data missing 'edited_content'.")

                # Ensure target directories exist
                os.makedirs(USER_PROMPT_DIR, exist_ok=True)
                os.makedirs(target_archive_dir, exist_ok=True)

                # 1. Archive the *current user version* if it exists
                if os.path.exists(target_save_path):
                    archive_filename = f"{filename}_{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
                    archive_file_path = os.path.join(target_archive_dir, archive_filename)
                    try:
                        # Use os.replace for atomicity if possible? Or stick to read/write
                        # os.rename might be better than read/write for archiving
                        # shutil.copy2(target_save_path, archive_file_path)
                        with open(target_save_path, 'r', encoding='utf-8') as current_file, \
                             open(archive_file_path, 'w', encoding='utf-8') as archive_file:
                             archive_file.write(current_file.read())
                        logger.info(f"Archived current user version of {filename} to {archive_filename} in {target_archive_dir}")
                    except Exception as archive_e:
                         # Log archive error but proceed with saving the main file
                         logger.error(f"Failed to archive {target_save_path}: {archive_e}")
                         flash(f"Warning: Could not archive previous version of {filename}. Proceeding with save.", "warning")
                else:
                    logger.info(f"No existing user file at {target_save_path} to archive. Saving new user override.")

                # 2. Save the new content to the user file
                with open(target_save_path, 'w', encoding='utf-8') as f:
                    f.write(edited_content)
                logger.info(f"Successfully saved user override changes to {target_save_path}")
                flash(f"User override for '{filename}' saved successfully.", "success")
                return redirect(url_for('view_rule', filename=filename)) # Redirect back to view (will show override)

            except Exception as e:
                logger.exception(f"Error saving user override file {filename}: {e}")
                flash(f"Error saving user override file '{filename}': {e}", "danger")
                # Stay on edit page, reload description
                all_rules_status = get_rule_files()
                description = all_rules_status.get(filename, {}).get('description', filename)
                # Pass back the attempted content and indicate if it was the first edit attempt
                return render_template('rules_edit.html', filename=filename, description=description, content=edited_content, is_editing_base=(not os.path.exists(user_file_path))), 500 # Pass back is_editing_base status

        # --- Handle GET Request (Show Edit Form with effective content) --- 
        else: # request.method == 'GET'
            content_to_edit = None
            is_editing_base = False # Flag to tell template if loading from system initially
            effective_path_type = None # 'user' or 'system'

            # Check user override first
            if os.path.exists(user_file_path) and user_file_path.startswith(os.path.abspath(USER_PROMPT_DIR)):
                 try:
                     with open(user_file_path, 'r', encoding='utf-8') as f:
                         content_to_edit = f.read()
                     effective_path_type = 'user'
                     logger.info(f"Editing user override for {filename}")
                 except Exception as e:
                     logger.exception(f"Error reading user override file {user_file_path} for editing: {e}")
                     flash(f"Error reading user override for '{filename}': {e}", "danger")
                     return redirect(url_for('list_rules'))
            # Then check system base if no user override
            elif os.path.exists(system_file_path) and system_file_path.startswith(os.path.abspath(SYSTEM_PROMPT_DIR)):
                 try:
                     with open(system_file_path, 'r', encoding='utf-8') as f:
                         content_to_edit = f.read()
                     effective_path_type = 'system'
                     is_editing_base = True # Mark that we loaded from system
                     logger.info(f"Editing system base for {filename} (will save to user_prompts)")
                 except Exception as e:
                     logger.exception(f"Error reading system base file {system_file_path} for editing: {e}")
                     flash(f"Error reading system base file '{filename}': {e}", "danger")
                     return redirect(url_for('list_rules'))
            # If neither exists
            else:
                 logger.warning(f"Rule file '{filename}' not found in system or user directories for editing.")
                 flash(f"Rule file '{filename}' not found.", "warning")
                 return redirect(url_for('list_rules'))

            all_rules_status = get_rule_files()
            description = all_rules_status.get(filename, {}).get('description', filename)

            return render_template('rules_edit.html',
                                   filename=filename,
                                   description=description,
                                   content=content_to_edit,
                                   is_editing_base=is_editing_base) # Pass flag to template
    # --- End Rules Routes ---

    # --- NEW: Route to Revert Rule Override --- 
    @app.route('/rules/revert/<path:filename>', methods=['POST'])
    def revert_rule(filename):
        """Reverts a user override back to the system default by archiving the user file."""
        logger.info(f"Received request to revert rule: {filename}")

        user_file_path = os.path.abspath(os.path.join(USER_PROMPT_DIR, filename))
        target_archive_dir = USER_ARCHIVE_DIR

        # Security Checks
        if '..' in filename or filename.startswith('/') or not user_file_path.startswith(os.path.abspath(USER_PROMPT_DIR)):
            logger.warning(f"Potentially insecure filename detected for revert: {filename}")
            flash("Invalid filename provided.", "danger")
            return redirect(url_for('list_rules'))

        if not os.path.exists(user_file_path):
            flash(f"No user override exists for '{filename}' to revert.", "warning")
            return redirect(url_for('view_rule', filename=filename))

        try:
            # Ensure archive directory exists
            os.makedirs(target_archive_dir, exist_ok=True)

            # Create archive filename
            archive_filename = f"{os.path.splitext(filename)[0]}_{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
            archive_file_path = os.path.join(target_archive_dir, archive_filename)

            # Move the user file to the archive
            os.rename(user_file_path, archive_file_path) # More atomic than copy/delete
            
            logger.info(f"Successfully reverted {filename} by archiving override to {archive_file_path}")
            flash(f"Successfully reverted '{filename}' to system base version.", "success")

        except OSError as e:
             logger.exception(f"Error reverting rule {filename}: {e}")
             flash(f"Error reverting rule '{filename}': {e}", "danger")
        except Exception as e:
             logger.exception(f"Unexpected error reverting rule {filename}: {e}")
             flash(f"An unexpected error occurred while reverting '{filename}'.", "danger")

        # Redirect back to the view page, which will now show the base version
        return redirect(url_for('view_rule', filename=filename))
    # --- End Revert Route --- 

    # --- Admin Routes --- 
    @app.route('/admin')
    def admin_page():
        """Displays the admin page for Vector Store management."""
        logger.info("Received request for /admin")
        try:
            vector_store_sets = db_manager.get_vector_store_sets(config.DATABASE_FILE)
        except Exception as e:
            logger.exception("Error fetching vector store sets for admin page.")
            flash(f'Error loading vector store sets: {e}', 'danger')
            vector_store_sets = []
        return render_template('admin.html', vector_store_sets=vector_store_sets)

    @app.route('/admin/prepare_vs', methods=['POST'])
    def prepare_vector_stores():
        """Handles CSV upload, creates DB entries, and starts background processing."""
        logger.info("Received POST request for /admin/prepare_vs")
        file_path = None # Initialize for finally block
        set_id = None    # Initialize for cleanup logic
        try:
            if 'full_translation_csv' not in request.files:
                flash('No file part in the request.', 'warning')
                logger.warning("Prepare VS: No file part in request.")
                return redirect(url_for('admin_page'))

            file = request.files['full_translation_csv']
            if file.filename == '':
                flash('No selected file.', 'warning')
                logger.warning("Prepare VS: No file selected.")
                return redirect(url_for('admin_page'))

            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file_path = os.path.join(UPLOAD_FOLDER, filename)
                logger.info(f"Prepare VS: Processing uploaded file: {filename}")

                # Avoid overwriting
                if os.path.exists(file_path):
                     flash(f'File {filename} already exists. Please rename and re-upload.', 'warning')
                     logger.warning(f"Prepare VS: File {filename} already exists.")
                     return redirect(url_for('admin_page'))

                file.save(file_path)
                logger.info(f"Prepare VS: File saved to {file_path}")

                notes = request.form.get('notes', '') # Get notes from form

                # 1. Add Set to DB
                set_id = db_manager.add_vector_store_set(config.DATABASE_FILE, filename, notes)
                if set_id is None:
                    flash('Database error creating vector store set entry.', 'danger')
                    logger.error(f"Prepare VS: Failed to add VectorStoreSet to DB for {filename}")
                    # No need to delete set if it wasn't created
                    raise Exception("Failed to create DB set entry.") # Raise to trigger cleanup

                logger.info(f"Prepare VS: Created VectorStoreSet ID: {set_id} for {filename}")

                # 2. Parse Header and Add Mappings
                try:
                    df_header = pd.read_csv(file_path, nrows=0) # Read only header
                    target_lang_cols = [col for col in df_header.columns if re.match(r'^tg_([a-zA-Z]{2,3}[A-Z]{2})$', col)]
                    logger.info(f"Prepare VS: Found target language columns for Set {set_id}: {target_lang_cols}")

                    if not target_lang_cols:
                         flash('No target language columns (e.g., tg_frFR) found in CSV header.', 'danger')
                         logger.error(f"Prepare VS: No target columns found in {filename} for Set {set_id}")
                         # Cleanup handled by exception/finally block
                         raise ValueError("No target language columns found in CSV.")

                    # Check for source column before proceeding
                    if SOURCE_COLUMN_NAME not in df_header.columns:
                        flash(f"Source column '{SOURCE_COLUMN_NAME}' not found in CSV header.", 'danger')
                        logger.error(f"Prepare VS: Source column '{SOURCE_COLUMN_NAME}' not found in {filename} for Set {set_id}")
                        raise ValueError(f"Source column '{SOURCE_COLUMN_NAME}' missing.")

                    mappings_added = 0
                    added_languages_in_set = set() # Keep track of languages added for this set
                    for col_name in target_lang_cols:
                        match = re.match(r'^tg_([a-zA-Z]{2,3}[A-Z]{2})$', col_name)
                        lang_code = match.group(1)

                        # Check for duplicates within this set processing
                        if lang_code in added_languages_in_set:
                            logger.warning(f"Prepare VS: Duplicate language code '{lang_code}' derived from column '{col_name}' for Set {set_id}. Skipping duplicate mapping.")
                            continue # Skip this column

                        mapping_id = db_manager.add_vector_store_mapping(config.DATABASE_FILE, set_id, lang_code, col_name)
                        if mapping_id:
                            mappings_added += 1
                            added_languages_in_set.add(lang_code) # Add to set only on successful DB add
                        else:
                            logger.error(f"Prepare VS: Failed to add mapping for {lang_code} ({col_name}) for Set {set_id}")
                            # If one mapping fails, should we abort the whole thing?

                    logger.info(f"Prepare VS: Successfully added {mappings_added} mappings for Set {set_id}")

                except ValueError as ve:
                    # Reraise specific ValueErrors from checks above
                    raise ve
                except Exception as e:
                    flash(f'Error parsing CSV header or adding mappings: {e}', 'danger')
                    logger.exception(f"Prepare VS: Error parsing CSV header/adding mappings for {filename}, Set {set_id}")
                    raise Exception("CSV Header/Mapping Error") # Trigger cleanup

                # 3. Start Background Thread
                logger.info(f"Prepare VS: Starting background processing thread for Set {set_id}")
                thread = threading.Thread(target=_process_vector_store_set_background,
                                          args=(app.app_context(), set_id, file_path),
                                          daemon=True)
                thread.start()
                # Reset file_path after starting thread so finally block doesn't delete it
                file_path_to_keep = file_path
                file_path = None 

                flash(f'Successfully uploaded {filename}. Vector Store creation started in the background for Set ID {set_id}.', 'success')
                return redirect(url_for('admin_page'))

            else:
                flash('Invalid file type. Only CSV files are allowed.', 'danger')
                logger.warning(f"Prepare VS: Invalid file type uploaded: {file.filename}")
                # No file saved, no DB entry, just redirect
                return redirect(url_for('admin_page'))

        except Exception as e:
            # General error handling for the entire try block
            # Log the specific exception that occurred
            logger.exception(f"Prepare VS: Error during file upload/preparation for Set ID {set_id or 'N/A'}: {e}")
            # Use a more generic flash message unless it was a specific ValueError we raised
            if isinstance(e, ValueError):
                # We already flashed a specific message for these
                pass
            elif isinstance(e, FileExistsError):
                 # Already flashed
                 pass
            else:
                 flash(f'An unexpected error occurred: {e}', 'danger')
            # Cleanup happens in finally block
            return redirect(url_for('admin_page'))
            
        finally:
            # Cleanup: Delete the saved file and DB entries if an error occurred *before* the thread started
            if file_path and os.path.exists(file_path):
                logger.warning(f"Prepare VS: Cleaning up file due to error: {file_path}")
                try:
                    os.remove(file_path)
                except OSError as rm_e:
                    logger.error(f"Prepare VS: Error removing file during cleanup: {rm_e}")
            if set_id and file_path: # Only delete DB if set was created AND file cleanup needed (i.e., thread didn't start)
                logger.warning(f"Prepare VS: Cleaning up DB entries for Set ID {set_id} due to error before thread start.")
                db_manager.delete_vector_store_set(config.DATABASE_FILE, set_id)


    @app.route('/admin/activate_vs/<int:set_id>', methods=['POST'])
    def activate_vector_store_set(set_id):
        """Activates the specified Vector Store Set."""
        logger.info(f"Received request to activate Vector Store Set ID: {set_id}")
        try:
            # Check if the set has all mappings completed first? (Optional but safer)
            mappings = db_manager.get_mappings_for_set(config.DATABASE_FILE, set_id)
            if not mappings: 
                 flash(f"Cannot activate Set {set_id}: No language mappings found.", "warning")
                 logger.warning(f"Activation failed for Set {set_id}: No mappings.")
                 return redirect(url_for('admin_page'))
                 
            all_complete = all(m['status'] == 'completed' for m in mappings)
            if not all_complete:
                 failed_langs = [m['language_code'] for m in mappings if m['status'] != 'completed']
                 flash(f"Cannot activate Set {set_id}: Not all language stores are complete. Failed/Pending: {failed_langs}", "warning")
                 logger.warning(f"Activation failed for Set {set_id}: Not all mappings complete ({failed_langs}).")
                 return redirect(url_for('admin_page'))

            # Proceed with activation
            activated = db_manager.activate_set(config.DATABASE_FILE, set_id)
            if activated:
                flash(f"Vector Store Set {set_id} activated successfully.", "success")
                logger.info(f"Successfully activated Set {set_id} via admin UI.")
            else:
                flash(f"Failed to activate Vector Store Set {set_id}. Set might not exist or DB error occurred.", "danger")
                logger.error(f"Failed to activate Set {set_id} via admin UI (db_manager.activate_set returned False).")
        except Exception as e:
            logger.exception(f"Error activating Vector Store Set {set_id}: {e}")
            flash(f'An unexpected error occurred during activation: {e}', 'danger')

        return redirect(url_for('admin_page'))

    # --- NEW: Endpoint for AJAX status checks ---
    @app.route('/admin/set_status/<int:set_id>')
    def get_vector_store_set_status(set_id):
        """Returns the status summary of mappings for a given VectorStoreSet ID."""
        logger.debug(f"Received status request for Set ID: {set_id}")
        try:
            mappings = db_manager.get_mappings_for_set(config.DATABASE_FILE, set_id)
            if mappings is None: # Error fetching mappings
                return jsonify({"error": "Failed to fetch mapping status"}), 500
            if not mappings: # Set exists but has no mappings (shouldn't happen in normal flow)
                 # Check if the set itself exists, provide some info
                 set_info = db_manager.get_vector_store_sets_by_id(config.DATABASE_FILE, set_id) # Need this new DB function
                 if set_info:
                     return jsonify({"status": "No Mappings", "total": 0, "is_active": set_info['is_active']}), 200
                 else:
                     return jsonify({"error": "Set not found"}), 404

            total_mappings = len(mappings)
            status_counts = {
                'pending': 0,
                'processing': 0,
                'completed': 0,
                'failed': 0
            }
            for m in mappings:
                status_counts[m['status']] = status_counts.get(m['status'], 0) + 1

            # Determine overall status text
            overall_status_text = "Unknown"
            if status_counts['processing'] > 0:
                overall_status_text = f"Processing ({status_counts['completed']}/{total_mappings})"
            elif status_counts['pending'] > 0 and total_mappings > 0 and status_counts['completed'] == 0 and status_counts['failed'] == 0:
                 overall_status_text = "Pending"
            elif status_counts['completed'] == total_mappings and total_mappings > 0:
                overall_status_text = "Completed"
            elif status_counts['failed'] > 0:
                overall_status_text = f"Failed ({status_counts['failed']} Errors)" 
            elif total_mappings == 0: # Should be caught above, but belt-and-suspenders
                 overall_status_text = "No Mappings"
                 
            # Get the set's active status as well
            set_info = db_manager.get_vector_store_sets_by_id(config.DATABASE_FILE, set_id) # Need this new DB function
            is_active = set_info['is_active'] if set_info else 0 

            response_data = {
                "status": overall_status_text,
                "total": total_mappings,
                "completed": status_counts['completed'],
                "processing": status_counts['processing'],
                "pending": status_counts['pending'],
                "failed": status_counts['failed'],
                "is_active": is_active
            }
            logger.debug(f"Returning status for Set ID {set_id}: {response_data}")
            return jsonify(response_data), 200

        except Exception as e:
            logger.exception(f"Error fetching status for Set ID {set_id}: {e}")
            return jsonify({"error": "An internal server error occurred"}), 500
    # --- End AJAX status endpoint ---

    # --- API Endpoint for Frontend --- #
    @app.route('/get_available_languages', methods=['GET'])
    def get_available_languages_route():
        """Returns a JSON list of language codes for which rule files exist."""
        logger.info("Received request for /get_available_languages")
        try:
            languages = get_available_rule_languages()
            return jsonify({"available_languages": languages})
        except Exception as e:
            logger.exception("Error in /get_available_languages route")
            return jsonify({"error": "Failed to retrieve available languages"}), 500
    # --- End API Endpoint --- #

    @app.route('/placeholder') # Keep or remove this placeholder?
    def placeholder():
        return "Route not implemented yet.", 501
        
    # <<< Add Cache Control Headers (Always) >>>
    @app.after_request
    def add_header(response):
        # Apply cache-disabling headers regardless of debug mode
        # if app.debug: <<< REMOVE CHECK >>>
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        # Setting Cache-Control again might override the previous, ensure all directives are intended.
        # Let's combine them or choose the most effective set.
        # Using 'no-cache, no-store, must-revalidate' is generally robust.
        # Removing the second Cache-Control set for clarity unless specifically needed.
        # response.headers['Cache-Control'] = 'public, max-age=0' 
        return response
    # <<< End Cache Control >>>
    
    return app

# Allow running directly for development
if __name__ == '__main__':
    app = create_app()
    # Note: Flask's dev server is not suitable for production.
    # Use Waitress or Gunicorn/uWSGI + Nginx for deployment.
    # Debug mode enables auto-reloading.
    app.run(debug=True, threaded=True) # Set threaded=True generally for dev
