import os
import glob
from . import config
import logging
import re
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# Define path for user overrides
USER_PROMPT_DIR = "user_prompts" 

global_rules_content = None
stage1_templates = {}
stage2_template_prompt = None
stage3_template_prompt = None
stage0_template_prompt = None
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
    """Loads global rules, language-specific stage 1 bases, and stage templates."""
    global global_rules_content, stage1_templates, stage2_template_prompt, stage3_template_prompt, stage0_template_prompt, available_languages

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
            
    # Load Stage 0 Template
    logger.info(f"Loading Stage 0 Template (checking user override for {os.path.basename(config.STAGE0_TEMPLATE_FILE)})...")
    stage0_template_prompt = load_single_prompt_file(config.STAGE0_TEMPLATE_FILE)
    if not stage0_template_prompt:
         logger.error(f"CRITICAL WARNING: Stage 0 template {config.STAGE0_TEMPLATE_FILE} missing in system/user paths. FOUR_STAGE mode will fail.")

    # Load Stage 2 & 3 only if needed (or always? Let's keep existing logic)
    # Original logic checked config.TRANSLATION_MODE, but now we support multiple modes.
    # Let's load S2/S3 always if their files exist, error checking happens in get_full_prompt if needed.
    logger.info(f"Loading Stage 2 Template (checking user override for {os.path.basename(config.STAGE2_TEMPLATE_FILE)})...")
    stage2_template_prompt = load_single_prompt_file(config.STAGE2_TEMPLATE_FILE)
    if not stage2_template_prompt:
            logger.warning(f"Stage 2 template {config.STAGE2_TEMPLATE_FILE} missing. THREE/FOUR stage modes may fail if S2 is required.")
            
    logger.info(f"Loading Stage 3 Template (checking user override for {os.path.basename(config.STAGE3_TEMPLATE_FILE)})...")
    stage3_template_prompt = load_single_prompt_file(config.STAGE3_TEMPLATE_FILE)
    if not stage3_template_prompt:
            logger.warning(f"Stage 3 template {config.STAGE3_TEMPLATE_FILE} missing. THREE/FOUR stage modes may fail if S3 refinement is needed.")
             
    logger.info("Prompt loading complete.")

