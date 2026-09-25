import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GEMINI_API_KEY environment variable is not set. Please set it in your .env or environment.")

client = genai.Client(api_key=api_key)

print("Modelos disponíveis:")
for modelo in client.models.list():
    print(modelo.name)