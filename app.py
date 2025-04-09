import os
import logging
from flask import Flask, request, render_template, jsonify, send_file
import uuid
import json
import threading
from werkzeug.utils import secure_filename
import pandas as pd # Make sure pandas is imported

# Import backend modules
from src import config
from src import db_manager
from src import translation_service
from src import prompt_manager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Flask App Setup ---
def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.urandom(24) # Needed for session/flash potentially
    
    # Ensure necessary directories exist
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    os.makedirs(config.INPUT_CSV_DIR, exist_ok=True) # For temp uploads if needed
    os.makedirs(config.ARCHIVE_DIR, exist_ok=True)
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
            
            # Intersect with configured languages and those with prompts
            for lang_code in config.AVAILABLE_LANGUAGES:
                if lang_code in potential_langs and lang_code in prompt_manager.stage1_templates:
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
            tasks_list = [dict(task) for task in tasks_raw]

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
            # Approved translation can be None (for deny) or empty string
            approved_translation = data.get('approved_translation') 
            user_id = "webapp" # Placeholder for user ID

            # --- Validation --- #
            allowed_statuses = ['approved_original', 'approved_edited', 'denied']
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

    # Placeholder for other routes: /rules
    @app.route('/placeholder')
    def placeholder():
        return "Route not implemented yet."
        
    return app

# Allow running directly for development
if __name__ == '__main__':
    app = create_app()
    # Note: Flask's dev server is not suitable for production.
    # Use Waitress or Gunicorn/uWSGI + Nginx for deployment.
    # Debug mode enables auto-reloading.
    app.run(debug=True, threaded=False) # Set threaded=False for easier SSE/Celery debug initially
