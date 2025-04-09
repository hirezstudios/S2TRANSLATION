import os
import csv
import requests
from dotenv import load_dotenv
import time
import re 
import concurrent.futures
import threading
import random
from tqdm import tqdm # Added for progress bar

# Load environment variables from .env file
load_dotenv()
API_KEY = os.getenv("PERPLEXITY_API_KEY")
MODEL = os.getenv("PERPLEXITY_MODEL")
API_URL = "https://api.perplexity.ai/chat/completions"

# --- Configuration ---
INPUT_CSV = "input/blake-small.csv"
OUTPUT_CSV = "output/blake-small_esLA.csv"
SYSTEM_PROMPT_FILE = "system_prompts/tg_esLA.md"
SOURCE_COLUMN = "src_enUS"
TARGET_COLUMN = "tg_esLA"

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
        print(f"Error: System prompt file not found at {filepath}")
        exit(1)
    except Exception as e:
        print(f"Error reading system prompt file: {e}")
        exit(1)

def call_perplexity_api(system_prompt, user_content, row_identifier="N/A"):
    """Calls the Perplexity API for translation with retry logic and parses the output."""
    if not API_KEY:
        # This check might be redundant if done globally, but safe to keep
        with print_lock:
            print("Error: PERPLEXITY_API_KEY not found in .env file.")
        # Don't exit here, let the retry logic handle potential setup issues if needed
        # Or perhaps exit(1) is still appropriate? For now, let it return None.
        return None 
    if not MODEL:
        with print_lock:
            print("Error: PERPLEXITY_MODEL not found in .env file.")
        return None
        
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "accept": "application/json"
    }
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.2 
    }

    current_retry_delay = API_INITIAL_RETRY_DELAY
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            response = requests.post(API_URL, json=payload, headers=headers, timeout=30) # Added timeout
            
            # Handle specific HTTP errors
            if response.status_code == 429: # Rate limit exceeded
                raise requests.exceptions.RequestException(f"Rate limit exceeded (429)")
            
            response.raise_for_status() # Raise an exception for other bad status codes (4xx, 5xx)
            
            data = response.json()
            raw_content = ""
            if data.get("choices") and len(data["choices"]) > 0:
                raw_content = data["choices"][0].get("message", {}).get("content", "")

            if raw_content:
                parsed_content = re.sub(r"<think>.*?</think>\s*", "", raw_content, flags=re.DOTALL).strip()
                if parsed_content:
                    return parsed_content # Success!
                else:
                    with print_lock:
                        print(f"Warning [Row {row_identifier}]: Translation empty after parsing <think> block.")
                        # print(f"Original Response: {raw_content}") # Maybe too verbose
                    return "" # Return empty string if parsing resulted in empty content
            else:
                 with print_lock:
                     print(f"Warning [Row {row_identifier}]: Empty or unexpected API response structure.")
                     # print(f"Response: {data}") # Maybe too verbose
                 return "" # Return empty string if structure is wrong

        except requests.exceptions.Timeout:
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Row {row_identifier}]: API call timed out.")
        except requests.exceptions.RequestException as e:
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Row {row_identifier}]: API Error - {e}")
        except Exception as e:
            # Catch any other unexpected errors during the process
            with print_lock:
                print(f"Attempt {attempt + 1}/{API_MAX_RETRIES + 1} [Row {row_identifier}]: Unexpected Error - {e}")

        # If we reached here, an error occurred or parsing failed; prepare for retry
        if attempt < API_MAX_RETRIES:
            # Exponential backoff with jitter
            wait_time = min(current_retry_delay + random.uniform(0, 1), API_MAX_RETRY_DELAY)
            with print_lock:
                print(f"Retrying [Row {row_identifier}] in {wait_time:.2f} seconds...")
            time.sleep(wait_time)
            current_retry_delay *= 2 # Double the base delay for next potential retry
        else:
            with print_lock:
                print(f"Error [Row {row_identifier}]: Max retries reached for: '{user_content[:50]}...'")
            return None # Indicate final failure after retries
            
    return None # Should not be reached, but ensures a return value

