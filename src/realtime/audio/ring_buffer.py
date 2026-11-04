"""Fixed-size circular buffer for PCM audio bytes."""

from __future__ import annotations

import threading


class RingBuffer:
    """Fixed-size circular buffer for 16-bit mono PCM byte streams."""

    def __init__(self, sample_rate: int, duration_ms: int) -> None:
        samples = max(1, sample_rate * duration_ms // 1000)
        self._capacity = samples * 2
        self._data = bytearray(self._capacity)
        self._write_pos = 0
        self._size = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def size(self) -> int:
        with self._lock:
            return self._size

    def clear(self) -> None:
        with self._lock:
            self._write_pos = 0
            self._size = 0

    def write(self, data: bytes) -> None:
        if not data:
            return

        with self._lock:
            data_len = len(data)
            if data_len >= self._capacity:
                self._data[:] = data[data_len - self._capacity :]
                self._write_pos = 0
                self._size = self._capacity
                return

            space_to_end = self._capacity - self._write_pos
            if data_len <= space_to_end:
                self._data[self._write_pos : self._write_pos + data_len] = data
                self._write_pos = (self._write_pos + data_len) % self._capacity
            else:
                self._data[self._write_pos :] = data[:space_to_end]
                wrap_len = data_len - space_to_end
                self._data[:wrap_len] = data[space_to_end:]
                self._write_pos = wrap_len

            self._size = min(self._capacity, self._size + data_len)

    def read_all(self) -> bytes:
        with self._lock:
            if self._size == 0:
                return b""

            if self._size < self._capacity:
                return bytes(self._data[: self._size])

            return bytes(self._data[self._write_pos :] + self._data[: self._write_pos])
