"""Classes for voice assistant pipelines."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable
from dataclasses import asdict, dataclass, field
import logging
from typing import Any

from homeassistant.backports.enum import StrEnum
from homeassistant.components import conversation, media_source, stt, tts
from homeassistant.components.tts.media_source import (
    generate_media_source_id as tts_generate_media_source_id,
)
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.util.dt import utcnow

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@callback
def async_get_pipeline(
    hass: HomeAssistant, pipeline_id: str | None = None, language: str | None = None
) -> Pipeline | None:
    """Get a pipeline by id or create one for a language."""
    if pipeline_id is not None:
        return hass.data[DOMAIN].get(pipeline_id)

    # Construct a pipeline for the required/configured language
    language = language or hass.config.language
    return Pipeline(
        name=language,
        language=language,
        stt_engine=None,  # first engine
        conversation_engine=None,  # first agent
        tts_engine=None,  # first engine
    )


class PipelineError(Exception):
    """Base class for pipeline errors."""

    def __init__(self, code: str, message: str) -> None:
        """Set error message."""
        self.code = code
        self.message = message

        super().__init__(f"Pipeline error code={code}, message={message}")


class SpeechToTextError(PipelineError):
    """Error in speech to text portion of pipeline."""


class IntentRecognitionError(PipelineError):
    """Error in intent recognition portion of pipeline."""


class TextToSpeechError(PipelineError):
    """Error in text to speech portion of pipeline."""


class PipelineEventType(StrEnum):
    """Event types emitted during a pipeline run."""

    RUN_START = "run-start"
    RUN_END = "run-end"
    STT_START = "stt-start"
    STT_END = "stt-end"
    INTENT_START = "intent-start"
    INTENT_END = "intent-end"
    TTS_START = "tts-start"
    TTS_END = "tts-end"
    ERROR = "error"


@dataclass
class PipelineEvent:
    """Events emitted during a pipeline run."""

    type: PipelineEventType
    data: dict[str, Any] | None = None
    timestamp: str = field(default_factory=lambda: utcnow().isoformat())

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the event."""
        return {
            "type": self.type,
            "timestamp": self.timestamp,
            "data": self.data or {},
        }


@dataclass
class Pipeline:
    """A voice assistant pipeline."""

    name: str
    language: str | None
    stt_engine: str | None
    conversation_engine: str | None
    tts_engine: str | None


class PipelineStage(StrEnum):
    """Stages of a pipeline."""

    STT = "stt"
    INTENT = "intent"
    TTS = "tts"


PIPELINE_STAGE_ORDER = [
    PipelineStage.STT,
    PipelineStage.INTENT,
    PipelineStage.TTS,
]


class PipelineRunValidationError(Exception):
    """Error when a pipeline run is not valid."""


class InvalidPipelineStagesError(PipelineRunValidationError):
    """Error when given an invalid combination of start/end stages."""

    def __init__(
        self,
        start_stage: PipelineStage,
        end_stage: PipelineStage,
    ) -> None:
        """Set error message."""
        super().__init__(
            f"Invalid stage combination: start={start_stage}, end={end_stage}"
        )


