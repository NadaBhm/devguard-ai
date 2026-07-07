import os
import json
import asyncio
from typing import AsyncGenerator, Optional, Any
from dataclasses import dataclass
from enum import Enum

import google.generativeai as genai
from google.generativeai.types import GenerationConfig, HarmCategory, HarmBlockThreshold


class GeminiModel(str, Enum):
    """Gemini models."""
    FLASH = "gemini-2.5-flash"          
    PRO = "gemini-2.5-pro"               
    PRO_LATEST = "gemini-2.5-pro"       
    ULTRA = "gemini-2.5-pro"          

@dataclass
class GeminiResponse:
    """Standardized response for Gemini calls."""
    text: str
    raw_response: Any
    tokens_input: int
    tokens_output: int
    model_used: str
    finish_reason: str
    structured_output: Optional[dict] = None

class GeminiClient:
    """Shared Gemini client for DevGuard AI agents."""
    
    DEFAULT_TEMPERATURE = 0.3
    DEFAULT_MAX_TOKENS = 4096
    DEFAULT_TOP_P = 0.95
    DEFAULT_TOP_K = 40
    
    SAFETY_SETTINGS = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
    }
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = GeminiModel.FLASH,
        temperature: float = DEFAULT_TEMPERATURE,
        max_output_tokens: int = DEFAULT_MAX_TOKENS,
        top_p: float = DEFAULT_TOP_P,
        top_k: int = DEFAULT_TOP_K,
    ):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("Gemini API key required.")
        
        genai.configure(api_key=self.api_key)
        self.model_name = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.top_p = top_p
        self.top_k = top_k
        
        self._model = genai.GenerativeModel(
            model_name=self.model_name,
            safety_settings=self.SAFETY_SETTINGS,
        )
    
    def _build_generation_config(self, **overrides) -> GenerationConfig:
        """Build generation config."""
        return GenerationConfig(
            temperature=overrides.get("temperature", self.temperature),
            max_output_tokens=overrides.get("max_output_tokens", self.max_output_tokens),
            top_p=overrides.get("top_p", self.top_p),
            top_k=overrides.get("top_k", self.top_k),
            response_mime_type=overrides.get("response_mime_type", "text/plain"),
            response_schema=overrides.get("response_schema"),
        )
    
    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> GeminiResponse:
        """Generate text from a prompt."""
        config = self._build_generation_config(
            temperature=temperature or self.temperature,
            max_output_tokens=max_tokens or self.max_output_tokens,
        )
        
        if system_instruction:
            content = [
                {"role": "user", "parts": [system_instruction]},
                {"role": "model", "parts": ["Understood."]},
                {"role": "user", "parts": [prompt]},
            ]
            response = await asyncio.to_thread(
                self._model.generate_content,
                content,
                generation_config=config,
            )
        else:
            response = await asyncio.to_thread(
                self._model.generate_content,
                prompt,
                generation_config=config,
            )
        
        return self._parse_response(response)
    
    async def generate_structured(
        self,
        prompt: str,
        schema: dict,
        system_instruction: Optional[str] = None,
    ) -> GeminiResponse:
        """Generate structured JSON output."""
        schema = self._normalize_schema_types(schema)
        
        config = self._build_generation_config(
            response_mime_type="application/json",
            response_schema=schema,
        )
        
        enhanced_prompt = (
            f"{prompt}\n\n"
            "Respond ONLY with valid JSON matching the specified schema. "
            "No markdown, no explanations, no code blocks."
        )
        
        if system_instruction:
            content = [
                {"role": "user", "parts": [system_instruction]},
                {"role": "model", "parts": ["Understood."]},
                {"role": "user", "parts": [enhanced_prompt]},
            ]
            response = await asyncio.to_thread(
                self._model.generate_content,
                content,
                generation_config=config,
            )
        else:
            response = await asyncio.to_thread(
                self._model.generate_content,
                enhanced_prompt,
                generation_config=config,
            )
        
        parsed = self._parse_response(response)
        
        try:
            parsed.structured_output = json.loads(parsed.text)
        except json.JSONDecodeError as e:
            import re
            json_match = re.search(r'```(?:json)?\s*(.*?)```', parsed.text, re.DOTALL)
            if json_match:
                try:
                    parsed.structured_output = json.loads(json_match.group(1).strip())
                except json.JSONDecodeError:
                    raise ValueError(f"Failed to parse structured output: {e}\nRaw: {parsed.text[:500]}")
            else:
                raise ValueError(f"Failed to parse structured output: {e}\nRaw: {parsed.text[:500]}")
        
        return parsed
    
    def _normalize_schema_types(self, schema: dict) -> dict:
        """Normalize schema type strings to uppercase."""
        if not isinstance(schema, dict):
            return schema
        
        normalized = {}
        for key, value in schema.items():
            if key == "type" and isinstance(value, str):
                normalized[key] = value.upper()
            elif key == "properties" and isinstance(value, dict):
                normalized[key] = {
                    k: self._normalize_schema_types(v) if isinstance(v, dict) else v
                    for k, v in value.items()
                }
            elif key == "items" and isinstance(value, dict):
                normalized[key] = self._normalize_schema_types(value)
            elif key == "required" and isinstance(value, list):
                normalized[key] = value
            elif isinstance(value, dict):
                normalized[key] = self._normalize_schema_types(value)
            elif isinstance(value, list):
                normalized[key] = [
                    self._normalize_schema_types(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                normalized[key] = value
        
        return normalized
    
    async def generate_stream(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        """Stream text chunks."""
        config = self._build_generation_config()
        
        if system_instruction:
            content = [
                {"role": "user", "parts": [system_instruction]},
                {"role": "model", "parts": ["Understood."]},
                {"role": "user", "parts": [prompt]},
            ]
            response = await asyncio.to_thread(
                self._model.generate_content,
                content,
                generation_config=config,
                stream=True,
            )
        else:
            response = await asyncio.to_thread(
                self._model.generate_content,
                prompt,
                generation_config=config,
                stream=True,
            )
        
        for chunk in response:
            if chunk.text:
                yield chunk.text
    
    def start_chat(self, system_instruction: Optional[str] = None) -> genai.ChatSession:
        """Start a new chat session."""
        model = self._model
        if system_instruction:
            chat = model.start_chat(history=[])
            chat.send_message(f"[SYSTEM INSTRUCTION: {system_instruction}]")
            return chat
        return model.start_chat(history=[])
    
    async def chat_message(
        self,
        chat_session: genai.ChatSession,
        message: str,
    ) -> GeminiResponse:
        """Send a message in an existing chat session."""
        response = await asyncio.to_thread(
            chat_session.send_message,
            message,
            generation_config=self._build_generation_config(),
        )
        return self._parse_response(response)
    
    def _parse_response(self, raw_response: Any) -> GeminiResponse:
        """Parse raw Gemini response."""
        usage = getattr(raw_response, "usage_metadata", None)
        tokens_input = getattr(usage, "prompt_token_count", 0) if usage else 0
        tokens_output = getattr(usage, "candidates_token_count", 0) if usage else 0
        
        finish_reason = "STOP"
        if raw_response.candidates:
            finish_reason = str(raw_response.candidates[0].finish_reason)
        
        text = ""
        try:
            text = raw_response.text
        except AttributeError:
            if raw_response.candidates and raw_response.candidates[0].content:
                text = str(raw_response.candidates[0].content.parts)
            else:
                text = "[Response blocked or empty]"
        
        return GeminiResponse(
            text=text,
            raw_response=raw_response,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            model_used=self.model_name,
            finish_reason=finish_reason,
        )
    
    async def embed(self, text: str) -> list[float]:
        """Generate embeddings."""
        embedding_model = "models/embedding-001"
        result = await asyncio.to_thread(
            genai.embed_content,
            model=embedding_model,
            content=text,
            task_type="retrieval_document",
        )
        return result["embedding"]
    
    # --- Agent convenience methods ---
    
    async def analyze_code(self, code: str, language: str) -> GeminiResponse:
        """CodeSec agent: Analyze code for vulnerabilities."""
        system = (
            "You are a security expert. Analyze code for vulnerabilities. "
            "Identify: CWE category, severity, affected lines, and remediation."
        )
        prompt = f"Language: {language}\n\nCode:\n```\n{code}\n```\n\nProvide findings in structured format."
        return await self.generate(prompt, system_instruction=system)
    
    async def generate_terraform(
        self,
        stack_info: dict,
        architecture: str,
    ) -> GeminiResponse:
        """InfraCost agent: Generate Terraform."""
        schema = {
            "type": "OBJECT",
            "properties": {
                "main_tf": {"type": "STRING", "description": "Main Terraform configuration"},
                "variables_tf": {"type": "STRING"},
                "outputs_tf": {"type": "STRING"},
                "architecture_notes": {"type": "STRING"},
            },
            "required": ["main_tf", "variables_tf"],
        }
        system = "You are an AWS infrastructure expert. Generate production-ready Terraform HCL."
        prompt = (
            f"Generate Terraform for a {stack_info.get('language')} application "
            f"using {stack_info.get('framework')} on AWS {architecture}.\n"
            f"Stack details: {json.dumps(stack_info, indent=2)}"
        )
        return await self.generate_structured(prompt, schema, system_instruction=system)
    
    async def estimate_cost(
        self,
        terraform_code: str,
        region: str = "us-east-1",
    ) -> GeminiResponse:
        """InfraCost agent: Estimate AWS costs from Terraform."""
        schema = {
            "type": "OBJECT",
            "properties": {
                "monthly_cost_usd": {"type": "NUMBER"},
                "annual_cost_usd": {"type": "NUMBER"},
                "breakdown": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "service": {"type": "STRING"},
                            "cost_usd": {"type": "NUMBER"},
                        }
                    }
                },
                "optimization_suggestions": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
            "required": ["monthly_cost_usd", "breakdown"],
        }
        prompt = (
            f"Estimate monthly AWS costs for this Terraform configuration in {region}.\n"
            f"Use AWS pricing knowledge (not actual API call).\n\n```hcl\n{terraform_code}\n```"
        )
        return await self.generate_structured(prompt, schema)
    
    async def orchestrate_chat(
        self,
        user_message: str,
        job_context: dict,
        chat_history: list[dict],
    ) -> GeminiResponse:
        """Orchestrator agent: Conversational chat with context."""
        system = (
            "You are DevGuard AI's orchestrator. Help users understand "
            "repository analysis, security findings, infrastructure choices, and costs. "
            "Be concise, technical but accessible. Use job context."
        )
        
        context_str = json.dumps(job_context, indent=2)
        history_str = "\n".join([
            f"{'User' if msg['role'] == 'user' else 'Assistant'}: {msg['content']}"
            for msg in chat_history[-5:]
        ])
        
        prompt = (
            f"Current job context:\n{context_str}\n\n"
            f"Chat history:\n{history_str}\n\n"
            f"User: {user_message}\n\n"
            f"Respond helpfully using the job context."
        )
        
        return await self.generate(prompt, system_instruction=system)

_gemini_client: Optional[GeminiClient] = None

def get_gemini_client() -> GeminiClient:
    """Get or create singleton Gemini client instance."""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = GeminiClient()
    return _gemini_client

async def gemini_dependency() -> GeminiClient:
    """FastAPI dependency for injecting Gemini client."""
    return get_gemini_client()