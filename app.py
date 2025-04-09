import os
import logging
from flask import Flask, request, render_template, jsonify, send_file
import uuid
import json
import threading

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
        """Endpoint to start a new translation batch."""
        logger.info("Received request to /start_job")
        # Extract data from the POST request (form data or JSON)
        # This depends on how the frontend JS sends it
        try:
            if 'file' not in request.files:
                return jsonify({"error": "No file part"}), 400
            file = request.files['file']
            if file.filename == '':
                return jsonify({"error": "No selected file"}), 400
            
            # Get selected languages, mode, api config from form
            selected_languages = request.form.getlist('languages') # Assume name="languages"
            mode = request.form.get('mode', 'ONE_STAGE')
            one_stage_api = request.form.get('one_stage_api')
            one_stage_model = request.form.get('one_stage_model') or None # Convert empty string to None
            s1_api = request.form.get('s1_api')
            s1_model = request.form.get('s1_model') or None
            s2_api = request.form.get('s2_api')
            s2_model = request.form.get('s2_model') or None
            s3_api = request.form.get('s3_api')
            s3_model = request.form.get('s3_model') or None
            
            if not selected_languages:
                 return jsonify({"error": "No languages selected"}), 400

            # Save file temporarily (safer than using filename directly)
            temp_dir = "temp_uploads"
            os.makedirs(temp_dir, exist_ok=True)
            temp_filename = f"{uuid.uuid4()}_{file.filename}"
            file_path = os.path.join(temp_dir, temp_filename)
            file.save(file_path)
            logger.info(f"Temporarily saved uploaded file to {file_path}")
            
            # Construct mode_config dictionary 
            if mode == "ONE_STAGE":
                mode_config = {
                    "mode": mode, 
                    "languages": selected_languages, 
                    "default_api": one_stage_api, 
                    "s1_api": one_stage_api, "s1_model": one_stage_model,
                    "s2_api": None, "s2_model": None,
                    "s3_api": None, "s3_model": None,
                 }
            else: # THREE_STAGE
                 mode_config = {
                    "mode": mode, 
                    "languages": selected_languages, 
                    "default_api": config.DEFAULT_API, # Still needed for reference?
                    "s1_api": s1_api,
                    "s2_api": s2_api,
                    "s3_api": s3_api,
                    "s1_model": s1_model, 
                    "s2_model": s2_model,
                    "s3_model": s3_model,
                }
            
            # Call backend prepare_batch
            batch_id = translation_service.prepare_batch(file_path, selected_languages, mode_config)

            if batch_id:
                logger.info(f"Batch {batch_id} prepared. Starting background thread...")
                # Trigger background thread (NOT Celery)
                thread = threading.Thread(
                    target=translation_service.run_batch_background, # New target function name
                    args=(batch_id,)
                    # Note: We might need to pass initialized API clients if they aren't thread-safe
                    # or cannot be initialized within the thread easily.
                    # For now, assume backend handles client init per thread if needed.
                )
                thread.start()
                # Return success and batch_id to UI
                return jsonify({"message": "Job started successfully", "batch_id": batch_id}), 200
            else:
                logger.error("Batch preparation failed.")
                return jsonify({"error": "Batch preparation failed."}), 500
                
        except Exception as e:
             logger.error(f"Error in /start_job: {e}", exc_info=True)
             return jsonify({"error": "An unexpected error occurred."}), 500
             
    # Add status endpoint
    @app.route('/status/<batch_id>')
    def batch_status(batch_id):
        logger.debug(f"Received status request for batch {batch_id}")
        try:
            conn = db_manager.get_db_connection(config.DATABASE_FILE)
            # Get batch status
            batch_info = conn.execute("SELECT status FROM Batches WHERE batch_id = ?", (batch_id,)).fetchone()
            batch_status = batch_info['status'] if batch_info else 'not_found'
            
            # Get task progress
            total_tasks_cursor = conn.execute("SELECT COUNT(*) FROM TranslationTasks WHERE batch_id = ?", (batch_id,))
            total_tasks = total_tasks_cursor.fetchone()[0]
            completed_tasks_cursor = conn.execute("SELECT COUNT(*) FROM TranslationTasks WHERE batch_id = ? AND status LIKE 'completed%'", (batch_id,))
            completed_tasks = completed_tasks_cursor.fetchone()[0]
            error_tasks_cursor = conn.execute("SELECT COUNT(*) FROM TranslationTasks WHERE batch_id = ? AND status = 'error'", (batch_id,))
            error_tasks = error_tasks_cursor.fetchone()[0]
            
            conn.close()

            return jsonify({
                "batch_id": batch_id,
                "batch_status": batch_status,
                "total_tasks": total_tasks,
                "completed_tasks": completed_tasks,
                "error_tasks": error_tasks
            })
        except Exception as e:
            logger.error(f"Error fetching status for batch {batch_id}: {e}", exc_info=True)
            return jsonify({"error": "Failed to fetch status"}), 500
            
    # Add Export route
    @app.route('/export/<batch_id>')
    def export_batch(batch_id):
        logger.info(f"Received export request for batch {batch_id}")
        try:
            # Determine expected output filename (needs original filename)
            batch_info = db_manager.get_batch_info(config.DATABASE_FILE, batch_id)
            if not batch_info or not batch_info['config_details']:
                return jsonify({"error": "Batch configuration not found"}), 404
                
            batch_config = json.loads(batch_info['config_details'])
            original_filename = batch_info['upload_filename'] or "unknown_file.csv"
            base_name = os.path.splitext(original_filename)[0]
            api_name = batch_config.get('default_api', 'unknown').lower()
            # Use batch status to determine filename suffix
            error_suffix = "_ERRORS" if batch_info['status'] != 'completed' else ""
            output_filename = f"output_{base_name}_{api_name}_batch_{batch_id[:8]}{error_suffix}.csv"
            output_file_path = os.path.join(config.OUTPUT_DIR, output_filename)

            # Generate the file
            success = translation_service.generate_export(batch_id, output_file_path)

            if success and os.path.exists(output_file_path):
                return send_file(output_file_path, as_attachment=True, download_name=output_filename)
            else:
                 logger.error(f"Failed to generate or find export file for batch {batch_id} at {output_file_path}")
                 return jsonify({"error": "Failed to generate export file."}), 500

        except Exception as e:
            logger.error(f"Error during export for batch {batch_id}: {e}", exc_info=True)
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
