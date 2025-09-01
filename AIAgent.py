
import openai
import json

class AIAgent:
    def __init__(self, model):
        self.model = model
        self.SYSTEM_PROMPT = f"""You are an expert at research about drugs for conditions. 
                Your task is to provide a summary of the relevant clinical trial data for a specific condition.
                You will be provided with patient data and a condition.
                Use the patient data to tailor your response to the specific patient.
                
                CITATION POLICY (MANDATORY)

• After every factual sentence, bullet, or table cell, append one or more Markdown links immediately after the claim, separated by an em dash (—) or placed in parentheses.
• Use Markdown link syntax ONLY (no HTML): [Short label](https://example.com)
• Prefer primary, published sources (FDA labels, guidelines, peer-reviewed journals, official docs). No placeholders or made-up URLs.
• Keep link text concise (e.g., “FDA label”, “AAD guideline”, “NEJM 2023”). Avoid showing raw URLs unless no clear label exists.
• If a reputable source cannot be found, either remove the claim or add “(no public source)”.
• Output must be valid Markdown and MUST NOT use HTML anchor tags.

ACCEPTANCE CHECK
• Every non-trivial claim ends with ≥1 Markdown link.
• No <a …> HTML anchors appear anywhere in the output.

If there are no drugs available for this condition, please state that clearly.
"""
    
    def drug_agent(self, prompt, patient):
        # Combine prompt and data for context
        context = f"Data: {json.dumps(patient)}\nInstruction: {prompt}"
        response = openai.chat.completions.create(
            model=self.model,
            messages=[
               {"role": "system", "content": self.SYSTEM_PROMPT},
               {"role": "user", "content": context}
            ]
        )
        return response.choices[0].message.content
