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
logger.info("Log levels for src.translation_service and src.db_manager set to DEBUG.")
# --- End Explicit level setting --- #

# --- Constants for Rules Feature ---
PROMPT_DIR = os.path.join(os.path.dirname(__file__), 'system_prompts')
ARCHIVE_DIR = os.path.join(PROMPT_DIR, 'archive')

# --- Helper function to get rule files ---
def get_rule_files():
    """Scans the system_prompts directory for rule files."""
    rule_files = {}
    
    # Define patterns for specific important files and language files
    patterns = {
        'global.md': 'Global Rules',
        'stage2_evaluate_template.md': 'Stage 2 Evaluate Template',
        'stage3_refine_template.md': 'Stage 3 Refine Template',
        'tg_*.md': 'Language Specific Rules' # Placeholder description
    }

    # Ensure PROMPT_DIR exists
    if not os.path.isdir(PROMPT_DIR):
        logger.error(f"Prompt directory not found: {PROMPT_DIR}")
        return {} # Return empty if directory doesn't exist

    # Find files matching patterns
    for filename_pattern, description in patterns.items():
        full_pattern = os.path.join(PROMPT_DIR, filename_pattern)
        found_files = glob.glob(full_pattern)
        
        for file_path in found_files:
            # Make sure we don't list files from the archive directory itself
            if os.path.dirname(file_path) == PROMPT_DIR:
                base_filename = os.path.basename(file_path)
                
                current_description = description # Default description
                # Try to get a more specific description for language files
                if filename_pattern == 'tg_*.md':
                    lang_code_match = re.match(r"tg_([a-zA-Z]{2}[A-Z]{2})\.md", base_filename)
                    if lang_code_match:
                        lang_code = lang_code_match.group(1)
                        # Use language map from config if available
                        current_description = f"{config.LANGUAGE_NAME_MAP.get(lang_code, lang_code)} Rules"
                
                rule_files[base_filename] = current_description
            else:
                 # This case handles specific files like 'global.md'
                 # If a file like global.md was expected but not found, log it
                 if '*' not in filename_pattern and not os.path.isfile(file_path):
                     logger.warning(f"Expected rule file not found: {file_path}")

    # Sort rules for consistent display: global, stages, then languages alphabetically
    sorted_rule_files = {}
    # Add specific files in desired order
    for fname in ['global.md', 'stage2_evaluate_template.md', 'stage3_refine_template.md']:
        if fname in rule_files:
            sorted_rule_files[fname] = rule_files.pop(fname)
            
    # Add remaining language files sorted alphabetically
    for fname in sorted(rule_files.keys()):
         sorted_rule_files[fname] = rule_files[fname]

    return sorted_rule_files


