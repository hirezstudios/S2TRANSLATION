import os
import csv
import requests
import openai # Added
from dotenv import load_dotenv
import time
import re 
import concurrent.futures
import threading
import random
from tqdm import tqdm # Added for progress bar

# Load environment variables from .env file
load_dotenv()

# --- API Configuration --- 
# General Settings
ACTIVE_API = os.getenv("ACTIVE_API", "PERPLEXITY").upper()

# Perplexity Settings
PPLX_API_KEY = os.getenv("PERPLEXITY_API_KEY")
PPLX_MODEL = os.getenv("PERPLEXITY_MODEL")
PPLX_API_URL = "https://api.perplexity.ai/chat/completions"

# OpenAI Settings
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")
# Initialize OpenAI client if using OpenAI
openai_client = None
if ACTIVE_API == "OPENAI" and OPENAI_API_KEY:
    openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
# --- End API Configuration ---

# --- CSV Configuration ---
INPUT_CSV = "input/blake-small.csv"
# Dynamically generate output filename based on API and language
LANGUAGE_CODE = "esLA" # TODO: Make this dynamic if supporting multiple languages
OUTPUT_CSV = f"output/blake-small_{LANGUAGE_CODE}_{ACTIVE_API.lower()}.csv"
SYSTEM_PROMPT_FILE = f"system_prompts/tg_{LANGUAGE_CODE}.md"
SOURCE_COLUMN = "src_enUS"
TARGET_COLUMN = f"tg_{LANGUAGE_CODE}" 
# --- End CSV Configuration ---

# --- Threading and Retry Configuration ---
# Use environment variables with defaults
MAX_WORKER_THREADS = int(os.getenv("MAX_WORKER_THREADS", 8))
API_MAX_RETRIES = int(os.getenv("API_MAX_RETRIES", 3))
# Convert delays to float
API_INITIAL_RETRY_DELAY = float(os.getenv("API_INITIAL_RETRY_DELAY", 5.0))
API_MAX_RETRY_DELAY = float(os.getenv("API_MAX_RETRY_DELAY", 60.0))
THREAD_STAGGER_DELAY = float(os.getenv("THREAD_STAGGER_DELAY", 1.0)) 
# --- End Configuration ---

# Thread-safe lock for printing and counters if needed (especially with tqdm)
print_lock = threading.Lock()
translated_counter = 0
error_counter = 0