@dataclass
class PipelineRun:
    """Running context for a pipeline."""

    hass: HomeAssistant
    context: Context
    pipeline: Pipeline
    start_stage: PipelineStage
    end_stage: PipelineStage
    event_callback: Callable[[PipelineEvent], None]
    language: str = None  # type: ignore[assignment]
    runner_data: Any | None = None
    stt_provider: stt.Provider | None = None
    intent_agent: str | None = None
    tts_engine: str | None = None

    def __post_init__(self):
        """Set language for pipeline."""
        self.language = self.pipeline.language or self.hass.config.language

        # stt -> intent -> tts
        if PIPELINE_STAGE_ORDER.index(self.end_stage) < PIPELINE_STAGE_ORDER.index(
            self.start_stage
        ):
            raise InvalidPipelineStagesError(self.start_stage, self.end_stage)

    def start(self):
        """Emit run start event."""
        data = {
            "pipeline": self.pipeline.name,
            "language": self.language,
        }
        if self.runner_data is not None:
            data["runner_data"] = self.runner_data

        self.event_callback(PipelineEvent(PipelineEventType.RUN_START, data))

    def end(self):
        """Emit run end event."""
        self.event_callback(
            PipelineEvent(
                PipelineEventType.RUN_END,
            )
        )

    async def prepare_speech_to_text(self, metadata: stt.SpeechMetadata) -> None:
        """Prepare speech to text."""
        stt_provider = stt.async_get_provider(self.hass, self.pipeline.stt_engine)

        if stt_provider is None:
            engine = self.pipeline.stt_engine or "default"
            raise SpeechToTextError(
                code="stt-provider-missing",
                message=f"No speech to text provider for: {engine}",
            )

        if not stt_provider.check_metadata(metadata):
            raise SpeechToTextError(
                code="stt-provider-unsupported-metadata",
                message=(
                    f"Provider {stt_provider.name} does not support input speech "
                    "to text metadata"
                ),
            )

        self.stt_provider = stt_provider

    async def speech_to_text(
        self,
        metadata: stt.SpeechMetadata,
        stream: AsyncIterable[bytes],
    ) -> str:
        """Run speech to text portion of pipeline. Returns the spoken text."""
        if self.stt_provider is None:
            raise RuntimeError("Speech to text was not prepared")

        engine = self.stt_provider.name

        self.event_callback(
            PipelineEvent(
                PipelineEventType.STT_START,
                {
                    "engine": engine,
                    "metadata": asdict(metadata),
                },
            )
        )

        try:
            # Transcribe audio stream
            result = await self.stt_provider.async_process_audio_stream(
                metadata, stream
            )
        except Exception as src_error:
            _LOGGER.exception("Unexpected error during speech to text")
            raise SpeechToTextError(
                code="stt-stream-failed",
                message="Unexpected error during speech to text",
            ) from src_error

        _LOGGER.debug("speech-to-text result %s", result)

        if result.result != stt.SpeechResultState.SUCCESS:
            raise SpeechToTextError(
                code="stt-stream-failed",
                message="Speech to text failed",
            )

        if not result.text:
            raise SpeechToTextError(
                code="stt-no-text-recognized", message="No text recognized"
            )

        self.event_callback(
            PipelineEvent(
                PipelineEventType.STT_END,
                {
                    "stt_output": {
                        "text": result.text,
                    }
                },
            )
        )

        return result.text

    async def prepare_recognize_intent(self) -> None:
        """Prepare recognizing an intent."""
        agent_info = conversation.async_get_agent_info(
            self.hass, self.pipeline.conversation_engine
        )

        if agent_info is None:
            engine = self.pipeline.conversation_engine or "default"
            raise IntentRecognitionError(
                code="intent-not-supported",
                message=f"Intent recognition engine {engine} is not found",
            )

        self.intent_agent = agent_info["id"]

    async def recognize_intent(
        self, intent_input: str, conversation_id: str | None
    ) -> str:
        """Run intent recognition portion of pipeline. Returns text to speak."""
        if self.intent_agent is None:
            raise RuntimeError("Recognize intent was not prepared")

        self.event_callback(
            PipelineEvent(
                PipelineEventType.INTENT_START,
                {
                    "engine": self.intent_agent,
                    "intent_input": intent_input,
                },
            )
        )

        try:
            conversation_result = await conversation.async_converse(
                hass=self.hass,
                text=intent_input,
                conversation_id=conversation_id,
                context=self.context,
                language=self.language,
                agent_id=self.intent_agent,
            )
        except Exception as src_error:
            _LOGGER.exception("Unexpected error during intent recognition")
            raise IntentRecognitionError(
                code="intent-failed",
                message="Unexpected error during intent recognition",
            ) from src_error

        _LOGGER.debug("conversation result %s", conversation_result)

        self.event_callback(
            PipelineEvent(
                PipelineEventType.INTENT_END,
                {"intent_output": conversation_result.as_dict()},
            )
        )

        speech = conversation_result.response.speech.get("plain", {}).get("speech", "")

        return speech

    async def prepare_text_to_speech(self) -> None:
        """Prepare text to speech."""
        engine = tts.async_resolve_engine(self.hass, self.pipeline.tts_engine)

        if engine is None:
            engine = self.pipeline.tts_engine or "default"
            raise TextToSpeechError(
                code="tts-not-supported",
                message=f"Text to speech engine '{engine}' not found",
            )

        if not await tts.async_support_options(self.hass, engine, self.language):
            raise TextToSpeechError(
                code="tts-not-supported",
                message=(
                    f"Text to speech engine {engine} "
                    f"does not support language {self.language}"
                ),
            )

        self.tts_engine = engine

    async def text_to_speech(self, tts_input: str) -> str:
        """Run text to speech portion of pipeline. Returns URL of TTS audio."""
        if self.tts_engine is None:
            raise RuntimeError("Text to speech was not prepared")

        self.event_callback(
            PipelineEvent(
                PipelineEventType.TTS_START,
                {
                    "engine": self.tts_engine,
                    "tts_input": tts_input,
                },
            )
        )

        try:
            # Synthesize audio and get URL
            tts_media = await media_source.async_resolve_media(
                self.hass,
                tts_generate_media_source_id(
                    self.hass,
                    tts_input,
                    engine=self.tts_engine,
                    language=self.language,
                ),
            )
        except Exception as src_error:
            _LOGGER.exception("Unexpected error during text to speech")
            raise TextToSpeechError(
                code="tts-failed",
                message="Unexpected error during text to speech",
            ) from src_error

        _LOGGER.debug("TTS result %s", tts_media)

        self.event_callback(
            PipelineEvent(
                PipelineEventType.TTS_END,
                {"tts_output": asdict(tts_media)},
            )
        )

        return tts_media.url