def get_full_prompt(
    prompt_type: str,
    language_code: str,
    base_variables: Optional[Dict[str, str]] = None,
    stage_variables: Optional[Dict[str, str]] = None,
    batch_prompt: Optional[str] = None,
    generated_glossary: Optional[str] = None
) -> Optional[str]:
    """Constructs the full system prompt for a given stage and language."""
    
    # Final instruction (applied to stages 1, 2, 3)
    final_instruction = "\n\n**IMPORTANT FINAL INSTRUCTION: Your final output should contain ONLY the final text (translation/evaluation/revision). No extra text, formatting, or explanations.**"
    
    # --- Common Setup: Batch Prompt, Language Rules, Global Rules --- 
    batch_prompt_section = ""
    if batch_prompt and batch_prompt.strip():
        batch_prompt_section = f"**BATCH-SPECIFIC INSTRUCTIONS:**\n{batch_prompt.strip()}\n---\n\n"
    
    lang_specific_part = stage1_templates.get(language_code)
    if not lang_specific_part:
        raise ValueError(f"Language specific prompt for {language_code} not loaded.")
    lang_name = config.LANGUAGE_NAME_MAP.get(language_code, language_code)
    lang_specific_part = lang_specific_part.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)
        
    standard_ruleset = f"{batch_prompt_section}{lang_specific_part}\n\n{global_rules_content}"
    # -------------------------------------------------------------------

    # --- Prompt Type Specific Logic --- 
    if prompt_type == "0":
        if not stage0_template_prompt:
             raise ValueError("Stage 0 template (stage0_glossary_template.md) not loaded.")
        # Format S0 instructions
        stage0_instructions = stage0_template_prompt.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)
        # Combine S0 instructions with standard rules
        combined_s0_prompt = f"{stage0_instructions}\n\n**GENERAL TRANSLATION RULES (Apply these when generating glossary):**\n---\n{standard_ruleset}"
        return combined_s0_prompt # Return combined prompt without final_instruction

    else: # Handle stages 1, 2, 3
        # <<< START TEMP LOGGING >>>
        logger.debug(f"get_full_prompt (Type {prompt_type}, Lang {language_code}): Received generated_glossary (len={len(generated_glossary) if generated_glossary else 0})")
        # logger.debug(f"Received Glossary Content:\n{generated_glossary}") # Uncomment for full content
        # <<< END TEMP LOGGING >>>
        
        # Prepend generated glossary if available
        glossary_section = ""
        if generated_glossary and generated_glossary.strip():
            glossary_section = f"**GENERATED GLOSSARY (From Stage 0):**\n{generated_glossary.strip()}\n---\n\n"
        
        # Combine Glossary + Standard Rules for S1/S2/S3 context
        full_ruleset_with_glossary = f"{glossary_section}{standard_ruleset}"
        
        # <<< START TEMP LOGGING >>>
        logger.debug(f"get_full_prompt (Type {prompt_type}, Lang {language_code}): Constructed full_ruleset_with_glossary (len={len(full_ruleset_with_glossary)}). Starts with: {full_ruleset_with_glossary[:100]}...")
        # logger.debug(f"Full Ruleset Content:\n{full_ruleset_with_glossary}") # Uncomment for full content
        # <<< END TEMP LOGGING >>>

        if prompt_type == "1":
            # Stage 1: Ruleset + Final Instruction
            return full_ruleset_with_glossary + final_instruction
        
        elif prompt_type == "2":
            # Stage 2: Template using Ruleset + Base Variables + Final Instruction
            if not stage2_template_prompt:
                raise ValueError("Stage 2 template not loaded.")
            prompt = stage2_template_prompt.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)\
                                           .replace("<<RULES>>", full_ruleset_with_glossary)\
                                           .replace("<<SOURCE_TEXT>>", base_variables.get('source_text') or "")\
                                           .replace("<<INITIAL_TRANSLATION>>", base_variables.get('initial_translation') or "")
            return prompt + final_instruction
            
        elif prompt_type == "3":
            # Stage 3: Template using Ruleset + Base Variables + Stage Variables + Final Instruction
            if not stage3_template_prompt:
                raise ValueError("Stage 3 template not loaded.")
            prompt = stage3_template_prompt.replace("<<TARGET_LANGUAGE_NAME>>", lang_name)\
                                           .replace("<<RULES>>", full_ruleset_with_glossary)\
                                           .replace("<<SOURCE_TEXT>>", base_variables.get('source_text') or "")\
                                           .replace("<<INITIAL_TRANSLATION>>", base_variables.get('initial_translation') or "")\
                                           .replace("<<FEEDBACK>>", stage_variables.get('feedback') or "")
            return prompt + final_instruction
            
        else:
            raise ValueError(f"Invalid prompt type: {prompt_type}")

def parse_evaluation(evaluation_text: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Parses the evaluation text from the LLM to extract the score and feedback.

    Assumes a format like:
    Score: [number]
    Feedback: [text]

    Args:
        evaluation_text: The raw text output from the evaluation LLM call.

    Returns:
        A tuple containing (score, feedback). Score is an int, feedback is a string.
        Returns (None, None) if parsing fails.
    """
    if not evaluation_text:
        return None, None

    score_match = re.search(r"Score:\s*(\d+)", evaluation_text, re.IGNORECASE)
    feedback_match = re.search(r"Feedback:\s*(.*)", evaluation_text, re.IGNORECASE | re.DOTALL) # DOTALL allows feedback to span multiple lines

    score = int(score_match.group(1)) if score_match else None
    feedback = feedback_match.group(1).strip() if feedback_match else None

    # Basic validation or fallback if only one part is found?
    # For now, return what's found. If score is missing, it implies failure.
    if score is None and feedback is None:
        logger.warning(f"Could not parse score or feedback from evaluation text: {evaluation_text[:100]}...")
        # Fallback: return the original text as feedback if nothing else is found
        feedback = evaluation_text.strip()

    elif score is None:
         logger.warning(f"Could not parse score from evaluation text: {evaluation_text[:100]}...")
    elif feedback is None:
        logger.warning(f"Could not parse feedback from evaluation text: {evaluation_text[:100]}...")
        # If score is present but feedback isn't, maybe just return the score?
        # Or return the original text as feedback? Let's return original for now.
        feedback = evaluation_text.strip()

    return score, feedback

# Load prompts when module is imported
load_prompts() 