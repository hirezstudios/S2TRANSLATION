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

        # Update TranslationTasks Table
        # Check existing columns first to handle potential re-runs
        cursor.execute("PRAGMA table_info(TranslationTasks)")
        existing_columns = [info[1] for info in cursor.fetchall()]

        # Define new columns and modifications
        schema_updates = {
            'approved_translation': 'TEXT',
            'review_status': 'TEXT NOT NULL DEFAULT \'pending_review\'',
            'reviewed_by': 'TEXT',
            'review_timestamp': 'DATETIME',
            'edit_history': 'TEXT' # Add placeholder for future
        }

        base_create_sql = """
            CREATE TABLE IF NOT EXISTS TranslationTasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                row_index_in_file INTEGER NOT NULL, 
                language_code TEXT NOT NULL,
                source_text TEXT,
                status TEXT NOT NULL DEFAULT 'pending', 
                initial_translation TEXT,
                evaluation_score INTEGER,
                evaluation_feedback TEXT,
                final_translation TEXT,              -- LLM's final output
                approved_translation TEXT,           -- User-approved/edited output
                review_status TEXT NOT NULL DEFAULT 'pending_review',
                reviewed_by TEXT,
                review_timestamp DATETIME,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                metadata_json TEXT,
                edit_history TEXT,                   -- Optional for future
                FOREIGN KEY (batch_id) REFERENCES Batches (batch_id)
            )
        """
        cursor.execute(base_create_sql)
        logger.info("Base table 'TranslationTasks' checked/created.")

        # Add new columns if they don't exist
        needs_default_update = []
        for col_name, col_def in schema_updates.items():
            if col_name not in existing_columns:
                # Separate base type from constraints for ALTER TABLE
                col_type_for_add = col_def.split()[0] # e.g., TEXT, DATETIME, INTEGER
                default_value = None
                is_not_null = False
                
                if "NOT NULL" in col_def.upper(): is_not_null = True
                if "DEFAULT" in col_def.upper():
                    # Extract default value (handle quotes for strings)
                    default_part = col_def.upper().split("DEFAULT")[1].strip()
                    if default_part.startswith("'"):
                        default_value = default_part.strip("'")
                    elif default_part.startswith("\""):
                         default_value = default_part.strip('\"')
                    else: # Assume numeric or keyword like CURRENT_TIMESTAMP
                         default_value = default_part 
                    needs_default_update.append((col_name, default_value))
                
                try:
                    add_column_sql = f"ALTER TABLE TranslationTasks ADD COLUMN {col_name} {col_type_for_add}"
                    cursor.execute(add_column_sql)
                    logger.info(f"Added column '{col_name}' to TranslationTasks.")
                    
                    # Add NOT NULL constraint separately if needed (Requires newer SQLite? Check docs)
                    # For simplicity, let's omit adding NOT NULL via ALTER for now
                    # if is_not_null:
                    #    logger.warning(f"NOT NULL constraint for {col_name} not added via ALTER TABLE.")

                except sqlite3.OperationalError as e:
                    if "duplicate column name" in str(e):
                        logger.warning(f"Column '{col_name}' already exists.")
                    else: raise e 
        
        # Update default values for newly added columns on existing rows
        for col_name, default_val in needs_default_update:
             if default_val is not None and default_val != 'CURRENT_TIMESTAMP': # Cannot set CURRENT_TIMESTAMP this way
                 logger.info(f"Updating existing rows with default for {col_name}...")
                 # Use parameterized query
                 update_sql = f"UPDATE TranslationTasks SET {col_name} = ? WHERE {col_name} IS NULL"
                 cursor.execute(update_sql, (default_val,))
             elif default_val == 'CURRENT_TIMESTAMP':
                  logger.info(f"Updating existing rows with CURRENT_TIMESTAMP for {col_name}...")
                  update_sql = f"UPDATE TranslationTasks SET {col_name} = CURRENT_TIMESTAMP WHERE {col_name} IS NULL"
                  cursor.execute(update_sql)

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
        logger.debug(f"DB_MANAGER: Adding task for Batch {batch_id}, Row {row_index}, Lang {lang_code}. Received metadata_json: {metadata_json}")
        # --- End Debug Log --- #
        conn.execute("""
            INSERT INTO TranslationTasks 
            (batch_id, row_index_in_file, language_code, source_text, metadata_json, status)
            VALUES (?, ?, ?, ?, ?, ?) 
        """, (batch_id, row_index, lang_code, source_text, metadata_json, 'pending')) 
        conn.commit()
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

def update_task_results(db_file_path, task_id, status, 
                          initial_tx=None, score=None, feedback=None, 
                          final_tx=None, approved_tx=None, review_sts=None,
                          error_msg=None):
    """Updates task results, status, and optionally review status/approved text."""
    conn = get_db_connection(db_file_path)
    try:
        # Build SET clauses dynamically to avoid overwriting unrelated fields with None
        set_clauses = [
            "status = ?",
            "last_updated = CURRENT_TIMESTAMP"
        ]
        params = [status]

        if initial_tx is not None: 
            set_clauses.append("initial_translation = ?")
            params.append(initial_tx)
        if score is not None:
            set_clauses.append("evaluation_score = ?")
            params.append(score)
        if feedback is not None:
            set_clauses.append("evaluation_feedback = ?")
            params.append(feedback)
        if final_tx is not None:
            set_clauses.append("final_translation = ?")
            params.append(final_tx)
        if approved_tx is not None: # Update approved text if provided
            set_clauses.append("approved_translation = ?")
            params.append(approved_tx)
        if review_sts is not None: # Update review status if provided
            set_clauses.append("review_status = ?")
            params.append(review_sts)
            # Also update timestamp/user if review status is changing?
            # Could be done here or in update_review_status
            if review_sts != 'pending_review': # Example: Set timestamp if not pending
                 set_clauses.append("review_timestamp = CURRENT_TIMESTAMP")
                 # Add reviewed_by = ? if user tracking is added later
                 # params.append(user_id)

        # Always update error message (could be None to clear it)
        set_clauses.append("error_message = ?")
        params.append(error_msg) 
            
        params.append(task_id) # For the WHERE clause
        
        sql = f"UPDATE TranslationTasks SET { ', '.join(set_clauses) } WHERE task_id = ?"
        
        conn.execute(sql, tuple(params))
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Failed to update results for task {task_id}: {e}")
    finally:
        conn.close()

