import os
import logging
from flask import Flask, request, render_template, jsonify, send_file
import uuid
import json
import threading
from werkzeug.utils import secure_filename

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
        """Main dashboard page."""
        return render_template('index.html')

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
                
            batch_status = batch_info['status'] 
            
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
                "batch_status": batch_status,
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
            
    # Placeholder for other routes: /results, /rules
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