# --- Flask App Setup ---
def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.urandom(24) # Needed for session/flash
    
    # Ensure necessary directories exist
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    # os.makedirs(config.INPUT_CSV_DIR, exist_ok=True) # Removed, using temp_uploads instead
    os.makedirs(ARCHIVE_DIR, exist_ok=True) # Use constant
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
            
            # 2. Construct re-translate prompt
            # Load template (requires prompt_manager update)
            retranslate_prompt_template = prompt_manager.load_single_prompt_file(config.STAGE4_TEMPLATE_FILE) # Need STAGE4 path in config
            if not retranslate_prompt_template:
                 return jsonify({"error": "Retranslate prompt template not found"}), 500
                 
            # Get full ruleset
            lang_specific_part = prompt_manager.stage1_templates.get(lang_code)
            if not lang_specific_part: raise ValueError(f"Lang specific prompt for {lang_code} missing")
            lang_name = config.LANGUAGE_NAME_MAP.get(lang_code, lang_code)
            lang_specific_part = lang_specific_part.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)
            full_ruleset_prompt = f"{lang_specific_part}\n\n{prompt_manager.global_rules_content}"
            
            final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION:..." # Add instruction

            system_prompt = retranslate_prompt_template.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)\
                                                       .replace("<<RULES>>", full_ruleset_prompt)\
                                                       .replace("<<SOURCE_TEXT>>", source_text or "")\
                                                       .replace("<<CURRENT_TRANSLATION>>", current_translation)\
                                                       .replace("<<REFINEMENT_INSTRUCTION>>", refinement_instruction)
            system_prompt += final_instruction

            # 3. Determine API/Model (Use batch defaults for now)
            batch_info = db_manager.get_batch_info(db_path, task_info['batch_id'])
            api_to_use = config.DEFAULT_API
            model_to_use = None # Default model
            if batch_info and batch_info['config_details']:
                try:
                    batch_config = json.loads(batch_info['config_details'])
                    # Use S3 API/Model config from batch if available? Or S1? Let's use S1 for re-translate.
                    api_to_use = batch_config.get('s1_api', config.DEFAULT_API)
                    model_to_use = batch_config.get('s1_model')
                except Exception:
                    logger.warning(f"Could not parse batch config for retranslate API selection, using defaults.")

            # 4. Call API
            openai_client_obj = api_clients.get_openai_client() # Get client if needed
            new_translation = translation_service.call_active_api(
                api_to_use, system_prompt, 
                "Apply the refinement instruction.", # Simple user content
                row_identifier=f"{task_id}-RETRANSLATE", 
                model_override=model_to_use,
                openai_client_obj=openai_client_obj
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
        """Displays a list of available rule files."""
        logger.info("Received request for /rules")
        try:
            rules = get_rule_files()
            return render_template('rules_list.html', rules=rules)
        except Exception as e:
            logger.exception("Error getting rule files list.")
            flash(f'Error loading rules list: {e}', 'danger')
            return render_template('rules_list.html', rules={}) # Pass empty dict on error
            
    @app.route('/rules/view/<path:filename>')
    def view_rule(filename):
        """Displays the content of a specific rule file."""
        logger.info(f"Received request to view rule: {filename}")
        # Security: Prevent accessing files outside the PROMPT_DIR
        target_file_path = os.path.abspath(os.path.join(PROMPT_DIR, filename))
        if not target_file_path.startswith(os.path.abspath(PROMPT_DIR)):
            logger.warning(f"Attempt to access file outside prompt directory: {filename}")
            flash("Invalid file path.", "danger")
            return redirect(url_for('list_rules'))

        try:
            with open(target_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Get the friendly description for the title
            all_rules = get_rule_files() # Reuse helper to get descriptions
            description = all_rules.get(filename, filename) # Default to filename if not found
            return render_template('rules_view.html', filename=filename, description=description, content=content)
        except FileNotFoundError:
            logger.error(f"Rule file not found: {target_file_path}")
            flash(f"Rule file '{filename}' not found.", "warning")
            return redirect(url_for('list_rules'))
        except Exception as e:
            logger.exception(f"Error reading rule file {filename}: {e}")
            flash(f"Error reading rule file '{filename}': {e}", "danger")
            return redirect(url_for('list_rules'))

    @app.route('/rules/edit/<path:filename>', methods=['GET', 'POST'])
    def edit_rule(filename):
        """Handles viewing the edit form and saving changes to a rule file."""
        logger.info(f"Received request to edit rule: {filename} (Method: {request.method})")
        
        # --- Security Check & Path Setup --- 
        target_file_path = os.path.abspath(os.path.join(PROMPT_DIR, filename))
        if not target_file_path.startswith(os.path.abspath(PROMPT_DIR)):
            logger.warning(f"Attempt to access file outside prompt directory for editing: {filename}")
            flash("Invalid file path.", "danger")
            return redirect(url_for('list_rules'))
            
        # Ensure the prompt directory exists (should always, but belt-and-suspenders)
        if not os.path.isdir(PROMPT_DIR):
             logger.error(f"Prompt directory {PROMPT_DIR} not found during edit.")
             flash("Configuration error: Prompt directory not found.", "danger")
             return redirect(url_for('list_rules'))
             
        # --- Handle POST Request (Saving Changes) --- 
        if request.method == 'POST':
            try:
                edited_content = request.form.get('edited_content')
                if edited_content is None: # Check if the form field exists
                    raise ValueError("Form data missing 'edited_content'.")

                # 1. Archive the current version
                archive_filename = f"{filename}_{datetime.now().strftime('%Y%m%d%H%M%S')}.md"
                archive_file_path = os.path.join(ARCHIVE_DIR, archive_filename)
                os.makedirs(ARCHIVE_DIR, exist_ok=True) # Ensure archive dir exists
                
                if os.path.exists(target_file_path):
                    with open(target_file_path, 'r', encoding='utf-8') as current_file, \
                         open(archive_file_path, 'w', encoding='utf-8') as archive_file:
                        archive_file.write(current_file.read())
                    logger.info(f"Archived current version of {filename} to {archive_filename}")
                else:
                    logger.warning(f"Original file {filename} not found for archiving during save.")

                # 2. Save the new content
                with open(target_file_path, 'w', encoding='utf-8') as f:
                    f.write(edited_content)
                logger.info(f"Successfully saved changes to {filename}")
                flash(f"Rule file '{filename}' saved successfully.", "success")
                return redirect(url_for('view_rule', filename=filename)) # Redirect back to view

            except Exception as e:
                logger.exception(f"Error saving rule file {filename}: {e}")
                flash(f"Error saving rule file '{filename}': {e}", "danger")
                # Stay on the edit page if save fails, passing back the attempted content
                # Need to get description again for the template title
                all_rules = get_rule_files()
                description = all_rules.get(filename, filename)
                # Pass the content the user tried to save back to the template
                return render_template('rules_edit.html', filename=filename, description=description, content=edited_content), 500 

        # --- Handle GET Request (Show Edit Form) --- 
        else: # request.method == 'GET'
            try:
                with open(target_file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                all_rules = get_rule_files()
                description = all_rules.get(filename, filename)
                return render_template('rules_edit.html', filename=filename, description=description, content=content)
            except FileNotFoundError:
                logger.error(f"Rule file not found for editing: {target_file_path}")
                flash(f"Rule file '{filename}' not found.", "warning")
                return redirect(url_for('list_rules'))
            except Exception as e:
                logger.exception(f"Error reading rule file {filename} for editing: {e}")
                flash(f"Error reading rule file '{filename}' for editing: {e}", "danger")
                return redirect(url_for('list_rules'))
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
