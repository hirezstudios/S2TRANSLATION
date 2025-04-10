import os
import glob
from . import config
import logging

logger = logging.getLogger(__name__)

# Define path for user overrides
USER_PROMPT_DIR = "user_prompts" 

global_rules_content = None
stage1_templates = {}
stage2_template_prompt = None
stage3_template_prompt = None
available_languages = []

def discover_available_languages():
    """Discovers available languages by scanning for base stage 1 prompt files in system_prompts/."""
    global available_languages
    available_languages = []
    # IMPORTANT: Discovery MUST be based on system prompts, not user overrides
    pattern = config.STAGE1_PROMPT_FILE_TPL.format(lang_code='*') 
    logger.info(f"Scanning for base language prompts using pattern: {pattern}")
    try:
        prompt_files = glob.glob(pattern)
        for filepath in prompt_files:
            # Extract lang_code from filename like 'system_prompts/tg_enGB.md'
            filename = os.path.basename(filepath)
            # Ensure it's directly in system_prompts, not archive or elsewhere
            if os.path.dirname(filepath) == config.SYSTEM_PROMPT_DIR:
                parts = filename.split('_')
                # Corrected check for 'tg_' prefix and '.md' suffix
                if len(parts) == 2 and parts[0] == 'tg' and parts[1].endswith('.md'):
                    lang_code = parts[1][:-3] # Remove .md extension
                    if lang_code not in available_languages:
                         available_languages.append(lang_code)
                         logger.info(f"Discovered available language based on system prompt: {lang_code} from {filepath}")
                else:
                     logger.warning(f"Skipping file with unexpected format in system_prompts: {filepath}")
            else:
                logger.debug(f"Ignoring file outside direct system_prompts directory during discovery: {filepath}")

        if not available_languages:
             logger.warning("No language-specific Stage 1 prompt files found in system_prompts.")
    except Exception as e:
         logger.error(f"Error scanning for language prompts: {e}")
    logger.info(f"Available languages discovered based on system prompts: {available_languages}")
    return available_languages

def load_single_prompt_file(system_filepath):
    """
    Loads content from a single prompt file, checking USER_PROMPT_DIR first.
    Args:
        system_filepath: The path to the file in the system_prompts directory.
    Returns:
        The content of the file as a string, or None if not found or error.
    """
    if not system_filepath:
        logger.error("load_single_prompt_file called with empty system_filepath")
        return None
        
    base_filename = os.path.basename(system_filepath)
    user_filepath = os.path.join(USER_PROMPT_DIR, base_filename)

    # Check user override first
    if os.path.exists(user_filepath):
        try:
            with open(user_filepath, 'r', encoding='utf-8') as f:
                logger.info(f"Loading USER override for {base_filename} from {user_filepath}")
                return f.read()
        except Exception as e:
            logger.error(f"Error reading user prompt file {user_filepath}: {e}")
            # Fallback to system path might hide user file errors, so return None.
            return None # Indicate read failure of user file

    # If no user override, check system path
    elif os.path.exists(system_filepath):
        try:
            with open(system_filepath, 'r', encoding='utf-8') as f:
                logger.debug(f"Loading SYSTEM prompt file: {system_filepath}")
                return f.read()
        except Exception as e:
            logger.error(f"Error reading system prompt file {system_filepath}: {e}")
            return None # Indicate read failure of system file
    
    # If neither exists
    else:
        # This case should be rare if discover_available_languages is working,
        # but could happen for global/stage templates if misconfigured.
        logger.warning(f"Prompt file not found in user ({user_filepath}) or system ({system_filepath}) paths.")
        return None

