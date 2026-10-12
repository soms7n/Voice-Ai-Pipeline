"""Pipecat pipeline served behind Exotel's bidirectional AgentStream WebSocket."""

from __future__ import annotations

import json
import os

from fastapi import WebSocket

from .services import DomainError, bind_media_stream, verify_media_token
from .config import get_settings


SYSTEM_INSTRUCTION = """You are Airtel's device-recovery assistant.
State that you are an AI assistant calling about a device return. Be concise,
respect a refusal immediately, and do not claim that a pickup is booked. If the
customer asks not to be contacted, acknowledge the opt-out. Do not collect
unnecessary personal data. End the call politely once the purpose is complete."""


def _require_voice_credentials() -> None:
    missing = [
        name
        for name in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "CARTESIA_VOICE_ID")
        if not os.getenv(name)
    ]
    if missing:
        raise DomainError(f"voice agent is not configured: {', '.join(missing)}")


async def serve_exotel_media(websocket: WebSocket, token: str) -> None:
    """Receive Exotel's `start` event, then hand the session to Pipecat.

    Pipecat's Exotel serializer owns the subsequent media, DTMF, interruption,
    and outbound-audio framing. Outcome extraction intentionally remains an
    explicit callback to `/internal/pipecat/outcomes`, rather than letting an LLM
    mark a booking as successful.
    """

    _require_voice_credentials()
    attempt_id = verify_media_token(token, get_settings())
    await websocket.accept()

    start_event: dict[str, object] | None = None
    while start_event is None:
        raw = await websocket.receive_text()
        message = json.loads(raw)
        if message.get("event") == "start":
            start_event = message

    start = start_event.get("start", {})
    if not isinstance(start, dict):
        raise DomainError("invalid Exotel start event")
    call_sid = str(start.get("call_sid", ""))
    stream_sid = str(start.get("stream_sid", start_event.get("stream_sid", "")))
    if not call_sid or not stream_sid:
        raise DomainError("Exotel start event is missing a call or stream SID")
    bind_media_stream(attempt_id, call_sid, stream_sid)

    # Imported lazily so campaign management can start without loading voice
    # SDKs until Exotel actually opens an authenticated media connection.
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import LLMRunFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
    from pipecat.serializers.exotel import ExotelFrameSerializer
    from pipecat.services.cartesia.tts import CartesiaTTSService
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.services.openai.llm import OpenAILLMService
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
    from pipecat.workers.runner import WorkerRunner

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_analyzer=SileroVADAnalyzer(),
            serializer=ExotelFrameSerializer(stream_sid=stream_sid, call_sid=call_sid),
        ),
    )
    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )
    pipeline = Pipeline(
        [
            transport.input(),
            DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"]),
            user_aggregator,
            OpenAILLMService(
                api_key=os.environ["OPENAI_API_KEY"],
                settings=OpenAILLMService.Settings(system_instruction=SYSTEM_INSTRUCTION),
            ),
            CartesiaTTSService(
                api_key=os.environ["CARTESIA_API_KEY"],
                settings=CartesiaTTSService.Settings(voice=os.environ["CARTESIA_VOICE_ID"]),
            ),
            transport.output(),
            assistant_aggregator,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000, enable_metrics=True),
    )
    context.add_message({"role": "developer", "content": "Start by concisely introducing yourself."})
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await worker.queue_frames([LLMRunFrame()])
    await runner.run()
