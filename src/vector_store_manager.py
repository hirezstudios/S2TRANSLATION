"""Manages the creation, loading, and querying of vector stores."""

import time
import os
import logging
import pandas as pd
from openai import OpenAI, APIError, APIConnectionError
import tempfile

from src import config

logger = logging.getLogger(__name__)

# Define a base directory for vector stores relative to the project root
# VECTOR_STORE_BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), config.VECTOR_STORE_DIR)

def create_vector_store(csv_path, source_column, target_column, language_code, set_id, mapping_id):
    """(Placeholder) Creates a vector store for a specific language from a CSV.

    Args:
        csv_path (str): Path to the source CSV file.
        source_column (str): Name of the source language column (e.g., 'src_enUS').
        target_column (str): Name of the target language column (e.g., 'tg_frFR').
        language_code (str): The target language code (e.g., 'frFR').
        set_id (int): The ID of the parent VectorStoreSet.
        mapping_id (int): The ID of the VectorStoreMapping being processed.

    Returns:
        str: The path to the created vector store directory if successful, None otherwise.
    """
    logger.info(f"[Set {set_id}/Map {mapping_id}] Starting placeholder vector store creation for {language_code} from {csv_path}")
    # vector_store_path = get_vector_store_path(set_id, language_code)

    try:
        # --- TODO: Replace with actual ChromaDB implementation --- #
        # 1. Ensure base directory exists: os.makedirs(vector_store_path, exist_ok=True)
        # 2. Load CSV data (pandas): df = pd.read_csv(csv_path, usecols=[source_column, target_column]).dropna()
        # 3. Initialize ChromaDB client: client = chromadb.PersistentClient(path=vector_store_path)
        # 4. Get or create collection: collection = client.get_or_create_collection(f"translations_{language_code}")
        # 5. Prepare documents (target language): documents = df[target_column].tolist()
        # 6. Prepare metadatas (source language): metadatas = [{source_column: src} for src in df[source_column].tolist()]
        # 7. Prepare IDs: ids = [f"{mapping_id}_{i}" for i in range(len(documents))]
        # 8. Add to collection: collection.add(documents=documents, metadatas=metadatas, ids=ids)
        # 9. Persist changes (usually automatic with PersistentClient)
        # --- End ChromaDB Implementation --- #

        # Simulate work
        # os.makedirs(vector_store_path, exist_ok=True)
        time.sleep(2) # Simulate time taken for embedding/saving
        # Create a dummy file to indicate completion for the placeholder
        with open(os.path.join(vector_store_path, 'placeholder.txt'), 'w') as f:
            f.write(f"Placeholder for {language_code}")

        logger.info(f"[Set {set_id}/Map {mapping_id}] Placeholder vector store created for {language_code} at {vector_store_path}")
        return vector_store_path

    except Exception as e:
        logger.exception(f"[Set {set_id}/Map {mapping_id}] Error during placeholder vector store creation for {language_code}: {e}")
        # Optional: Clean up partially created store directory?
        # if os.path.exists(vector_store_path): shutil.rmtree(vector_store_path)?
        return None

# --- OpenAI Client Initialization --- #
# It's generally better practice to initialize the client once, 
# potentially in app setup or lazily when first needed, but for simplicity
# in this background task context, initializing it per function call is acceptable.
# Ensure OPENAI_API_KEY is loaded via dotenv or environment elsewhere.

# --- Helper Function to Create Temporary TXT --- #
def _create_lang_pair_txt(csv_path, source_col, target_col, language_code):
    """Creates a temporary TXT file containing only the specified language pair data."""
    try:
        df = pd.read_csv(csv_path, usecols=[source_col, target_col], encoding='utf-8')
        df.fillna('', inplace=True)
        df = df[(df[source_col].astype(str).str.strip() != '') & (df[target_col].astype(str).str.strip() != '')]

        if df.empty:
            logger.warning(f"No valid translation pairs found for {source_col} <-> {target_col} in {csv_path}. Cannot create TXT.")
            return None

        # Create a temporary file
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False, suffix='.txt') as temp_f:
            for _, row in df.iterrows():
                src_val = str(row[source_col]).replace('|', '/').replace('\n', ' ').strip()
                tgt_val = str(row[target_col]).replace('|', '/').replace('\n', ' ').strip()
                temp_f.write(f"{source_col}: {src_val} | {target_col}: {tgt_val}\n")
            temp_filepath = temp_f.name
            logger.info(f"Created temporary TXT for {language_code} at {temp_filepath}")
            return temp_filepath
            
    except FileNotFoundError:
        logger.error(f"CSV file not found at {csv_path} during TXT creation for {language_code}.")
        return None
    except KeyError as e:
        logger.error(f"Column error creating TXT for {language_code}: {e}. Ensure '{source_col}' and '{target_col}' exist.")
        return None
    except Exception as e:
        logger.exception(f"Error creating temporary TXT for {language_code}: {e}")
        return None

