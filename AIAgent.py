
import openai
import json

class AIAgent:
    def __init__(self, model="gpt-4.1"):
        self.model = model

    def read_json_data(self, json_data):
        return json.loads(json_data)

    def query_agent(self, prompt, data, condition):
        # Combine prompt and data for context
        context = f"Data: {json.dumps(data)}\nInstruction: {prompt}"
        response = openai.chat.completions.create(
            model=self.model,
            messages=[
               {"role": "system", "content": f"You read clinical trial study results written in JSON data. Write a concise summary for a patient to understand the results of a study based on the condition {condition}."},
               {"role": "user", "content": context}
            ],
            temperature=0.0
        )
        return response.choices[0].message.content
    