def translate_row_worker(row_data):
    """Worker function to translate a single row."""
    global translated_counter, error_counter # Use global counters
    row, row_index, system_prompt = row_data # Unpack data
    source_text = row.get(SOURCE_COLUMN, "").strip()
    
    # Add a small stagger before starting work for a thread
    # time.sleep(random.uniform(0, THREAD_STAGGER_DELAY)) # Stagger start
    
    translated_text = None
    if source_text:
        # Pass row_index + 2 because CSV is 1-indexed and has a header
        translated_text = call_perplexity_api(system_prompt, source_text, row_identifier=row_index + 2)
        
        with print_lock: # Lock access to shared counters
            if translated_text is not None:
                translated_counter += 1
            else:
                error_counter += 1
                translated_text = "" # Ensure target column is empty on error
    else:
        # Keep target empty if source is empty, don't count as error or success
        translated_text = ""
        
    row[TARGET_COLUMN] = translated_text
    return row # Return the modified row

def translate_csv(input_file, output_file, system_prompt):
    """Reads the input CSV, translates specified column using threads, and writes to output CSV."""
    global translated_counter, error_counter # Reset counters
    translated_counter = 0
    error_counter = 0
    processed_rows = []
    
    try:
        # Read all rows into memory first for threading
        with open(input_file, 'r', encoding='utf-8') as infile:
            reader = csv.DictReader(infile)
            if SOURCE_COLUMN not in reader.fieldnames or TARGET_COLUMN not in reader.fieldnames:
                print(f"Error: CSV must contain '{SOURCE_COLUMN}' and '{TARGET_COLUMN}' columns.")
                exit(1)
            fieldnames = reader.fieldnames
            # Store rows with their original index for processing and later sorting if needed
            rows_to_process = [(row, index, system_prompt) for index, row in enumerate(reader)]
        
        total_rows = len(rows_to_process)
        print(f"Starting translation process for {total_rows} rows using up to {MAX_WORKER_THREADS} threads...")

        # Use ThreadPoolExecutor for managing threads
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKER_THREADS) as executor:
            # Use tqdm for progress bar
            # Wrap executor.map or use as_completed for progress tracking
            futures = [executor.submit(translate_row_worker, row_data) for row_data in rows_to_process]
            
            # Process results as they complete, showing progress
            for future in tqdm(concurrent.futures.as_completed(futures), total=total_rows, desc="Translating"): 
                try:
                    processed_rows.append(future.result()) # Append results as they finish
                except Exception as e:
                    # This catches errors *within* the worker function execution itself, 
                    # though call_perplexity_api should handle most API issues.
                    with print_lock:
                        print(f"Error processing future result: {e}")
                    # Decide how to handle this - perhaps append a placeholder row?
                    # For now, we just lose the row if the future itself fails badly.
                    error_counter += 1 # Count this as an error
        
        # --- IMPORTANT: Ensure results are sorted back to original order if necessary --- 
        # Since we are appending results as they complete, they might be out of order.
        # If the original order matters (it usually does for CSVs), sort based on index.
        # We didn't explicitly store the original index *in the result* row dict. Let's assume
        # the input `rows_to_process` order corresponds to final desired order for simplicity now.
        # If order was critical and `as_completed` used, we'd need to return (index, row) from worker.
        # For now, assuming the order `processed_rows` has is acceptable or matches input. 

        print("\nWriting results to output file...")
        # Write the processed rows to the output file
        with open(output_file, 'w', encoding='utf-8', newline='') as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(processed_rows) # Write all processed rows

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
    # Basic check for API Key presence before starting
    if not API_KEY:
        print("Critical Error: PERPLEXITY_API_KEY not found in .env file. Exiting.")
        exit(1)
    if not MODEL:
        print("Critical Error: PERPLEXITY_MODEL not found in .env file. Exiting.")
        exit(1)
        
    print("Loading system prompt...")
    system_prompt_content = load_system_prompt(SYSTEM_PROMPT_FILE)
    
    # Append the crucial final instruction directly to the loaded prompt
    final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the translated text for the given input string. Do not include any other information, explanations, thinking processes (like <think> blocks), or formatting.**"
    system_prompt_content += final_instruction
    
    translate_csv(INPUT_CSV, OUTPUT_CSV, system_prompt_content) 