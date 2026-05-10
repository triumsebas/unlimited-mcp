import os
import sys
from dotenv import load_dotenv

load_dotenv()

provider = sys.argv[1] if len(sys.argv) > 1 else "groq"

from smolagents import CodeAgent, LiteLLMModel

if provider == "groq":
    model = LiteLLMModel(
        model_id=f"groq/{os.environ['GROQ_MODEL']}",
        api_key=os.environ["GROQ_API_KEY"],
    )
elif provider == "gemini":
    model = LiteLLMModel(
        model_id="gemini/gemini-2.5-flash",
        api_key=os.environ["GEMINI_API_KEY"],
    )
elif provider == "github":
    model = LiteLLMModel(
        model_id="openai/DeepSeek-V3-0324",
        api_key=os.environ["GITHUB_TOKEN"],
        api_base=os.environ["GITHUB_API_BASE"],
    )
elif provider == "ollama":
    model = LiteLLMModel(
        model_id=f"ollama_chat/{os.environ['OLLAMA_MODEL']}",
        api_base=os.environ.get("OLLAMA_API_BASE", "http://localhost:11434"),
    )
elif provider == "opencode":
    model = LiteLLMModel(
        model_id=f"openai/{os.environ['OPENCODE_MODEL']}",
        api_key=os.environ["OPENCODE_API_KEY"],
        api_base=os.environ["OPENCODE_API_BASE"],
    )

agent = CodeAgent(model=model, tools=[])
result = agent.run("Calcula los números primos hasta 50 y devuelve la lista")
print("\nResultado:", result)
