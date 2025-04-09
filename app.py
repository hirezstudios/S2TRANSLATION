import os
import logging
from flask import Flask, request, render_template, jsonify
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
            
            # TODO: Get selected languages, mode, api config from request.form
            selected_languages = request.form.getlist('languages[]') # Example if sent as list
            mode = request.form.get('mode', 'ONE_STAGE')
            # ... extract other config ...
            
            if not selected_languages:
                 return jsonify({"error": "No languages selected"}), 400

            # Save file temporarily (safer than using filename directly)
            temp_dir = "temp_uploads"
            os.makedirs(temp_dir, exist_ok=True)
            temp_filename = f"{uuid.uuid4()}_{file.filename}"
            file_path = os.path.join(temp_dir, temp_filename)
            file.save(file_path)
            logger.info(f"Temporarily saved uploaded file to {file_path}")
            
            # Construct mode_config dictionary (as done in Streamlit app)
            mode_config = {"mode": mode, "languages": selected_languages, # Add API/Model config here 
                          "s1_api": request.form.get('s1_api'), # ... etc
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
            
    # Placeholder for other routes: /results, /rules, /export
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