def get_completed_tasks_for_export(db_file_path, batch_id):
    """Fetches data needed for final export (uses APPROVED translation)."""
    conn = get_db_connection(db_file_path)
    try:
        # Fetch tasks that have an approved translation
        # Or should we fetch all and let generate_export decide?
        # Let's fetch all relevant data for rows that reached completion, export will use approved.
        cursor = conn.execute("""
            SELECT row_index_in_file, language_code, approved_translation, final_translation, review_status, metadata_json, source_text 
            FROM TranslationTasks 
            WHERE batch_id = ? 
            -- Maybe filter by status LIKE 'completed%'?
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

def count_tasks_for_batch(db_file_path, batch_id):
    """Counts the total number of tasks associated with a batch."""
    conn = get_db_connection(db_file_path)
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM TranslationTasks WHERE batch_id = ?", (batch_id,))
        count = cursor.fetchone()[0]
        return count
    except sqlite3.Error as e:
        logger.error(f"Failed to count tasks for batch {batch_id}: {e}")
        return 0 # Return 0 on error
    finally:
        conn.close()

def count_tasks_by_status(db_file_path, batch_id, status):
    """Counts the number of tasks for a batch with a specific status."""
    conn = get_db_connection(db_file_path)
    try:
        # Use LIKE for statuses like 'completed%'
        status_pattern = status + '%' if status.endswith('%') else status
        cursor = conn.execute("SELECT COUNT(*) FROM TranslationTasks WHERE batch_id = ? AND status LIKE ?", 
                            (batch_id, status_pattern))
        count = cursor.fetchone()[0]
        return count
    except sqlite3.Error as e:
        logger.error(f"Failed to count tasks for batch {batch_id} with status {status}: {e}")
        return 0 # Return 0 on error
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

def get_all_batches_for_history(db_file_path):
    """Fetches all batches for the history page, ordered by most recent."""
    conn = get_db_connection(db_file_path)
    try:
        cursor = conn.execute("""
            SELECT batch_id, upload_filename, upload_timestamp, status 
            FROM Batches 
            ORDER BY upload_timestamp DESC
            """)
        batches = cursor.fetchall()
        logger.info(f"Retrieved {len(batches)} batches for history.")
        return batches
    except sqlite3.Error as e:
        logger.error(f"Failed to get all batches for history: {e}")
        return []
    finally:
        conn.close()

def update_review_status(db_file_path, task_id, review_status, approved_translation, user_id=None):
    """Updates the review status and approved translation for a task."""
    conn = get_db_connection(db_file_path)
    try:
        # Ensure approved_translation is set correctly based on status
        if review_status == 'denied':
            approved_translation_to_save = None # Store NULL for denied
        elif review_status == 'approved_original':
             # Fetch final_translation if approved_translation not provided directly (should be provided by JS now)
             if approved_translation is None:
                 cursor = conn.execute("SELECT final_translation FROM TranslationTasks WHERE task_id = ?", (task_id,))
                 result = cursor.fetchone()
                 approved_translation_to_save = result['final_translation'] if result else None
                 logger.warning(f"Approved_original for task {task_id} - fetched final_translation from DB.")
             else:
                 approved_translation_to_save = approved_translation
        elif review_status == 'approved_edited':
            # Use the text provided, which could be an empty string
             approved_translation_to_save = approved_translation
        else:
            # Should not happen due to validation in Flask route, but handle defensively
             logger.error(f"Invalid review_status '{review_status}' passed to update_review_status for task {task_id}")
             return # Don't proceed

        conn.execute("""
            UPDATE TranslationTasks 
            SET review_status = ?, 
                approved_translation = ?, 
                reviewed_by = ?, 
                review_timestamp = CURRENT_TIMESTAMP,
                last_updated = CURRENT_TIMESTAMP
            WHERE task_id = ?
            """, (review_status, approved_translation_to_save, user_id, task_id))
        conn.commit()
        logger.info(f"Updated review status for task {task_id} to {review_status}")
    except sqlite3.Error as e:
        logger.error(f"Failed to update review status for task {task_id}: {e}")
    finally:
        conn.close()

def get_tasks_for_review(db_file_path, batch_id, language_code=None, review_status_filter=None):
    """Fetches tasks for review UI, optionally filtering by language and review status."""
    conn = get_db_connection(db_file_path)
    try:
        query = """
            SELECT task_id, row_index_in_file, language_code, source_text, 
                   initial_translation, evaluation_score, evaluation_feedback, 
                   final_translation, approved_translation, review_status, status, error_message,
                   metadata_json
            FROM TranslationTasks 
            WHERE batch_id = ? 
        """
        params = [batch_id]
        
        if language_code:
            query += " AND language_code = ?"
            params.append(language_code)
        
        if review_status_filter:
            query += " AND review_status = ?"
            params.append(review_status_filter)
            
        query += " ORDER BY row_index_in_file ASC, language_code ASC"
        
        cursor = conn.execute(query, params)
        tasks = cursor.fetchall()
        return tasks
    except sqlite3.Error as e:
        logger.error(f"Failed to get tasks for review (batch {batch_id}): {e}")
        return []
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