# --- Helper Function to Monitor File Processing --- #
def _monitor_file_processing(client: OpenAI, vector_store_id: str, file_id: str, timeout: int = 120):
    """Monitors the status of a file being processed in a vector store."""
    logger.info(f"Monitoring OpenAI file {file_id} processing in VS {vector_store_id} (timeout: {timeout}s)...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            vs_file_status = client.vector_stores.files.retrieve(
                vector_store_id=vector_store_id,
                file_id=file_id
            )
            status = vs_file_status.status
            logger.debug(f"  VS {vector_store_id} / File {file_id} status: {status} (Elapsed: {int(time.time() - start_time)}s)")
            if status == "completed":
                logger.info(f"File {file_id} processing completed for VS {vector_store_id}.")
                return True
            elif status == "failed":
                error_details = "Unknown error"
                if hasattr(vs_file_status, 'last_error') and vs_file_status.last_error:
                    err_code = getattr(vs_file_status.last_error, 'code', 'N/A')
                    err_msg = getattr(vs_file_status.last_error, 'message', 'N/A')
                    error_details = f"code={err_code}, message={err_msg}"
                logger.error(f"OpenAI File {file_id} processing failed for VS {vector_store_id}. Details: {error_details}")
                return False
            elif status == "cancelled":
                logger.error(f"OpenAI File {file_id} processing cancelled for VS {vector_store_id}.")
                return False
            # Continue polling for 'in_progress'
            time.sleep(10)
        except (APIError, APIConnectionError) as e:
            logger.warning(f"API Error checking OpenAI file status for VS {vector_store_id} / File {file_id}: {e}")
            time.sleep(15) # Longer sleep on API errors
        except Exception as e:
             logger.warning(f"Unexpected error checking OpenAI file status for VS {vector_store_id} / File {file_id}: {e}")
             time.sleep(10)
    else:
        logger.warning(f"Monitoring timed out after {timeout}s for VS {vector_store_id} / File {file_id}. Processing might still be in progress.")
        return None # Indicate uncertain status

