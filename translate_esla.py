import os
import csv
import requests
from dotenv import load_dotenv
import time

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
    """Calls the Perplexity API for translation."""
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
        
        # Extract the translation from the response
        # Adjust this based on the actual API response structure if needed
        if data.get("choices") and len(data["choices"]) > 0:
            translation = data["choices"][0].get("message", {}).get("content")
            if translation:
                return translation.strip()
            else:
                print(f"Warning: Empty translation received for: {user_content}")
                return "" # Return empty string for empty translation
        else:
            print(f"Warning: Unexpected API response structure for: {user_content}")
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
            
            for row in reader:
                row_count += 1
                source_text = row.get(SOURCE_COLUMN, "").strip()
                
                # Only translate if source text is not empty
                if source_text:
                    print(f"Translating row {row_count}: '{source_text[:50]}...'")
                    translation = call_perplexity_api(system_prompt, source_text)
                    
                    if translation is not None:
                        row[TARGET_COLUMN] = translation
                        translated_count +=1
                    else:
                        # Keep original empty value or handle error case if needed
                        print(f"Skipping translation for row {row_count} due to API error.")
                        row[TARGET_COLUMN] = "" # Or keep original content: row.get(TARGET_COLUMN, "")
                    
                    # Add delay between requests
                    time.sleep(REQUEST_DELAY_SECONDS) 
                else:
                    # Keep target empty if source is empty
                    print(f"Skipping row {row_count}: Source text is empty.")
                    row[TARGET_COLUMN] = ""

                writer.writerow(row)

            print(f"\nTranslation process completed.")
            print(f"Total rows processed: {row_count}")
            print(f"Rows translated: {translated_count}")
            print(f"Output saved to: {output_file}")

    except FileNotFoundError:
        print(f"Error: Input CSV file not found at {input_file}")
        exit(1)
    except Exception as e:
        print(f"An error occurred during CSV processing: {e}")
        exit(1)

if __name__ == "__main__":
    print("Loading system prompt...")
    system_prompt_content = load_system_prompt(SYSTEM_PROMPT_FILE)
    translate_csv(INPUT_CSV, OUTPUT_CSV, system_prompt_content) 