@dataclass
class PipelineInput:
    """Input to a pipeline run."""

    run: PipelineRun

    stt_metadata: stt.SpeechMetadata | None = None
    """Metadata of stt input audio. Required when start_stage = stt."""

    stt_stream: AsyncIterable[bytes] | None = None
    """Input audio for stt. Required when start_stage = stt."""

    intent_input: str | None = None
    """Input for conversation agent. Required when start_stage = intent."""

    tts_input: str | None = None
    """Input for text to speech. Required when start_stage = tts."""

    conversation_id: str | None = None

    async def execute(self):
        """Run pipeline."""
        self.run.start()
        current_stage = self.run.start_stage

        try:
            # Speech to text
            intent_input = self.intent_input
            if current_stage == PipelineStage.STT:
                assert self.stt_metadata is not None
                assert self.stt_stream is not None
                intent_input = await self.run.speech_to_text(
                    self.stt_metadata,
                    self.stt_stream,
                )
                current_stage = PipelineStage.INTENT

            if self.run.end_stage != PipelineStage.STT:
                tts_input = self.tts_input

                if current_stage == PipelineStage.INTENT:
                    assert intent_input is not None
                    tts_input = await self.run.recognize_intent(
                        intent_input, self.conversation_id
                    )
                    current_stage = PipelineStage.TTS

                if self.run.end_stage != PipelineStage.INTENT:
                    if current_stage == PipelineStage.TTS:
                        assert tts_input is not None
                        await self.run.text_to_speech(tts_input)

        except PipelineError as err:
            self.run.event_callback(
                PipelineEvent(
                    PipelineEventType.ERROR,
                    {"code": err.code, "message": err.message},
                )
            )
            return

        self.run.end()

    async def validate(self):
        """Validate pipeline input against start stage."""
        if self.run.start_stage == PipelineStage.STT:
            if self.stt_metadata is None:
                raise PipelineRunValidationError(
                    "stt_metadata is required for speech to text"
                )

            if self.stt_stream is None:
                raise PipelineRunValidationError(
                    "stt_stream is required for speech to text"
                )
        elif self.run.start_stage == PipelineStage.INTENT:
            if self.intent_input is None:
                raise PipelineRunValidationError(
                    "intent_input is required for intent recognition"
                )
        elif self.run.start_stage == PipelineStage.TTS:
            if self.tts_input is None:
                raise PipelineRunValidationError(
                    "tts_input is required for text to speech"
                )

        start_stage_index = PIPELINE_STAGE_ORDER.index(self.run.start_stage)

        prepare_tasks = []

        if start_stage_index <= PIPELINE_STAGE_ORDER.index(PipelineStage.STT):
            prepare_tasks.append(self.run.prepare_speech_to_text(self.stt_metadata))

        if start_stage_index <= PIPELINE_STAGE_ORDER.index(PipelineStage.INTENT):
            prepare_tasks.append(self.run.prepare_recognize_intent())

        if start_stage_index <= PIPELINE_STAGE_ORDER.index(PipelineStage.TTS):
            prepare_tasks.append(self.run.prepare_text_to_speech())

        if prepare_tasks:
            await asyncio.gather(*prepare_tasks)