def load_prompts():
    """Loads global rules, language-specific stage 1 bases, and stage 2/3 templates."""
    global global_rules_content, stage1_templates, stage2_template_prompt, stage3_template_prompt, available_languages

    logger.info("Discovering available languages based on system prompts...")
    discover_available_languages() # Based on system_prompts/

    logger.info(f"Loading Global Rules (checking user override for {os.path.basename(config.GLOBAL_RULES_FILE)})...")
    global_rules_content = load_single_prompt_file(config.GLOBAL_RULES_FILE)
    if not global_rules_content:
        logger.error(f"Critical Error: Global rules file {config.GLOBAL_RULES_FILE} is missing or empty in both system and user paths. Exiting.")
        exit(1) # Can't run without global rules

    logger.info("Loading language-specific Stage 1 prompts (checking user overrides)...")
    stage1_templates = {}
    # Only attempt to load prompts for languages discovered from base files
    for lang_code in available_languages: 
        system_filepath = config.STAGE1_PROMPT_FILE_TPL.format(lang_code=lang_code)
        content = load_single_prompt_file(system_filepath) # Checks user_prompts/tg_*.md first
        if content:
            stage1_templates[lang_code] = content
            # Logger inside load_single_prompt_file indicates if system or user was loaded
        else:
            # This means neither user nor system file could be loaded for this discovered language
            logger.error(f"Critical Error: Could not load Stage 1 prompt content for discovered language: {lang_code} from {system_filepath} or user override. This language will be unavailable.")
            # Should we remove it from available_languages here? Or let it fail later?
            # Let's remove it to prevent further errors.
            if lang_code in available_languages:
                 available_languages.remove(lang_code)

    if not stage1_templates:
         logger.warning("No Stage 1 prompts could be loaded for any discovered language.")
            
    if config.TRANSLATION_MODE == "THREE_STAGE":
        logger.info(f"Loading Stage 2 Template (checking user override for {os.path.basename(config.STAGE2_TEMPLATE_FILE)})...")
        stage2_template_prompt = load_single_prompt_file(config.STAGE2_TEMPLATE_FILE)
        if not stage2_template_prompt:
             logger.error(f"Critical Error: Stage 2 template {config.STAGE2_TEMPLATE_FILE} missing in both system/user paths for THREE_STAGE mode. Exiting.")
             exit(1)
             
        logger.info(f"Loading Stage 3 Template (checking user override for {os.path.basename(config.STAGE3_TEMPLATE_FILE)})...")
        stage3_template_prompt = load_single_prompt_file(config.STAGE3_TEMPLATE_FILE)
        if not stage3_template_prompt:
             logger.error(f"Critical Error: Stage 3 template {config.STAGE3_TEMPLATE_FILE} missing in both system/user paths for THREE_STAGE mode. Exiting.")
             exit(1)
             
    logger.info("Prompt loading complete.")

def get_full_prompt(stage, lang_code, source_text=None, initial_translation=None, feedback=None):
    """Constructs the full system prompt for a given stage and language."""
    
    # Final instruction (applied to all stages)
    final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the final text (translation/evaluation/revision). No extra text, formatting, or explanations.**"
    
    # Get base language rules
    lang_specific_part = stage1_templates.get(lang_code)
    if not lang_specific_part:
        raise ValueError(f"Language specific prompt for {lang_code} not loaded.")
    lang_name = config.LANGUAGE_NAME_MAP.get(lang_code, lang_code)
    lang_specific_part = lang_specific_part.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)
        
    full_ruleset = f"{lang_specific_part}\n\n{global_rules_content}"

    if stage == 1:
        return full_ruleset + final_instruction
    
    elif stage == 2:
        if not stage2_template_prompt:
            raise ValueError("Stage 2 template not loaded.")
        prompt = stage2_template_prompt.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)\
                                       .replace("<<RULES>>", full_ruleset)\
                                       .replace("<<SOURCE_TEXT>>", source_text or "")\
                                       .replace("<<INITIAL_TRANSLATION>>", initial_translation or "")
        return prompt + final_instruction
        
    elif stage == 3:
        if not stage3_template_prompt:
            raise ValueError("Stage 3 template not loaded.")
        prompt = stage3_template_prompt.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)\
                                       .replace("<<RULES>>", full_ruleset)\
                                       .replace("<<SOURCE_TEXT>>", source_text or "")\
                                       .replace("<<INITIAL_TRANSLATION>>", initial_translation or "")\
                                       .replace("<<FEEDBACK>>", feedback or "")
        return prompt + final_instruction
        
    else:
        raise ValueError(f"Invalid stage number: {stage}")

# Load prompts when module is imported
load_prompts() 