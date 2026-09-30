from langchain_ollama import ChatOllama
from dotenv import load_dotenv
import os

load_dotenv()
ollama_bearer_token = os.getenv("OLLAMA_API_KEY", "")

LLM_GPT = ChatOllama(
    model="gpt-oss:120b:cloud",
    base_url="https://api.ollama.com",
    client_kwargs={
        "headers": {
            "Authorization": "Bearer " + ollama_bearer_token
        }
    }, 
)