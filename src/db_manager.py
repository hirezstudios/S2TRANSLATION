import sqlite3
import os
import logging
from typing import List, Dict, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_db_connection(db_file_path):
    """Establishes a connection to the SQLite database specified by path."""
    # Ensure directory exists only if specified
    db_dir = os.path.dirname(db_file_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
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

        # Create VectorStoreSets Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS VectorStoreSets (
                set_id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                source_csv_filename TEXT,
                notes TEXT,
                is_active INTEGER DEFAULT 0 -- Boolean (0 or 1)
            )
        """)
        logger.info("Table 'VectorStoreSets' checked/created.")

        # Create VectorStoreMappings Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS VectorStoreMappings (
                mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
                set_id INTEGER NOT NULL,
                language_code TEXT NOT NULL, -- e.g., 'frFR'
                column_name TEXT NOT NULL,   -- e.g., 'tg_frFR', the actual column name in the CSV
                status TEXT NOT NULL DEFAULT 'pending', -- pending, processing, completed, failed
                openai_vs_id TEXT,           -- OpenAI Vector Store ID
                openai_file_id TEXT,         -- OpenAI File ID
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (set_id) REFERENCES VectorStoreSets (set_id)
            )
        """)
        logger.info("Table 'VectorStoreMappings' checked/created.")

        # Update TranslationTasks Table
        cursor.execute("PRAGMA table_info(TranslationTasks)")
        existing_columns = [info[1] for info in cursor.fetchall()]

        # Define new columns and modifications
        schema_updates = {
            'approved_translation': 'TEXT',
            'review_status': 'TEXT NOT NULL DEFAULT \'pending_review\'',
            'reviewed_by': 'TEXT',
            'review_timestamp': 'DATETIME',
            'edit_history': 'TEXT',
            'stage0_glossary': 'TEXT',
            'stage0_raw_output': 'TEXT',
            'stage0_status': 'TEXT',
            'initial_target_text': 'TEXT'
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
                final_translation TEXT,
                approved_translation TEXT,
                review_status TEXT NOT NULL DEFAULT 'pending_review',
                reviewed_by TEXT,
                review_timestamp DATETIME,
                last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                metadata_json TEXT,
                edit_history TEXT,
                stage0_glossary TEXT,
                stage0_raw_output TEXT,
                stage0_status TEXT,
                initial_target_text TEXT,
                FOREIGN KEY (batch_id) REFERENCES Batches (batch_id) ON DELETE CASCADE
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vs_sets_active ON VectorStoreSets (is_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vs_mappings_set ON VectorStoreMappings (set_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_vs_mappings_status ON VectorStoreMappings (status)")
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

def add_translation_task(db_file_path, batch_id, row_index, lang_code, source_text, metadata_json, initial_target_text=None):
    conn = get_db_connection(db_file_path)
    try:
        logger.debug(f"DB_MANAGER: Adding task for Batch {batch_id}, Row {row_index}, Lang {lang_code}. Received metadata_json: {metadata_json}, initial_target: {initial_target_text}")
        conn.execute("""
            INSERT INTO TranslationTasks 
            (batch_id, row_index_in_file, language_code, source_text, metadata_json, initial_target_text, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (batch_id, row_index, lang_code, source_text, metadata_json, initial_target_text, 'pending'))
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
                          error_msg=None,
                          stage0_glossary=None,
                          stage0_raw_output=None,
                          stage0_status=None):
    """Updates task results, status, and optionally review status/approved text and Stage 0 results."""
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

        # <<< ADD STAGE 0 CLAUSES >>>
        if stage0_glossary is not None:
            set_clauses.append("stage0_glossary = ?")
            params.append(stage0_glossary)
        if stage0_raw_output is not None:
            set_clauses.append("stage0_raw_output = ?")
            params.append(stage0_raw_output)
        if stage0_status is not None:
            set_clauses.append("stage0_status = ?")
            params.append(stage0_status)
        # <<<<<<<<<<<<<<<<<<<<<<<<<<<
            
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

# --- DB Functions for Vector Stores (NEW) ---

def get_vector_store_sets(db_file_path):
    """Retrieves all VectorStoreSets ordered by timestamp."""
    conn = get_db_connection(db_file_path)
    try:
        cursor = conn.execute("""
            SELECT set_id, upload_timestamp, source_csv_filename, notes, is_active 
            FROM VectorStoreSets 
            ORDER BY upload_timestamp DESC
            """)
        sets = cursor.fetchall()
        logger.info(f"Retrieved {len(sets)} vector store sets for admin page.")
        return sets
    except sqlite3.Error as e:
        logger.error(f"Failed to get vector store sets: {e}")
        return []
    finally:
        conn.close()

def get_vector_store_sets_by_id(db_file_path: str, set_id: int) -> Optional[Dict]:
    """Retrieves a single VectorStoreSet by its ID."""
    conn = get_db_connection(db_file_path)
    try:
        cursor = conn.execute("""
            SELECT set_id, upload_timestamp, source_csv_filename, notes, is_active 
            FROM VectorStoreSets 
            WHERE set_id = ?
            """, (set_id,))
        set_info = cursor.fetchone()
        if set_info:
            logger.debug(f"Retrieved vector store set info for ID: {set_id}")
            return dict(set_info)
        else:
            logger.warning(f"No vector store set found for ID: {set_id}")
            return None
    except sqlite3.Error as e:
        logger.error(f"Failed to get vector store set for ID {set_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def add_vector_store_set(db_path, source_csv_filename, notes):
    """Adds a new vector store set entry to the database.

    Args:
        db_path (str): Path to the SQLite database file.
        source_csv_filename (str): The original name of the uploaded CSV file.
        notes (str): Optional user-provided notes for the set.

    Returns:
        int: The ID of the newly inserted set, or None if an error occurred.
    """
    sql = """
    INSERT INTO VectorStoreSets (source_csv_filename, notes)
    VALUES (?, ?)
    """
    conn = None
    new_set_id = None
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()
        cursor.execute(sql, (source_csv_filename, notes))
        conn.commit()
        new_set_id = cursor.lastrowid
        logger.info(f"Added new VectorStoreSet with ID: {new_set_id}, filename: {source_csv_filename}")
    except sqlite3.Error as e:
        logger.exception(f"Database error adding VectorStoreSet for {source_csv_filename}: {e}")
        if conn:
            conn.rollback() # Rollback changes on error
    finally:
        if conn:
            conn.close()
    return new_set_id

def add_vector_store_mapping(db_path, set_id, language_code, column_name):
    """Adds a new vector store mapping entry for a specific language within a set.

    Args:
        db_path (str): Path to the SQLite database file.
        set_id (int): The ID of the VectorStoreSet this mapping belongs to.
        language_code (str): The language code (e.g., 'frFR').
        column_name (str): The corresponding column name in the source CSV (e.g., 'tg_frFR').

    Returns:
        int: The ID of the newly inserted mapping, or None if an error occurred.
    """
    sql = """
    INSERT INTO VectorStoreMappings (set_id, language_code, column_name, status)
    VALUES (?, ?, ?, 'pending')
    """
    conn = None
    new_mapping_id = None
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()
        cursor.execute(sql, (set_id, language_code, column_name))
        conn.commit()
        new_mapping_id = cursor.lastrowid
        logger.info(f"Added new VectorStoreMapping ID: {new_mapping_id} for Set ID: {set_id}, Lang: {language_code}")
    except sqlite3.Error as e:
        logger.exception(f"Database error adding VectorStoreMapping for Set ID {set_id}, Lang {language_code}: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
    return new_mapping_id

def delete_vector_store_set(db_path, set_id):
    """Deletes a vector store set and its associated mappings from the database.

    This is typically used for cleanup if the background preparation fails early.

    Args:
        db_path (str): Path to the SQLite database file.
        set_id (int): The ID of the VectorStoreSet to delete.

    Returns:
        bool: True if deletion was successful (or set didn't exist), False otherwise.
    """
    conn = None
    success = False
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()
        # Use a transaction to ensure both deletes succeed or fail together
        conn.execute("BEGIN TRANSACTION")
        # Delete associated mappings first due to potential foreign key constraints (if added later)
        cursor.execute("DELETE FROM VectorStoreMappings WHERE set_id = ?", (set_id,))
        mappings_deleted = cursor.rowcount
        # Delete the set itself
        cursor.execute("DELETE FROM VectorStoreSets WHERE set_id = ?", (set_id,))
        set_deleted = cursor.rowcount
        conn.commit()
        logger.info(f"Deleted VectorStoreSet ID: {set_id}. Set rows deleted: {set_deleted}, Mapping rows deleted: {mappings_deleted}")
        success = True
    except sqlite3.Error as e:
        logger.exception(f"Database error deleting VectorStoreSet ID {set_id}: {e}")
        if conn:
            conn.rollback()
        success = False
    finally:
        if conn:
            conn.close()
    return success

def get_mappings_for_set(db_path: str, set_id: int) -> Optional[List[Dict]]:
    """Retrieves all mappings associated with a specific VectorStoreSet ID."""
    sql = """
        SELECT 
            mapping_id, 
            set_id, 
            language_code, 
            column_name, 
            status, 
            openai_vs_id, 
            openai_file_id, 
            last_updated
        FROM VectorStoreMappings 
        WHERE set_id = ?
        ORDER BY language_code
    """
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()
        cursor.execute(sql, (set_id,))
        rows = cursor.fetchall()
        mappings = [dict(row) for row in rows]
        logger.info(f"Retrieved {len(mappings)} mappings for Set ID: {set_id}")
        return mappings
    except sqlite3.Error as e:
        logger.exception(f"Database error retrieving mappings for Set ID {set_id}: {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_mapping_status(db_path, mapping_id, status, openai_vs_id=None, openai_file_id=None):
    """Updates the status and optionally the OpenAI IDs for a mapping.

    Args:
        db_path (str): Path to the SQLite database file.
        mapping_id (int): The ID of the VectorStoreMapping to update.
        status (str): The new status ('processing', 'completed', 'failed').
        openai_vs_id (str, optional): OpenAI Vector Store ID. Defaults to None.
        openai_file_id (str, optional): OpenAI File ID. Defaults to None.

    Returns:
        bool: True if the update was successful, False otherwise.
    """
    # Only update IDs if provided, otherwise keep existing values
    updates = {}
    params = []
    
    updates["status"] = "?"
    params.append(status)
    
    updates["last_updated"] = "CURRENT_TIMESTAMP"
    # No parameter needed for CURRENT_TIMESTAMP

    if openai_vs_id is not None:
        updates["openai_vs_id"] = "?"
        params.append(openai_vs_id)
    if openai_file_id is not None:
        updates["openai_file_id"] = "?"
        params.append(openai_file_id)
    
    set_clauses = ", ".join(f"{key} = {value}" for key, value in updates.items()) 
    sql = f"""UPDATE VectorStoreMappings SET {set_clauses} WHERE mapping_id = ?"""
    params.append(mapping_id) # Add mapping_id for the WHERE clause

    conn = None
    success = False
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(f"Updated status to '{status}' for Mapping ID: {mapping_id} (VS_ID: {openai_vs_id}, File_ID: {openai_file_id})")
            success = True
        else:
            logger.warning(f"Attempted to update status for non-existent Mapping ID: {mapping_id}")
            success = False
    except sqlite3.Error as e:
        logger.exception(f"Database error updating status for Mapping ID {mapping_id}: {e}")
        if conn:
            conn.rollback()
        success = False
    finally:
        if conn:
            conn.close()
    return success

def activate_set(db_path, set_id_to_activate):
    """Activates a specific vector store set and deactivates all others.

    Args:
        db_path (str): Path to the SQLite database file.
        set_id_to_activate (int): The ID of the VectorStoreSet to activate.

    Returns:
        bool: True if activation was successful, False otherwise.
    """
    sql_deactivate_all = "UPDATE VectorStoreSets SET is_active = 0 WHERE is_active = 1"
    sql_activate_one = "UPDATE VectorStoreSets SET is_active = 1 WHERE set_id = ?"
    conn = None
    success = False
    try:
        conn = get_db_connection(db_path)
        cursor = conn.cursor()
        # Use a transaction
        conn.execute("BEGIN TRANSACTION")
        # Deactivate any currently active set(s)
        cursor.execute(sql_deactivate_all)
        deactivated_count = cursor.rowcount
        # Activate the target set
        cursor.execute(sql_activate_one, (set_id_to_activate,))
        activated_count = cursor.rowcount
        conn.commit()

        if activated_count == 1:
            logger.info(f"Successfully activated Set ID: {set_id_to_activate}. Deactivated {deactivated_count} other set(s).")
            success = True
        elif activated_count == 0:
            logger.warning(f"Attempted to activate non-existent Set ID: {set_id_to_activate}. Deactivated {deactivated_count} set(s). Rolling back.")
            # Rollback because the target set wasn't found, even if others were deactivated
            conn.rollback() # Explicit rollback needed here after commit was skipped
            success = False
        else:
            # Should not happen with primary key constraint
            logger.error(f"Unexpectedly activated {activated_count} rows for Set ID: {set_id_to_activate}. Deactivated {deactivated_count}. Rolling back.")
            conn.rollback()
            success = False

    except sqlite3.Error as e:
        logger.exception(f"Database error activating Set ID {set_id_to_activate}: {e}")
        if conn:
            conn.rollback()
        success = False
    finally:
        if conn:
            conn.close()
    return success

def get_active_vector_store_map(db_path: str) -> Optional[Dict[str, str]]:
    """Retrieves the language_code -> openai_vs_id map for the currently active set."""
    conn = get_db_connection(db_path)
    active_set_id = None
    active_map = {}
    try:
        # Find the active set ID
        cursor = conn.execute("SELECT set_id FROM VectorStoreSets WHERE is_active = 1 LIMIT 1")
        result = cursor.fetchone()
        if result:
            active_set_id = result['set_id']
            logger.info(f"Found active Vector Store Set ID: {active_set_id}")
        else:
            logger.warning("No active Vector Store Set found.")
            return None # Return None if no set is active

        # Get completed mappings for the active set
        cursor = conn.execute("""
            SELECT language_code, openai_vs_id 
            FROM VectorStoreMappings 
            WHERE set_id = ? AND status = 'completed' AND openai_vs_id IS NOT NULL
            """, (active_set_id,))
        
        mappings = cursor.fetchall()
        for row in mappings:
            active_map[row['language_code']] = row['openai_vs_id']
            
        logger.info(f"Retrieved {len(active_map)} completed vector store mappings for active set {active_set_id}.")
        return active_map

    except sqlite3.Error as e:
        logger.exception(f"Database error retrieving active vector store map: {e}")
        return None
    finally:
        if conn:
            conn.close()

# --- Stages Report Query Function --- 
# Find or create the function used by generate_stages_report
# Let's assume we need to create/update get_tasks_for_stages_report

def get_tasks_for_stages_report(db_path: str, batch_id: str) -> List[Dict]:
    """Fetches all task details needed for the detailed stages report."""
    conn = get_db_connection(db_path)
    try:
        cursor = conn.execute("""
            SELECT task_id, row_index_in_file, language_code, source_text, 
                   initial_translation, evaluation_score, evaluation_feedback, 
                   final_translation, approved_translation, review_status, 
                   status as task_status, error_message, 
                   stage0_glossary, stage0_raw_output  -- <<< ADD STAGE 0 COLUMNS >>>
            FROM TranslationTasks 
            WHERE batch_id = ? 
            ORDER BY row_index_in_file ASC, language_code ASC
            """, (batch_id,))
        tasks = [dict(row) for row in cursor.fetchall()]
        logger.info(f"Retrieved {len(tasks)} tasks for stages report (batch {batch_id}).")
        return tasks
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch tasks for stages report (batch {batch_id}): {e}")
        return []
    finally:
        if conn:
            conn.close()

# <<< ADD NEW FUNCTION: count_tasks_with_stage0_status >>>
def count_tasks_with_stage0_status(db_path: str, batch_id: str, stage0_status: str) -> int:
    """Counts tasks in a batch with a specific stage0_status."""
    conn = get_db_connection(db_path)
    count = 0
    try:
        cursor = conn.execute(""" 
            SELECT COUNT(*) FROM TranslationTasks 
            WHERE batch_id = ? AND stage0_status = ?
            """, (batch_id, stage0_status))
        result = cursor.fetchone()
        if result:
            count = result[0]
        logger.debug(f"Counted {count} tasks for batch {batch_id} with stage0_status '{stage0_status}'.")
    except sqlite3.Error as e:
        logger.error(f"Failed to count tasks for batch {batch_id} with stage0_status '{stage0_status}': {e}")
        count = 0 # Return 0 on error
    finally:
        if conn:
            conn.close()
    return count
# <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# Allow running initialization directly (needs path argument)
if __name__ == '__main__':
    # Example: Get path from config if run directly
    # This assumes config can be imported safely here, which might not be true.
    # Best practice might be to pass path via CLI arg if running standalone.
    try:
        # Use relative import for config when run directly
        import config
        db_path = config.DATABASE_FILE
        print(f"Initializing database directly at {db_path}...")
        # Ensure the directory exists before initializing
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
            print(f"Ensured directory exists: {db_dir}")
        initialize_database(db_path)
        print("Database ready.")
    except ImportError:
        print("Could not import config. Please provide DB path manually or run via main service.")
    except AttributeError:
         print("Could not find DATABASE_FILE in config. Run via main service or fix config.")
    except Exception as e:
        print(f"An error occurred during direct initialization: {e}")
        import traceback
        traceback.print_exc() # Print full traceback for debugging 