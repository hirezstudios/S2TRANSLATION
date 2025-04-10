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

# Import backend modules
from src import config
from src import db_manager
from src import translation_service
from src import prompt_manager
from src import api_clients

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s') # Changed level to DEBUG
logger = logging.getLogger(__name__)

# --- Explicitly set levels for module loggers --- #
logging.getLogger('src.translation_service').setLevel(logging.DEBUG)
logging.getLogger('src.db_manager').setLevel(logging.DEBUG)
logging.getLogger('src.prompt_manager').setLevel(logging.INFO) # Set prompt_manager to INFO for less noise
logger.info("Log levels configured.")
# --- End Explicit level setting --- #

# --- Constants for Rules Feature ---
SYSTEM_PROMPT_DIR = config.SYSTEM_PROMPT_DIR # Use path from config
SYSTEM_ARCHIVE_DIR = config.ARCHIVE_DIR # Use path from config
USER_PROMPT_DIR = "user_prompts"
USER_ARCHIVE_DIR = os.path.join(USER_PROMPT_DIR, "archive")

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


# --- Flask App Setup ---
def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.urandom(24) # Needed for session/flash
    
    # Ensure necessary directories exist
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    # os.makedirs(config.INPUT_CSV_DIR, exist_ok=True) # Removed, using temp_uploads instead
    # os.makedirs(ARCHIVE_DIR, exist_ok=True) # Use constant - System archive handled via config?
    # Let's explicitly ensure User archive dir exists on startup too
    os.makedirs(USER_ARCHIVE_DIR, exist_ok=True)
    db_manager.initialize_database(config.DATABASE_FILE)
    prompt_manager.load_prompts()

    @app.route('/')
    def index():
        # Pass available APIs for dynamic population? Or handle in JS?
        # Let's pass defaults for now.
        default_apis = {
            'ONE_STAGE': config.DEFAULT_API,
            'S1': config.STAGE1_API,
            'S2': config.STAGE2_API,
            'S3': config.STAGE3_API
        }
        return render_template('index.html', valid_apis=config.VALID_APIS, default_apis=default_apis)

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
            if 'file' not in request.files:
                return jsonify({"error": "No file part"}), 400
            file = request.files['file']
            if not file or not file.filename:
                return jsonify({"error": "No selected file or filename missing"}), 400
            
            # Ensure filename is secure
            filename = secure_filename(file.filename)
            
            # Get form data
            selected_languages = request.form.getlist('languages') # Assuming name="languages"
            mode = request.form.get('mode', config.TRANSLATION_MODE) # Default to config if not provided
            one_stage_api = request.form.get('one_stage_api', config.DEFAULT_API)
            one_stage_model = request.form.get('one_stage_model') or None 
            s1_api = request.form.get('s1_api', config.STAGE1_API)
            s1_model = request.form.get('s1_model') or None
            s2_api = request.form.get('s2_api', config.STAGE2_API)
            s2_model = request.form.get('s2_model') or None
            s3_api = request.form.get('s3_api', config.STAGE3_API)
            s3_model = request.form.get('s3_model') or None
            
            logger.info(f"Form Data Received - Mode: {mode}, Languages: {selected_languages}")
            if not selected_languages:
                 return jsonify({"error": "No languages selected"}), 400

            # Save file temporarily
            temp_dir = "temp_uploads"
            os.makedirs(temp_dir, exist_ok=True)
            temp_filename = f"{uuid.uuid4()}_{filename}" # Use secure filename
            file_path = os.path.join(temp_dir, temp_filename)
            file.save(file_path)
            logger.info(f"Temporarily saved uploaded file to {file_path}")
            
            # Construct mode_config dictionary based on selected mode
            if mode == "ONE_STAGE":
                mode_config = {
                    "mode": mode, 
                    "languages": selected_languages, 
                    "default_api": one_stage_api, 
                    "s1_api": one_stage_api, "s1_model": one_stage_model,
                    "s2_api": None, "s2_model": None,
                    "s3_api": None, "s3_model": None,
                 }
            elif mode == "THREE_STAGE":
                 mode_config = {
                    "mode": mode, 
                    "languages": selected_languages, 
                    "default_api": one_stage_api, # Pass the API selected if mode was ONE_STAGE previously
                    "s1_api": s1_api,
                    "s2_api": s2_api,
                    "s3_api": s3_api,
                    "s1_model": s1_model, 
                    "s2_model": s2_model,
                    "s3_model": s3_model,
                }
            else:
                logger.error(f"Invalid mode received: {mode}")
                return jsonify({"error": "Invalid translation mode selected"}), 400
            
            # Call backend prepare_batch
            # Pass the actual filename for storage, but use temp path for processing
            batch_id = translation_service.prepare_batch(file_path, filename, selected_languages, mode_config)

            if batch_id:
                logger.info(f"Batch {batch_id} prepared. Starting background thread...")
                thread = threading.Thread(
                    target=translation_service.run_batch_background, 
                    args=(batch_id,)
                )
                thread.daemon = True # Allow app to exit even if thread hangs (optional)
                thread.start()
                return jsonify({"message": "Job started successfully", "batch_id": batch_id}), 200
            else:
                logger.error("Batch preparation failed (prepare_batch returned None).")
                # Clean up temp file if batch prep failed
                try: os.remove(file_path) 
                except OSError as e: logger.warning(f"Could not remove temp file {file_path}: {e}")
                return jsonify({"error": "Batch preparation failed. Check logs and input file."}) , 500
                
        except Exception as e:
             logger.exception(f"Error in /start_job: {e}") # Use exception for traceback
             return jsonify({"error": "An unexpected error occurred processing the request."}), 500
             
    # Add status endpoint
    @app.route('/status/<batch_id>')
    def batch_status(batch_id):
        logger.debug(f"Received status request for batch {batch_id}")
        db_path = config.DATABASE_FILE # Get DB path
        try:
            batch_info = db_manager.get_batch_info(db_path, batch_id)
            if not batch_info:
                return jsonify({"error": "Batch not found"}), 404
            batch_status_db = batch_info['status'] 
            # Get mode from config details
            mode = "UNKNOWN"
            try:
                 if batch_info['config_details']:
                     batch_config = json.loads(batch_info['config_details'])
                     mode = batch_config.get('mode', 'UNKNOWN')
            except Exception:
                 logger.warning(f"Could not determine mode from config for batch {batch_id}")
            
            # Use count function for efficiency
            total_tasks = db_manager.count_tasks_for_batch(db_path, batch_id)
            # Use specific statuses for counts
            completed_tasks = db_manager.count_tasks_by_status(db_path, batch_id, 'completed') # Need this function
            error_tasks = db_manager.count_tasks_by_status(db_path, batch_id, 'error') # Need this function
            
            # Calculate pending/running if needed for progress bar
            # pending_tasks = db_manager.count_tasks_by_status(db_path, batch_id, 'pending')
            # running_tasks = db_manager.count_tasks_by_status(db_path, batch_id, 'running')

            return jsonify({
                "batch_id": batch_id,
                "batch_status": batch_status_db, # Use specific name
                "mode": mode, # Add mode to response
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "error_tasks": error_tasks
            })
        except Exception as e:
            logger.exception(f"Error fetching status for batch {batch_id}: {e}") # Use exception
            return jsonify({"error": "Failed to fetch status"}), 500
            
    # Add Export route
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
            
    @app.route('/export_stages/<batch_id>')
    def export_stages_report(batch_id):
        """Generates and returns the detailed stages CSV report."""
        logger.info(f"Received stages report export request for batch {batch_id}")
        db_path = config.DATABASE_FILE
        try:
            # Check batch exists and was THREE_STAGE
            batch_info = db_manager.get_batch_info(db_path, batch_id)
            if not batch_info:
                return jsonify({"error": "Batch not found"}), 404
            if batch_info['config_details']:
                 try:
                     batch_config = json.loads(batch_info['config_details'])
                     if batch_config.get('mode') != 'THREE_STAGE':
                         return jsonify({"error": "Stages report only available for THREE_STAGE batches"}), 400
                 except Exception:
                     pass # Ignore config parsing error here, generate_stages_report will handle it
            
            # Define report filename
            original_filename = batch_info['upload_filename'] or "unknown_file.csv"
            base_name = os.path.splitext(original_filename)[0]
            report_filename = f"stages_report_{base_name}_batch_{batch_id[:8]}.csv"
            report_file_path = os.path.join(config.OUTPUT_DIR, report_filename)
            
            # Ensure output directory exists
            os.makedirs(config.OUTPUT_DIR, exist_ok=True)
            
            success = translation_service.generate_stages_report(batch_id, report_file_path)

            if success and os.path.exists(report_file_path):
                abs_report_path = os.path.abspath(report_file_path)
                logger.info(f"Sending stages report file: {abs_report_path}")
                return send_file(abs_report_path, as_attachment=True, download_name=report_filename)
            else:
                 # generate_stages_report logs specific errors
                 return jsonify({"error": "Failed to generate stages report file."}), 500

        except Exception as e:
            logger.exception(f"Error during stages report export for batch {batch_id}: {e}")
            return jsonify({"error": "An unexpected error occurred during stages report export."}), 500
            
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

    @app.route('/placeholder') # Keep or remove this placeholder?
    def placeholder():
        return "Route not implemented yet.", 501
        
    return app

# Allow running directly for development
if __name__ == '__main__':
    app = create_app()
    # Note: Flask's dev server is not suitable for production.
    # Use Waitress or Gunicorn/uWSGI + Nginx for deployment.
    # Debug mode enables auto-reloading.
    app.run(debug=True, threaded=True) # Set threaded=True generally for dev
