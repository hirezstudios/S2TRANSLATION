import sqlite3
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_db_connection(db_file_path):
    """Establishes a connection to the SQLite database specified by path."""
    # Ensure directory exists
    os.makedirs(os.path.dirname(db_file_path), exist_ok=True)
    conn = sqlite3.connect(db_file_path)
    conn.row_factory = sqlite3.Row # Return rows as dict-like objects
    return conn

def initialize_database(db_file_path):
    """Creates the necessary tables if they don't exist."""
    logger.info(f"Initializing database at {db_file_path}...")
    try:
        conn = get_db_connection(db_file_path)
        cursor = conn.cursor()

        # Create Batches Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Batches (
                batch_id TEXT PRIMARY KEY,
                upload_filename TEXT NOT NULL,
                upload_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'pending', -- pending, processing, completed, failed
                config_details TEXT -- JSON blob for config used (langs, mode, apis)
            )
        """)
        logger.info("Table 'Batches' checked/created.")

        # Create TranslationTasks Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS TranslationTasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                row_index_in_file INTEGER NOT NULL, -- Original row number (0-based or 1-based? Let's use 0-based internally)
                language_code TEXT NOT NULL,
                source_text TEXT,
                status TEXT NOT NULL DEFAULT 'pending', -- pending, running, completed, error, stage1_complete, stage2_complete
                initial_translation TEXT,
                evaluation_score INTEGER,
                evaluation_feedback TEXT,
                final_translation TEXT,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                metadata_json TEXT, -- Store other columns from input row
                FOREIGN KEY (batch_id) REFERENCES Batches (batch_id)
            )
        """)
        logger.info("Table 'TranslationTasks' checked/created.")
        
        # Add index for faster lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_batch_status ON TranslationTasks (batch_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_task_batch_lang ON TranslationTasks (batch_id, language_code)")
        logger.info("Indexes checked/created.")

        conn.commit()
        conn.close()
        logger.info("Database initialization complete.")
    except sqlite3.Error as e:
        logger.error(f"Database error during initialization: {e}")
        raise # Re-raise the exception to halt execution if DB setup fails
    except Exception as e:
        logger.error(f"An unexpected error occurred during DB initialization: {e}")
        raise
        
# --- CRUD Operations --- #

def add_batch(db_file_path, batch_id, filename, config_json):
    conn = get_db_connection(db_file_path)
    try:
        conn.execute("INSERT INTO Batches (batch_id, upload_filename, config_details, status) VALUES (?, ?, ?, ?)",
                     (batch_id, filename, config_json, 'pending'))
        conn.commit()
        logger.info(f"Added batch {batch_id} for file {filename}")
    except sqlite3.Error as e:
        logger.error(f"Failed to add batch {batch_id}: {e}")
    finally:
        conn.close()

def add_translation_task(db_file_path, batch_id, row_index, lang_code, source_text, metadata_json):
    conn = get_db_connection(db_file_path)
    try:
        conn.execute("""
            INSERT INTO TranslationTasks 
            (batch_id, row_index_in_file, language_code, source_text, metadata_json, status)
            VALUES (?, ?, ?, ?, ?, ?) 
        """, (batch_id, row_index, lang_code, source_text, metadata_json, 'pending'))
        conn.commit()
        # Avoid logging every single task insertion to prevent spam
        # logger.debug(f"Added task for batch {batch_id}, row {row_index}, lang {lang_code}") 
    except sqlite3.Error as e:
        logger.error(f"Failed to add task for batch {batch_id}, row {row_index}, lang {lang_code}: {e}")
    finally:
        conn.close()

def get_pending_tasks(db_file_path, batch_id):
    conn = get_db_connection(db_file_path)
    try:
        cursor = conn.execute("SELECT * FROM TranslationTasks WHERE batch_id = ? AND status = ?", (batch_id, 'pending'))
        tasks = cursor.fetchall()
        return tasks
    except sqlite3.Error as e:
        logger.error(f"Failed to get pending tasks for batch {batch_id}: {e}")
        return []
    finally:
        conn.close()

