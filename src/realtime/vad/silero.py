"""Silero VAD model wrapper aligned with realtime-ai semantics."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

STATE_SHAPE = (2, 1, 128)
CONTEXT_LEN = 64


def _resolve_model_path(model_path: str) -> Path:
    path = Path(model_path)
    if path.is_absolute():
        return path
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / path


class SileroVADModel:
    """Shared ONNX session for Silero VAD inference."""

    def __init__(self, model_path: str) -> None:
        try:
            import onnxruntime as ort
        except Exception as exc:  # pragma: no cover - import failure is environment-specific
            raise RuntimeError(
                "onnxruntime is required for realtime Silero VAD"
            ) from exc

        resolved_path = _resolve_model_path(model_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f"Silero VAD model not found: {resolved_path}")

        self._ort = ort
        self.model_path = resolved_path
        self._lock = threading.Lock()
        self._session = ort.InferenceSession(
            str(resolved_path),
            providers=["CPUExecutionProvider"],
        )

        inputs = {item.name: item for item in self._session.get_inputs()}
        outputs = self._session.get_outputs()
        self._input_name = "input" if "input" in inputs else self._session.get_inputs()[0].name
        self._state_name = "state" if "state" in inputs else self._session.get_inputs()[1].name
        self._sr_name = "sr" if "sr" in inputs else self._session.get_inputs()[2].name
        self._output_name = outputs[0].name
        self._next_state_name = outputs[1].name

    def warmup(self, sample_rate: int = 16000) -> None:
        stream = self.create_stream(sample_rate=sample_rate)
        stream.infer(np.zeros(512, dtype=np.float32))
        logger.info("Silero VAD model warmed up: model_path=%s sample_rate=%s", self.model_path, sample_rate)

    def create_stream(self, *, sample_rate: int = 16000) -> SileroVADStream:
        return SileroVADStream(model=self, sample_rate=sample_rate)

    def run(
        self,
        *,
        pcm: np.ndarray,
        state: np.ndarray,
        sample_rate: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            output, next_state = self._session.run(
                [self._output_name, self._next_state_name],
                {
                    self._input_name: pcm.reshape(1, -1).astype(np.float32, copy=False),
                    self._state_name: state.astype(np.float32, copy=False),
                    self._sr_name: np.asarray([sample_rate], dtype=np.int64),
                },
            )
        return np.asarray(output, dtype=np.float32), np.asarray(next_state, dtype=np.float32)


class SileroVADStream:
    """Per-stream detector state layered on top of a shared model."""

    def __init__(self, *, model: SileroVADModel, sample_rate: int) -> None:
        if sample_rate not in {8000, 16000}:
            raise ValueError("Silero VAD only supports sample_rate of 8000 or 16000")
        self._model = model
        self.sample_rate = sample_rate
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(CONTEXT_LEN, dtype=np.float32)
        self.curr_sample = 0

    def reset(self) -> None:
        self._state.fill(0)
        self._context.fill(0)
        self.curr_sample = 0

    def infer(self, samples: np.ndarray) -> float:
        pcm = samples.astype(np.float32, copy=False)
        if pcm.ndim != 1:
            raise ValueError("Silero VAD expects a 1-D float32 PCM window")

        if self.curr_sample > 0:
            pcm = np.concatenate((self._context, pcm))

        if samples.size >= CONTEXT_LEN:
            self._context = samples[-CONTEXT_LEN:].astype(np.float32, copy=True)
        else:
            next_context = np.zeros(CONTEXT_LEN, dtype=np.float32)
            next_context[-samples.size :] = samples.astype(np.float32, copy=False)
            self._context = next_context

        output, next_state = self._model.run(
            pcm=pcm,
            state=self._state,
            sample_rate=self.sample_rate,
        )
        self._state = next_state
        self.curr_sample += samples.size
        return float(output.reshape(-1)[0])
