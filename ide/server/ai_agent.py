"""
DCL AI Agent — Claude API interface for natural language → DCL code generation.
"""
import os
import logging
from typing import Optional

logger = logging.getLogger('dcl-ide.ai')

# DCL System Prompt for AI
DCL_SYSTEM_PROMPT = """You are a DCL (Deterministic Control Language) programming assistant for industrial control.

DCL Syntax Reference:
- SENSOR name FROM source [SCALE k b] [RANGE lo hi]
- FILTER name FROM signal LOWPASS a=alpha
- PID name FROM signal SP=setpoint KP=kp KI=ki KD=kd LIMIT lo hi
- TIMER name PT=time_ms [mode=TON|TOF|TP]
- COUNTER name CU=signal PV=preset [R=reset]
- LATCH name S1=set R=reset
- ALARM name FROM signal > threshold
- LOGIC name = signal1 AND|OR|NOT signal2
- OUTPUT name TO target FROM signal
- RATE name FROM signal
- DEADBAND name FROM signal width
- SCALE name FROM signal RANGE lo hi

Rules:
1. Each line defines one function block
2. Signal names are lowercase, FB keywords are UPPERCASE
3. FROM connects inputs, TO connects outputs
4. Parameters use key=value syntax
5. Comments start with # or //
6. The execution engine runs at 100μs cycle with deterministic timing

Generate ONLY valid DCL code. No explanations unless asked. Use comments sparingly."""

class AIAgent:
    def __init__(self):
        self.api_key = os.environ.get('ANTHROPIC_API_KEY', '')
        self.model = os.environ.get('DCL_AI_MODEL', 'claude-sonnet-4-20250514')
        self.conversation_history = []
        self._client = None

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        """Lazy init Anthropic client."""
        if self._client is None and self.api_key:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                logger.warning("anthropic package not installed, AI features disabled")
                return None
        return self._client

    def set_api_key(self, key: str):
        self.api_key = key
        self._client = None  # Reset client

    async def chat(self, user_message: str, context: str = '') -> dict:
        """Send a message and get AI response.

        Args:
            user_message: The user's natural language request
            context: Current DCL source code (for context-aware responses)

        Returns:
            dict with 'response' (str) and 'success' (bool) keys
        """
        if not self.is_available:
            return {
                'success': False,
                'response': 'AI not configured. Set ANTHROPIC_API_KEY environment variable.',
            }

        client = self._get_client()
        if not client:
            return {
                'success': False,
                'response': 'AI client not available. Install anthropic package: pip install anthropic',
            }

        # Build messages
        system_prompt = DCL_SYSTEM_PROMPT
        if context:
            system_prompt += f"\n\nCurrent DCL program:\n```\n{context}\n```"

        self.conversation_history.append({'role': 'user', 'content': user_message})

        # Keep last 10 messages for context window
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=system_prompt,
                    messages=self.conversation_history,
                )
            )

            assistant_msg = response.content[0].text
            self.conversation_history.append({'role': 'assistant', 'content': assistant_msg})

            return {
                'success': True,
                'response': assistant_msg,
            }

        except Exception as e:
            logger.error("AI API error: %s", e)
            return {
                'success': False,
                'response': f'AI error: {str(e)}',
            }

    def clear_history(self):
        self.conversation_history = []