def update_task_status(db_file_path, task_id, new_status, error_msg=None):
    conn = get_db_connection(db_file_path)
    try:
        conn.execute("UPDATE TranslationTasks SET status = ?, error_message = ?, last_updated = CURRENT_TIMESTAMP WHERE task_id = ?", 
                     (new_status, error_msg, task_id))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to update status for task {task_id}: {e}")
    finally:
        conn.close()

def update_task_results(db_file_path, task_id, status, initial_tx=None, score=None, feedback=None, final_tx=None, error_msg=None):
    conn = get_db_connection(db_file_path)
    try:
        conn.execute("""
            UPDATE TranslationTasks 
            SET status = ?, 
                initial_translation = ?, 
                evaluation_score = ?, 
                evaluation_feedback = ?, 
                final_translation = ?, 
                error_message = ?, 
                last_updated = CURRENT_TIMESTAMP 
            WHERE task_id = ?
            """, (status, initial_tx, score, feedback, final_tx, error_msg, task_id))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to update results for task {task_id}: {e}")
    finally:
        conn.close()

def get_completed_tasks_for_export(db_file_path, batch_id):
    conn = get_db_connection(db_file_path)
    try:
        # Fetch all tasks for the batch, ordered by original row index, then language
        cursor = conn.execute("""
            SELECT row_index_in_file, language_code, final_translation, metadata_json 
            FROM TranslationTasks 
            WHERE batch_id = ? 
            ORDER BY row_index_in_file ASC, language_code ASC
            """, (batch_id,))
        tasks = cursor.fetchall()
        return tasks
    except sqlite3.Error as e:
        logger.error(f"Failed to get completed tasks for export (batch {batch_id}): {e}")
        return []
    finally:
        conn.close()

def get_task_by_row_index(db_file_path, batch_id, row_index):
    """Retrieves the first task found for a given batch and original row index."""
    # Used to retrieve source_text or other metadata during export reconstruction.
    conn = get_db_connection(db_file_path)
    try:
        cursor = conn.execute("SELECT * FROM TranslationTasks WHERE batch_id = ? AND row_index_in_file = ? LIMIT 1", 
                            (batch_id, row_index))
        task = cursor.fetchone() # Fetch one row
        return task
    except sqlite3.Error as e:
        logger.error(f"Failed to get task for batch {batch_id}, row {row_index}: {e}")
        return None
    finally:
        conn.close()

def update_batch_status(db_file_path, batch_id, status):
    """Updates the status of a batch."""
    conn = get_db_connection(db_file_path)
    try:
        conn.execute("UPDATE Batches SET status = ?, upload_timestamp = CURRENT_TIMESTAMP WHERE batch_id = ?", 
                     (status, batch_id))
        conn.commit()
        logger.info(f"Updated batch {batch_id} status to {status}")
    except sqlite3.Error as e:
        logger.error(f"Failed to update status for batch {batch_id}: {e}")
    finally:
        conn.close()

def get_batch_info(db_file_path, batch_id):
    """Retrieves batch information, including config details."""
    conn = get_db_connection(db_file_path)
    try:
        cursor = conn.execute("SELECT * FROM Batches WHERE batch_id = ?", (batch_id,))
        batch = cursor.fetchone() # Fetch one row
        return batch
    except sqlite3.Error as e:
        logger.error(f"Failed to get info for batch {batch_id}: {e}")
        return None
    finally:
        conn.close()

# Allow running initialization directly (needs path argument)
if __name__ == '__main__':
    # Example: Get path from config if run directly
    # This assumes config can be imported safely here, which might not be true.
    # Best practice might be to pass path via CLI arg if running standalone.
    try:
        from . import config
        print(f"Initializing database directly at {config.DATABASE_FILE}...")
        initialize_database(config.DATABASE_FILE)
        print("Database ready.")
    except ImportError:
        print("Could not import config. Please provide DB path manually or run via main service.")
    except AttributeError:
         print("Could not find DATABASE_FILE in config. Run via main service or fix config.") 