# --- Main Function to Create Vector Store for a Language --- #
def create_openai_vector_store_for_language(csv_path, source_column, target_column, language_code, set_id, mapping_id):
    """Creates a dedicated OpenAI Vector Store for a specific language pair.

    Args:
        csv_path (str): Path to the source CSV file.
        source_column (str): Name of the source language column.
        target_column (str): Name of the target language column.
        language_code (str): The target language code (e.g., 'frFR').
        set_id (int): The ID of the parent VectorStoreSet (for naming/logging).
        mapping_id (int): The ID of the VectorStoreMapping being processed (for logging).

    Returns:
        tuple[str, str] | None: A tuple containing (openai_vector_store_id, openai_file_id)
                                if successful, None otherwise.
    """
    logger.info(f"[Set {set_id}/Map {mapping_id}] Starting OpenAI Vector Store creation for {language_code} from {csv_path}")
    
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        logger.error("[Set {set_id}/Map {mapping_id}] OPENAI_API_KEY not found.")
        return None

    try:
        client = OpenAI(api_key=openai_api_key)
    except Exception as e:
        logger.exception(f"[Set {set_id}/Map {mapping_id}] Error initializing OpenAI client: {e}")
        return None

    temp_txt_path = None
    uploaded_file_id = None
    vector_store_id = None

    try:
        # 1. Create language-specific TXT file
        temp_txt_path = _create_lang_pair_txt(csv_path, source_column, target_column, language_code)
        if not temp_txt_path:
            raise ValueError(f"Failed to create temporary TXT file for {language_code}")

        # 2. Upload TXT File to OpenAI
        logger.info(f"[Set {set_id}/Map {mapping_id}] Uploading {temp_txt_path} for {language_code}...")
        with open(temp_txt_path, "rb") as file_handle:
            uploaded_file = client.files.create(file=file_handle, purpose="assistants")
        uploaded_file_id = uploaded_file.id
        logger.info(f"[Set {set_id}/Map {mapping_id}] Uploaded OpenAI File ID: {uploaded_file_id} for {language_code}")

        # 3. Create Vector Store
        vs_name = f"TranslationAssist Set {set_id} - {language_code}"
        logger.info(f"[Set {set_id}/Map {mapping_id}] Creating OpenAI Vector Store: '{vs_name}'...")
        vector_store = client.vector_stores.create(name=vs_name)
        vector_store_id = vector_store.id
        logger.info(f"[Set {set_id}/Map {mapping_id}] Created OpenAI Vector Store ID: {vector_store_id} for {language_code}")

        # 4. Add File to Vector Store (This starts the processing)
        logger.info(f"[Set {set_id}/Map {mapping_id}] Adding File {uploaded_file_id} to Vector Store {vector_store_id}...")
        # Note: Adding the file implicitly starts processing. Handle potential immediate errors.
        try:
            client.vector_stores.files.create(
                vector_store_id=vector_store_id,
                file_id=uploaded_file_id
            )
            logger.info(f"[Set {set_id}/Map {mapping_id}] Successfully initiated file addition to vector store.")
        except APIError as api_err:
             # Sometimes adding the file fails immediately
            logger.error(f"[Set {set_id}/Map {mapping_id}] OpenAI API Error adding file {uploaded_file_id} to VS {vector_store_id}: {api_err}. Code: {api_err.code}")
            # Raise to trigger cleanup
            raise api_err
        except Exception as add_err:
            logger.exception(f"[Set {set_id}/Map {mapping_id}] Error adding file {uploaded_file_id} to VS {vector_store_id}: {add_err}")
            raise add_err
            
        # 5. Monitor Processing
        processing_status = _monitor_file_processing(client, vector_store_id, uploaded_file_id)

        if processing_status is True:
            logger.info(f"[Set {set_id}/Map {mapping_id}] Successfully created and processed Vector Store {vector_store_id} for {language_code}")
            return vector_store_id, uploaded_file_id
        else:
            status_msg = "timed out" if processing_status is None else "failed/cancelled"
            logger.error(f"[Set {set_id}/Map {mapping_id}] File processing {status_msg} for VS {vector_store_id} / File {uploaded_file_id}.")
            # Raise an error to ensure cleanup happens in the except block
            raise ValueError(f"OpenAI file processing {status_msg}")

    except Exception as e:
        logger.exception(f"[Set {set_id}/Map {mapping_id}] ERROR during OpenAI Vector Store creation for {language_code}: {e}")
        # --- Cleanup --- #
        logger.warning(f"[Set {set_id}/Map {mapping_id}] Attempting cleanup for {language_code} after error...")
        if vector_store_id:
            try: 
                client.vector_stores.delete(vector_store_id)
                logger.info(f"  Cleaned up OpenAI VS {vector_store_id}")
            except Exception as del_vs_e: 
                logger.warning(f"  Failed cleanup for OpenAI VS {vector_store_id}: {del_vs_e}")
        # Always try to delete the uploaded file if its ID exists, even if VS deletion failed or VS wasn't created
        if uploaded_file_id:
             try: 
                 client.files.delete(uploaded_file_id)
                 logger.info(f"  Cleaned up OpenAI File {uploaded_file_id}")
             except Exception as del_f_e: 
                 logger.warning(f"  Failed cleanup for OpenAI File {uploaded_file_id}: {del_f_e}")
        return None # Indicate failure

    finally:
        # Clean up the temporary TXT file
        if temp_txt_path and os.path.exists(temp_txt_path):
            try:
                os.remove(temp_txt_path)
                logger.info(f"Cleaned up temporary file: {temp_txt_path}")
            except OSError as e:
                logger.warning(f"Could not remove temporary file {temp_txt_path}: {e}")

# --- Add functions for loading/querying stores later --- # 