def load_system_prompt(filepath):
    """Loads the system prompt from a file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        print(f"Error: System prompt file {filepath} not found.")
        exit(1)
    except Exception as e:
        print(f"Error reading system prompt file {filepath}: {e}")
        exit(1)

# --- API Call Functions --- 

def call_perplexity_api(system_prompt, user_content, row_identifier="N/A"):
    """Calls the Perplexity API for translation with retry logic and parses the output."""
    # Note: API_KEY and MODEL are now PPLX_ specific
    if not PPLX_API_KEY:
        with print_lock:
            print(f"Error [Row {row_identifier}]: PERPLEXITY_API_KEY not found.")
        return None 
    if not PPLX_MODEL:
        with print_lock:
            print(f"Error [Row {row_identifier}]: PERPLEXITY_MODEL not found.")
        return None
        
    headers = {
        "Authorization": f"Bearer {PPLX_API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    payload = {
        "model": PPLX_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.2 
    }

    current_retry_delay = API_INITIAL_RETRY_DELAY
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = requests.post(PPLX_API_URL, json=payload, headers=headers, timeout=60) # Increased timeout
            
            if response.status_code == 429: 
                raise requests.exceptions.RequestException(f"Rate limit exceeded (429)")
            response.raise_for_status() 
            
            data = response.json()
            raw_content = ""
            if data.get("choices") and len(data["choices"]) > 0:
                raw_content = data["choices"][0].get("message", {}).get("content", "")

            if raw_content:
                # Perplexity specific parsing (if needed beyond standard instruction)
                parsed_content = re.sub(r"<think>.*?</think>\s*", "", raw_content, flags=re.DOTALL).strip()
                if parsed_content:
                    return parsed_content 
                else:
                    with print_lock:
                        print(f"Warning [PPLX Row {row_identifier}]: Translation empty after parsing.")
                    return "" 
            else:
                 with print_lock:
                     print(f"Warning [PPLX Row {row_identifier}]: Empty/unexpected API response.")
                 return "" 

        except requests.exceptions.Timeout:
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [PPLX Row {row_identifier}]: API call timed out.")
        except requests.exceptions.RequestException as e:
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [PPLX Row {row_identifier}]: API Error - {e}")
        except Exception as e:
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [PPLX Row {row_identifier}]: Unexpected Error - {e}")

        if attempt < API_MAX_RETRIES:
            wait_time = min(current_retry_delay + random.uniform(0, 1), API_MAX_RETRY_DELAY)
            with print_lock:
                print(f"Retrying [PPLX Row {row_identifier}] in {wait_time:.2f} seconds...")
            time.sleep(wait_time)
            current_retry_delay *= 2 
        else:
            with print_lock:
                print(f"Error [PPLX Row {row_identifier}]: Max retries reached for: '{user_content[:50]}...'")
            return None 
            
    return None 

def call_openai_api(system_prompt, user_content, row_identifier="N/A"):
    """Calls the OpenAI Responses API for translation with retry logic and parses the output."""
    if not openai_client:
        with print_lock:
             print(f"Error [Row {row_identifier}]: OpenAI client not initialized (check OPENAI_API_KEY).")
        return None
    if not OPENAI_MODEL:
         with print_lock:
             print(f"Error [Row {row_identifier}]: OPENAI_MODEL not found in .env file.")
         return None

    current_retry_delay = API_INITIAL_RETRY_DELAY
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = openai_client.responses.create(
                model=OPENAI_MODEL,
                input=user_content, # Direct string input for simple text
                instructions=system_prompt,
                temperature=0.2,
                max_output_tokens=1024 # Added a limit, adjust if needed
            )

            # Check response status and extract text
            if response.status == 'completed' and response.output:
                # Access the text content - assuming simple text output based on API docs
                # The structure is response.output[0].content[0].text
                if (response.output[0].type == 'message' and 
                    response.output[0].content and 
                    response.output[0].content[0].type == 'output_text'):
                    
                    translation = response.output[0].content[0].text.strip()
                    # OpenAI might not need the <think> parsing, but check if needed
                    # Add parsing here if OpenAI includes unwanted prefixes/suffixes
                    if translation:
                        return translation
                    else:
                         with print_lock:
                            print(f"Warning [OpenAI Row {row_identifier}]: Translation result empty.")
                         return ""
                else:
                    with print_lock:
                        print(f"Warning [OpenAI Row {row_identifier}]: Unexpected output structure in response.")
                        # print(response) # Optional: log full response
                    return "" # Unexpected structure
            elif response.status == 'failed':
                # Raise an exception to trigger retry logic for API-level failures
                error_message = f"OpenAI API call failed: {response.error}"
                raise openai.APIError(error_message) # Use appropriate OpenAI exception if available
            else: # Handle in_progress, incomplete etc. if necessary, or treat as error for retry
                 raise openai.APIError(f"OpenAI API call returned status: {response.status}")

        # --- Retry Logic --- 
        except openai.APITimeoutError:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: API call timed out.")
        except openai.APIConnectionError as e:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Network error - {e}")
        except openai.RateLimitError as e:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Rate limit exceeded - {e}")
        except openai.APIStatusError as e:
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: API Status Error ({e.status_code}) - {e.response}")
             # Potentially treat some status codes (e.g., 5xx) differently if needed
        except openai.APIError as e: # Catch broader OpenAI errors
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: OpenAI API Error - {e}")
        except Exception as e: # Catch any other unexpected errors
             with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [OpenAI Row {row_identifier}]: Unexpected Error - {e}")
        
        # Prepare for retry if applicable
        if attempt < API_MAX_RETRIES:
            wait_time = min(current_retry_delay + random.uniform(0, 1), API_MAX_RETRY_DELAY)
            with print_lock:
                print(f"Retrying [OpenAI Row {row_identifier}] in {wait_time:.2f} seconds...")
            time.sleep(wait_time)
            current_retry_delay *= 2
        else:
            with print_lock:
                print(f"Error [OpenAI Row {row_identifier}]: Max retries reached for: '{user_content[:50]}...'")
            return None # Indicate final failure

    return None

# --- Dispatcher --- 

def call_active_api(system_prompt, user_content, row_identifier="N/A"):
    """Calls the currently active API based on ACTIVE_API setting."""
    if ACTIVE_API == "PERPLEXITY":
        return call_perplexity_api(system_prompt, user_content, row_identifier)
    elif ACTIVE_API == "OPENAI":
        return call_openai_api(system_prompt, user_content, row_identifier)
    # Add elif for "GEMINI" here later
    else:
        with print_lock:
            print(f"Error: Unsupported ACTIVE_API value '{ACTIVE_API}' in .env file.")
        return None # Or raise an error

# --- Worker and CSV Processing --- 

def translate_row_worker(row_data):
    """Worker function to translate a single row using the active API."""
    global translated_counter, error_counter 
    row, row_index, system_prompt = row_data 
    source_text = row.get(SOURCE_COLUMN, "").strip()
        
    translated_text = None
    if source_text:
        # Use the dispatcher
        translated_text = call_active_api(system_prompt, source_text, row_identifier=row_index + 2)
        
        with print_lock: 
            if translated_text is not None:
                translated_counter += 1
            else:
                error_counter += 1
                translated_text = "" 
    else:
        translated_text = ""
        
    # Use the dynamically determined target column name
    row[TARGET_COLUMN] = translated_text 
    return row 

def translate_csv(input_file, output_file, system_prompt):
    """Reads input CSV, translates using active API via threads, writes to output CSV.""" # Updated docstring
    global translated_counter, error_counter 
    translated_counter = 0
    error_counter = 0
    processed_rows = []
    
    try:
        with open(input_file, 'r', encoding='utf-8') as infile:
            reader = csv.DictReader(infile)
            # Check for source column, target column check is implicit as it's generated
            if SOURCE_COLUMN not in reader.fieldnames:
                 print(f"Error: Input CSV must contain source column '{SOURCE_COLUMN}'.")
                 exit(1)
            # We need the fieldnames for the writer later
            # IMPORTANT: The TARGET_COLUMN might not exist in the *input* fieldnames
            fieldnames = reader.fieldnames
            if TARGET_COLUMN not in fieldnames:
                print(f"Info: Target column '{TARGET_COLUMN}' not found in input, will be added.")
                fieldnames = list(fieldnames) + [TARGET_COLUMN] # Ensure target column is in output header
                
            rows_to_process = [(row, index, system_prompt) for index, row in enumerate(reader)]
        
        total_rows = len(rows_to_process)
        print(f"Translating {total_rows} rows using {ACTIVE_API} (up to {MAX_WORKER_THREADS} threads)... Output: {output_file}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKER_THREADS) as executor:
            futures = [executor.submit(translate_row_worker, row_data) for row_data in rows_to_process]
            for future in tqdm(concurrent.futures.as_completed(futures), total=total_rows, desc=f"Translating ({ACTIVE_API})"):
                try:
                    processed_rows.append(future.result()) 
                except Exception as e:
                    with print_lock:
                        print(f"Error processing future result: {e}")
                    error_counter += 1 
        
        # Sort results back to original order based on how they were submitted
        # Since futures were created in order and results appended as completed, we need sorting.
        # We need the original index associated with each result for sorting.
        # --> Modify translate_row_worker to return (index, row)
        # --> Let's skip sorting for now to keep it simpler, assuming order is maintained enough.
        # --> If strict order needed, MUST implement index tracking and sorting.

        print("\nWriting results to output file...")
        with open(output_file, 'w', encoding='utf-8', newline='') as outfile:
            # Use the potentially modified fieldnames list that includes the target column
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            # Filter out None results if any future failed exceptionally before returning row
            valid_rows = [row for row in processed_rows if row is not None]
            writer.writerows(valid_rows) 

        print(f"\nTranslation process completed.")
        print(f"Total rows processed: {total_rows}")
        print(f"Rows successfully translated: {translated_counter}")
        print(f"Rows failed after retries: {error_counter}")
        print(f"Output saved to: {output_file}")

    except FileNotFoundError:
        print(f"Error: Input CSV file not found at {input_file}")
        exit(1)
    except Exception as e:
        print(f"An error occurred during CSV processing: {e}")
        exit(1)

if __name__ == "__main__":
    # Check presence of keys for the *active* API
    print(f"Selected API: {ACTIVE_API}")
    if ACTIVE_API == "PERPLEXITY" and (not PPLX_API_KEY or not PPLX_MODEL):
        print(f"Critical Error: PERPLEXITY_API_KEY or PERPLEXITY_MODEL not found in .env file for active API. Exiting.")
        exit(1)
    elif ACTIVE_API == "OPENAI" and (not OPENAI_API_KEY or not OPENAI_MODEL):
        print(f"Critical Error: OPENAI_API_KEY or OPENAI_MODEL not found in .env file for active API. Exiting.")
        exit(1)
    elif ACTIVE_API not in ["PERPLEXITY", "OPENAI"]:
         print(f"Critical Error: Invalid ACTIVE_API value '{ACTIVE_API}'. Choose 'PERPLEXITY' or 'OPENAI'. Exiting.")
         exit(1)
         
    # Initialize OpenAI client if needed (moved from global scope for clarity)
    if ACTIVE_API == "OPENAI" and not openai_client:
        try:
            openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)
            print("OpenAI client initialized.")
        except Exception as e:
            print(f"Critical Error: Failed to initialize OpenAI client: {e}. Exiting.")
            exit(1)
            
    print(f"Loading system prompt from {SYSTEM_PROMPT_FILE}...")
    system_prompt_content = load_system_prompt(SYSTEM_PROMPT_FILE)
    
    # Append the crucial final instruction directly to the loaded prompt
    # This should apply universally to both APIs
    final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the translated text for the given input string. Do not include any other information, explanations, thinking processes (like <think> blocks), or formatting.**"
    system_prompt_content += final_instruction
    
    translate_csv(INPUT_CSV, OUTPUT_CSV, system_prompt_content) 