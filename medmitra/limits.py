"""Silent usage caps. They protect the Groq and Gnani accounts if the link is passed around."""
import threading
import time
from collections import deque


class Budget:
    """Allows at most max_calls per rolling window."""

    def __init__(self, max_calls, window_seconds=3600, clock=time.time):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.clock = clock
        self.calls = deque()
        self._lock = threading.Lock()

    def allow(self):
        with self._lock:
            now = self.clock()
            while self.calls and now - self.calls[0] > self.window_seconds:
                self.calls.popleft()
            if len(self.calls) >= self.max_calls:
                return False
            self.calls.append(now)
            return True
