import os
import csv
import requests
from dotenv import load_dotenv
import time
import re # Add import for regex

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
# Add a small delay between API calls to avoid rate limiting
REQUEST_DELAY_SECONDS = 1 
# --- End Configuration ---

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

def call_perplexity_api(system_prompt, user_content):
    """Calls the Perplexity API for translation and parses the output."""
    if not API_KEY:
        print("Error: PERPLEXITY_API_KEY not found in .env file.")
        exit(1)
    if not MODEL:
        print("Error: PERPLEXITY_MODEL not found in .env file.")
        exit(1)
        
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
        "temperature": 0.2 # Keep it deterministic for translation
    }

    try:
        response = requests.post(API_URL, json=payload, headers=headers)
        response.raise_for_status() # Raise an exception for bad status codes
        data = response.json()
        
        # --- Remove START TEMP DEBUGGING ---
        # print("--- Raw API Response ---")
        # print(data)
        # print("--- End Raw API Response ---")
        # --- Remove END TEMP DEBUGGING ---

        # Extract the raw content from the response
        raw_content = "" # Default to empty string
        if data.get("choices") and len(data["choices"]) > 0:
            raw_content = data["choices"][0].get("message", {}).get("content", "")

        # Parse the content to remove <think> blocks
        if raw_content:
            # Use regex to remove the <think> block and any surrounding newlines/whitespace
            parsed_content = re.sub(r"<think>.*?</think>\s*", "", raw_content, flags=re.DOTALL).strip()
            
            if parsed_content:
                return parsed_content
            else:
                print(f"Warning: Translation was empty after removing <think> block for: {user_content}")
                print(f"Original Response Content: {raw_content}")
                return "" # Return empty string if parsing resulted in empty content
        else:
             print(f"Warning: Empty or unexpected API response structure for: {user_content}")
             print(f"Response: {data}")
             return "" # Return empty string if structure is wrong

    except requests.exceptions.RequestException as e:
        print(f"Error calling Perplexity API: {e}")
        # Consider adding retry logic here if needed
        return None # Indicate API call failure
    except Exception as e:
        print(f"An unexpected error occurred during API call: {e}")
        return None


def translate_csv(input_file, output_file, system_prompt):
    """Reads the input CSV, translates specified column, and writes to output CSV."""
    try:
        with open(input_file, 'r', encoding='utf-8') as infile, \
             open(output_file, 'w', encoding='utf-8', newline='') as outfile:
            
            reader = csv.DictReader(infile)
            if SOURCE_COLUMN not in reader.fieldnames or TARGET_COLUMN not in reader.fieldnames:
                print(f"Error: CSV must contain '{SOURCE_COLUMN}' and '{TARGET_COLUMN}' columns.")
                exit(1)

            writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
            writer.writeheader()

            print(f"Starting translation process for {input_file}...")
            row_count = 0
            translated_count = 0
            
            # --- Remove START TEMP DEBUGGING ---
            # # Process only the first data row (after header)
            # first_row = next(reader, None) 
            # if not first_row:
            #     print("Error: CSV is empty or has only a header.")
            #     exit(1)
            # --- Remove END TEMP DEBUGGING ---

            # --- Remove START TEMP DEBUGGING ---
            # row_count = 1 # Since we are only processing one row
            # --- Remove END TEMP DEBUGGING ---
            
            for row in reader: # Restore loop for all rows
                row_count += 1 # Restore row counting

            # --- Remove START TEMP DEBUGGING ---
            # # Use the first row data
            # row = first_row 
            # --- Remove END TEMP DEBUGGING ---

                source_text = row.get(SOURCE_COLUMN, "").strip()
                
                if source_text:
                    print(f"Translating row {row_count}: '{source_text[:50]}...'")
                    # --- Remove START TEMP DEBUGGING ---
                    # # Directly call and print the result, don't write to file yet
                    translation = call_perplexity_api(system_prompt, source_text)
                    # print(f"--- Translation Result ---")
                    # --- Remove END TEMP DEBUGGING ---
                    
                    if translation is not None: # Check includes empty string now
                        # --- Remove START TEMP DEBUGGING ---
                        # print(translation)
                        # --- Remove END TEMP DEBUGGING ---
                        row[TARGET_COLUMN] = translation # Assign parsed translation
                        translated_count += 1
                    else:
                        # --- Remove START TEMP DEBUGGING ---
                        # print("API call failed.")
                        # --- Remove END TEMP DEBUGGING ---
                        # Handle API call failure (returned None)
                        print(f"Skipping translation for row {row_count} due to API error.")
                        row[TARGET_COLUMN] = "" # Keep target empty on error
                    # --- Remove START TEMP DEBUGGING ---
                    # print(f"--- End Translation Result ---")
                    # --- Remove END TEMP DEBUGGING ---

                    # Add delay between requests
                    time.sleep(REQUEST_DELAY_SECONDS) # Restore delay
                else:
                    print(f"Skipping row {row_count}: Source text is empty.")
                    row[TARGET_COLUMN] = ""

                # --- Remove START TEMP DEBUGGING ---
                # Skip writing to file for this test
                writer.writerow(row) # Restore writing row
                # --- Remove END TEMP DEBUGGING ---

            # --- Remove START TEMP DEBUGGING ---
            # print(f"\nTranslation process completed for single row test.")
            # --- Remove END TEMP DEBUGGING ---
            print(f"\nTranslation process completed.") # Restore original message
            print(f"Total rows processed: {row_count}")
            print(f"Rows translated: {translated_count}")
            # --- Remove START TEMP DEBUGGING ---
            # # print(f"Output saved to: {output_file}") # No output saved in this test
            # --- Remove END TEMP DEBUGGING ---
            print(f"Output saved to: {output_file}") # Restore original message

    except FileNotFoundError:
        print(f"Error: Input CSV file not found at {input_file}")
        exit(1)
    except Exception as e:
        print(f"An error occurred during CSV processing: {e}")
        exit(1)

if __name__ == "__main__":
    print("Loading system prompt...")
    system_prompt_content = load_system_prompt(SYSTEM_PROMPT_FILE)
    
    # Append the crucial final instruction directly to the loaded prompt
    final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the translated text for the given input string. Do not include any other information, explanations, thinking processes (like <think> blocks), or formatting.**"
    system_prompt_content += final_instruction
    
    translate_csv(INPUT_CSV, OUTPUT_CSV, system_prompt_content) 