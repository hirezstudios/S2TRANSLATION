# SMITE 2 Translation Helper (Flask Version)

This tool facilitates the translation of game text strings for SMITE 2 using various large language models (LLMs), providing a web interface for managing the translation workflow.

**Current Status:** Web UI allows uploading CSV files, configuring and running ONE_STAGE or THREE_STAGE translation jobs using different LLMs (Perplexity, OpenAI, Gemini) via a Flask backend, tracking progress via database polling, viewing batch history, viewing/editing translation results, viewing/editing prompt rules, and exporting results.

## Architecture

This application uses a Flask + Python Threading + Frontend Polling architecture:

1.  **Flask Web Server (`app.py`):** Handles HTTP requests, serves the HTML frontend, manages background job initiation, provides status, results, rules editing, and export endpoints.
2.  **Frontend (`templates/`):** HTML interface using Bootstrap. JavaScript handles file uploads, dynamic language selection, form submission via Fetch API, polling the status endpoint, and interactive tables/editors (Tabulator, CodeMirror). Key templates:
    *   `base.html`: Base structure and navigation.
    *   `index.html`: Home page for upload, configuration, and job start.
    *   `history.html`: Displays past batch jobs.
    *   `results.html`: Displays and allows editing of translation results for a specific batch using Tabulator.
    *   `rules_list.html`: Lists available prompt rule files.
    *   `rules_view.html`: Displays a selected rule file.
    *   `rules_edit.html`: Allows editing of a selected rule file using CodeMirror.
3.  **Backend Logic (`src/`):**
    *   `translation_service.py`: Orchestrates batch preparation, starts background processing threads, handles export generation.
    *   `config.py`: Loads and manages configuration from the `.env` file (API keys, default models, paths, etc.).
    *   `db_manager.py`: Handles all interactions with the SQLite database.
    *   `prompt_manager.py`: Loads global rules, language-specific rules, and stage templates; constructs prompts dynamically; discovers available languages based on CSV headers and configuration.
    *   `api_clients.py`: Initializes API clients (Perplexity, OpenAI, Gemini).
4.  **SQLite Database (`output/translation_jobs.db`):** Stores batch information, individual translation task details, status, intermediate results, and final translations.
5.  **Configuration (`.env`):** Stores API keys, model names, language mappings, database path, etc. **Must be created manually and is not tracked by Git.**
6.  **Prompts (`system_prompts/`):** Contains rule files and templates used for LLM prompts.
    *   `global.md`: Common rules applied to all languages.
    *   `tg_*.md`: Language-specific rules (Stage 1).
    *   `stage*.md`: Templates for multi-stage processing.
    *   `archive/`: Automatically stores previous versions of rule files when edited via the UI.

## Features (Current)

*   **CSV Upload:** Upload input files containing source text (`src_enUS`) and target language columns (`tg_XXXX`).
*   **Dynamic Language Detection:** Identifies potentially translatable languages from input file columns and configuration.
*   **Mode Selection:** Choose between `ONE_STAGE` or `THREE_STAGE` translation processes.
*   **API/Model Configuration:** Select API providers and optionally override default models for each stage.
*   **Background Processing:** Translation jobs run in background threads.
*   **Database State:** Job progress and results stored persistently in SQLite.
*   **Status Monitoring:** UI polling updates status and progress bar on the Home page.
*   **Batch History:** View a table of past translation jobs.
*   **View/Edit Results:** Review and edit translations in an interactive table (Tabulator) with status tracking and re-translation capability.
*   **View/Edit Prompt Rules:** List, view, and edit the content of prompt rule files (`global.md`, `tg_*.md`, stage templates) directly in the UI using a code editor (CodeMirror), with automatic archiving of previous versions.
*   **Audit Logging:** Detailed JSONL file (`output/translation_audit_log.jsonl`) records steps for each task.
*   **Results Export:** Download final approved translations and detailed stage reports (for 3-stage) in CSV format.

## Setup and Running

1.  **Prerequisites:** Python 3.9+, Git.
2.  **Clone Repository:** `git clone <repo_url> && cd <repo_dir>`
3.  **Create Virtual Environment:** `python3 -m venv .venv && source .venv/bin/activate` (or `.venv\Scripts\activate` on Windows)
4.  **Install Dependencies:** `pip install -r requirements.txt`
5.  **Configure Environment (`.env`):** Copy `.env.example` to `.env` and fill in API keys (Perplexity, OpenAI, Gemini). Review other settings.
6.  **Initialize Database:** `python -m src.db_manager` (or happens automatically on first run).
7.  **Run Flask Server:** `flask run`.
8.  Access at `http://127.0.0.1:5000`.

## Usage

1.  **Home Page:** Upload CSV, select languages (using Select/Unselect All if needed), choose mode, configure APIs, start job.
2.  **Monitor:** Track progress on the Home page.
3.  **Batch History:** View past jobs.
4.  **Results Page:** Access via links on Home/History. Review/edit translations. Use action buttons (Approve Original, Deny, Reset, Retranslate) or edit cells directly. Save changes.
5.  **Rules Page:** Navigate to the "Rules" tab. View rule file contents. Click "Edit Rule", make changes in the editor, and click "Save Changes" (previous version is archived).
6.  **Export:** Download results from the Results page.

## Project Structure

```
├── app.py                  # Main Flask application
├── requirements.txt        # Python dependencies
├── .env.example            # Example environment file
├── .env                    # Environment variables (API keys, config - **DO NOT COMMIT**)
├── .gitignore              # Files/directories ignored by git
├── README.md               # This file
├── pytest.ini              # Pytest configuration
├── input/                  # Directory for sample input files (Optional)
├── output/                 # Directory for generated files (Created automatically, ignored by git)
├── src/                    # Backend Python modules
├── system_prompts/         # Directory for prompt files
│   ├── global.md
│   ├── tg_*.md
│   ├── stage*.md
│   └── archive/            # Archived versions of edited rules
├── templates/              # HTML templates for Flask
│   ├── base.html
│   ├── index.html
│   ├── history.html
│   ├── results.html
│   ├── rules_list.html
│   ├── rules_view.html
│   └── rules_edit.html
├── tests/                  # Directory for test files (Currently minimal)
└── temp_uploads/           # Temporary storage for uploaded files (Created automatically, ignored by git)
``` 