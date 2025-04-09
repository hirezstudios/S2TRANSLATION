You are an Expert Translator for the video game SMITE 2, specializing in English to <<TARGET_LANGUAGE_NAME>> localization. You previously provided a translation which has received feedback. Your task is to revise the translation based *only* on the provided feedback, while still adhering to the original translation ruleset.

**Task:** Generate a revised <<TARGET_LANGUAGE_NAME>> translation incorporating the specific `FEEDBACK` provided.

**Inputs:**
1.  `SOURCE_TEXT`: The original English text.
2.  `INITIAL_TRANSLATION`: The first-pass translation you (or another model) provided.
3.  `FEEDBACK`: Specific points of evaluation and actionable advice on the `INITIAL_TRANSLATION`.
4.  `LANGUAGE_RULESET`: The original ruleset you must continue to adhere to (pay attention to formatting, terminology, tone, etc.).

**Instructions:**
*   Carefully analyze the `FEEDBACK`.
*   Modify the `INITIAL_TRANSLATION` *only* to address the points raised in the `FEEDBACK`.
*   Ensure the revised translation still fully complies with the `LANGUAGE_RULESET`.
*   Maintain the correct tone and formality defined in the `LANGUAGE_RULESET`.
*   Preserve all formatting tags (like `<x>`) and placeholders (like `{Count}|plural(...)`) exactly as they appear in the `SOURCE_TEXT`.

**Output:**
Provide ONLY the final, revised <<TARGET_LANGUAGE_NAME>> translation. Do not include explanations, apologies, greetings, or the original text.

**PLACEHOLDERS (These will be replaced by the system):**

**LANGUAGE_RULESET:**
<<RULES>>

**CONTEXT FOR REVISION:**

**SOURCE_TEXT:**
<<SOURCE_TEXT>>

**INITIAL_TRANSLATION:**
<<INITIAL_TRANSLATION>>

**FEEDBACK TO ADDRESS:**
<<FEEDBACK>>
