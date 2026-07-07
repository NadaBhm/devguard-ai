from dotenv import load_dotenv
load_dotenv() 
import asyncio
from shared.llm.gemini import GeminiClient

async def test():
    client = GeminiClient()  # Reads GEMINI_API_KEY from env
    
    # Simple generation
    r = await client.generate("What is the best AWS service for a Python FastAPI app?")
    print(f"Response: {r.text[:200]}...")
    print(f"Tokens: {r.tokens_input} in / {r.tokens_output} out")
    
    # Structured output
    schema = {
        "type": "object",
        "properties": {
            "service": {"type": "string"},
            "reason": {"type": "string"},
            "monthly_cost_usd": {"type": "number"},
        },
        "required": ["service", "reason"],
    }
    r = await client.generate_structured(
        "Recommend an AWS service for a Python FastAPI app with 1000 users",
        schema,
    )
    print(f"Structured: {r.structured_output}")

if __name__ == "__main__":
    asyncio.run(test())