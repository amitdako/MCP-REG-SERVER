"""Local embedding model (bge-small-en-v1.5 on CPU).

The model loads in a background thread: importing torch + sentence-transformers
takes ~10 s, and the MCP handshake must not wait for that. Callers that need
the model block until it is ready.

Model calls run one at a time (the CPU is the bottleneck: parallel calls only
slow each other), and **searches have priority** over ingest batches: an ingest
embeds batch by batch and, before each batch, yields to any waiting search. A
plain lock is not enough - measured: CPython's lock is unfair, so the ingest
thread re-acquired it batch after batch and searches waited up to ~40 s.
With priority, a search waits for at most the one batch already running.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

import numpy as np

from rag_mcp.tokens import TokenCounter

log = logging.getLogger(__name__)

# bge models are trained with this instruction on the query side only.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...

    def token_counter(self) -> TokenCounter: ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, dim: int, batch_size: int = 16, load_timeout: float = 300) -> None:
        self.model_name = model_name
        self.dim = dim
        self.batch_size = batch_size
        self._load_timeout = load_timeout
        self._model = None
        self._error: BaseException | None = None
        self._ready = threading.Event()
        self._started = False
        self._start_lock = threading.Lock()
        self._model_turn = threading.Condition()
        self._model_busy = False
        self._searches_waiting = 0
        self._query_prefix = BGE_QUERY_PREFIX if "bge" in model_name.lower() else ""

    def start_loading(self) -> None:
        """Start loading in the background (idempotent). Called at server start."""
        with self._start_lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._load, name="model-loader", daemon=True).start()

    def _load(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            from transformers.utils import logging as hf_logging

            hf_logging.disable_progress_bar()  # "Loading weights" bars: noise in the server log
            model = SentenceTransformer(self.model_name, device="cpu")
            actual = model.get_embedding_dimension()
            if actual != self.dim:
                raise RuntimeError(f"{self.model_name} produces {actual} dims, EMBEDDING_DIM is {self.dim}")
            self._model = model
            log.info("embedding model %s loaded", self.model_name)
        except BaseException as exc:  # surfaced to callers in _get_model
            self._error = exc
            log.exception("failed to load embedding model")
        finally:
            self._ready.set()

    def _get_model(self):
        self.start_loading()  # no-op if already started
        if not self._ready.wait(self._load_timeout):
            raise TimeoutError("embedding model is still loading")
        if self._error:
            raise RuntimeError(f"embedding model failed to load: {self._error}")
        return self._model

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        model = self._get_model()
        # Lock per batch, not per call: a big document is hundreds of chunks (minutes),
        # and a search must only wait for the current batch, not the whole document.
        parts = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start:start + self.batch_size]
            parts.append(self._exclusive(lambda: model.encode(
                batch, batch_size=self.batch_size, normalize_embeddings=True,
                show_progress_bar=False, convert_to_numpy=True,
            ), priority=False))
        return np.concatenate(parts) if parts else np.empty((0, self.dim), dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        model = self._get_model()
        return self._exclusive(lambda: model.encode(
            self._query_prefix + text, normalize_embeddings=True,
            show_progress_bar=False, convert_to_numpy=True,
        ), priority=True)

    def _exclusive(self, work, priority: bool):
        """Run `work` with the model to itself. Priority work (searches) goes before
        any non-priority work (ingest batches) that is waiting."""
        with self._model_turn:
            if priority:
                self._searches_waiting += 1
            try:
                while self._model_busy or (not priority and self._searches_waiting):
                    self._model_turn.wait()
            finally:
                if priority:
                    self._searches_waiting -= 1
            self._model_busy = True
        try:
            return work()
        finally:
            with self._model_turn:
                self._model_busy = False
                self._model_turn.notify_all()

    def token_counter(self) -> TokenCounter:
        return TokenCounter(self._get_model().tokenizer)
