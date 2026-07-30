'自动战斗声音提示识别。'
from __future__ import annotations

import threading
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    import soundcard as sc
except ImportError:  
    sc = None


RED_CUE_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "assets" / "combat" / "red_cue.wav"
)


@dataclass(frozen=True)
class AudioCueState:
    ready: bool
    matched: bool
    score: float
    rms: float


def _read_mono_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as source:
        if source.getsampwidth() != 2:
            raise ValueError("声音模板必须为16位PCM")
        sample_rate = source.getframerate()
        channels = source.getnchannels()
        samples = np.frombuffer(source.readframes(source.getnframes()), "<i2")
    samples = samples.reshape(-1, channels).mean(axis=1)
    return sample_rate, (samples.astype(np.float32) / 32768.0)


def _time_frequency_feature(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    frame_size = max(256, round(sample_rate * 0.020))
    hop_size = max(128, round(sample_rate * 0.010))
    if samples.size < frame_size:
        return np.empty((0, 32), dtype=np.float32)
    starts = np.arange(0, samples.size - frame_size + 1, hop_size)
    frames = np.stack(
        [samples[start:start + frame_size] for start in starts],
        axis=0,
    )
    frames *= np.hanning(frame_size)
    spectrum = np.log1p(
        np.abs(np.fft.rfft(frames, axis=1)) ** 2 * 1000.0
    )
    frequencies = np.fft.rfftfreq(frame_size, 1.0 / sample_rate)
    spectrum = spectrum[:, (frequencies >= 180) & (frequencies <= 8000)]
    pooled = np.stack(
        [part.mean(axis=1) for part in np.array_split(spectrum, 32, axis=1)],
        axis=1,
    )
    pooled -= pooled.mean(axis=0, keepdims=True)
    pooled /= pooled.std() + 1e-6
    return pooled.astype(np.float32)


class RedCueAudioDetector:
    '采集默认扬声器回放，只在视觉红光窗口内比对声音模板。'

    SAMPLE_RATE = 48000
    TEMPLATE_SECONDS = 1.25
    BUFFER_SECONDS = 1.70
    BLOCK_FRAMES = 960
    MIN_RMS = 0.008
    MATCH_THRESHOLD = 0.72

    def __init__(
        self,
        template_path: Path = RED_CUE_TEMPLATE,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.log = log or (lambda _message: None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        self._match_lock = threading.Lock()
        self._match_request = threading.Event()
        self._match_thread: threading.Thread | None = None
        self._latest_match = AudioCueState(False, False, 0.0, 0.0)
        self._match_sequence = 0
        self._buffer = np.zeros(
            round(self.SAMPLE_RATE * self.BUFFER_SECONDS),
            dtype=np.float32,
        )
        self._write_index = 0
        self._sample_count = 0
        sample_rate, template = _read_mono_wav(template_path)
        if sample_rate != self.SAMPLE_RATE:
            raise ValueError(
                f"声音模板采样率应为{self.SAMPLE_RATE}，实际为{sample_rate}"
            )
        self._template = template[:round(sample_rate * self.TEMPLATE_SECONDS)]
        self._template_feature = _time_frequency_feature(
            self._template,
            self.SAMPLE_RATE,
        )

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if sc is None:
            self.log("缺少声音采集组件，声音与红光共同判定不可用")
            return False
        try:
            speaker = sc.default_speaker()
            microphone = sc.get_microphone(
                id=str(speaker.id),
                include_loopback=True,
            )
        except Exception as exc:
            self.log(f"无法打开系统回放声音，声音与红光共同判定不可用：{exc}")
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(microphone,),
            name="combat-audio",
            daemon=True,
        )
        self._thread.start()
        self._match_thread = threading.Thread(
            target=self._match_loop,
            name="combat-audio-match",
            daemon=True,
        )
        self._match_thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._match_request.set()
        thread = self._thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=1.0)
        match_thread = self._match_thread
        if (
            match_thread is not None
            and match_thread.is_alive()
            and match_thread is not threading.current_thread()
        ):
            match_thread.join(timeout=1.0)
        self._thread = None
        self._match_thread = None
        self._running.clear()

    def _capture_loop(self, microphone) -> None:
        try:
            with microphone.recorder(
                samplerate=self.SAMPLE_RATE,
                channels=2,
                blocksize=self.BLOCK_FRAMES,
            ) as recorder:
                self._running.set()
                while not self._stop.is_set():
                    block = winenv.record(numframes=self.BLOCK_FRAMES)
                    samples = np.asarray(block, dtype=np.float32)
                    if samples.ndim == 2:
                        samples = samples.mean(axis=1)
                    self._append_samples(samples.reshape(-1))
        except Exception as exc:
            if not self._stop.is_set():
                self.log(f"系统回放声音采集中断：{exc}")
        finally:
            self._running.clear()

    def _append_samples(self, samples: np.ndarray) -> None:
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return
        capacity = self._buffer.size
        if samples.size >= capacity:
            samples = samples[-capacity:]
        with self._lock:
            first = min(samples.size, capacity - self._write_index)
            self._buffer[self._write_index:self._write_index + first] = samples[:first]
            remaining = samples.size - first
            if remaining:
                self._buffer[:remaining] = samples[first:]
            self._write_index = (self._write_index + samples.size) % capacity
            self._sample_count = min(capacity, self._sample_count + samples.size)

    def _snapshot(self) -> np.ndarray:
        with self._lock:
            count = self._sample_count
            if count < self._buffer.size:
                return self._buffer[:count].copy()
            return np.concatenate(
                (
                    self._buffer[self._write_index:],
                    self._buffer[:self._write_index],
                )
            )

    def _compute_recent_match(self) -> AudioCueState:
        if self._thread is not None and not self._running.is_set():
            return AudioCueState(False, False, 0.0, 0.0)
        samples = self._snapshot()
        if samples.size < self._template.size:
            return AudioCueState(False, False, 0.0, 0.0)
        recent = samples[-self._template.size:]
        rms = float(np.sqrt(np.mean(recent * recent)))
        if rms < self.MIN_RMS:
            return AudioCueState(True, False, 0.0, rms)
        feature = _time_frequency_feature(samples, self.SAMPLE_RATE)
        template_rows = self._template_feature.shape[0]
        if feature.shape[0] < template_rows:
            return AudioCueState(False, False, 0.0, rms)
        best_score = -1.0
        for start in range(feature.shape[0] - template_rows + 1):
            candidate = feature[start:start + template_rows].copy()
            candidate -= candidate.mean(axis=0, keepdims=True)
            candidate /= candidate.std() + 1e-6
            score = float(np.mean(self._template_feature * candidate))
            best_score = max(best_score, score)
        return AudioCueState(
            True,
            best_score >= self.MATCH_THRESHOLD,
            best_score,
            rms,
        )

    def match_recent(self) -> AudioCueState:
        '同步比对最近声音，仅供离线检测和兼容调用。'
        return self._compute_recent_match()

    def request_match(self) -> None:
        '请求后台执行一次最近声音比对。'
        if self._match_thread is not None and self._match_thread.is_alive():
            self._match_request.set()

    def match_sequence(self) -> int:
        '返回后台声音结果的当前序号。'
        with self._match_lock:
            return self._match_sequence

    def latest_match(
        self,
        after_sequence: int = 0,
    ) -> tuple[int, AudioCueState | None]:
        '读取指定序号之后的最新声音结果。'
        with self._match_lock:
            sequence = self._match_sequence
            if sequence <= after_sequence:
                return sequence, None
            return sequence, self._latest_match

    def _match_loop(self) -> None:
        '按需在后台完成声音特征计算。'
        while not self._stop.is_set():
            if not self._match_request.wait(0.10):
                continue
            self._match_request.clear()
            if self._stop.is_set():
                break
            try:
                state = self._compute_recent_match()
            except Exception as exc:
                self.log(f"声音特征后台比对失败：{exc}")
                state = AudioCueState(False, False, 0.0, 0.0)
            with self._match_lock:
                self._match_sequence += 1
                self._latest_match = state
