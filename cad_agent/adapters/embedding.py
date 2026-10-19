"""Optional local embeddings. No drawing text leaves this process."""

from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import threading
import time

MODEL_ID = "BAAI/bge-small-zh-v1.5"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class EmbeddingUnavailable(RuntimeError):
    pass


class LocalEmbedding:
    def __init__(self, directory):
        path = Path(directory)
        manifest_path = path / "cad-model.json"
        if not manifest_path.is_file():
            raise EmbeddingUnavailable("embedding_model_not_prepared")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("model") != MODEL_ID or not manifest.get("revision"):
            raise EmbeddingUnavailable("embedding_model_manifest_invalid")
        # Verify actual bytes once per process, not just a user-editable revision label.
        for name, expected in manifest["files"].items():
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise EmbeddingUnavailable("embedding_model_manifest_invalid")
            if sha256((path / name).read_bytes()).hexdigest() != expected:
                raise EmbeddingUnavailable("embedding_model_hash_mismatch")
        from sentence_transformers import SentenceTransformer
        import torch

        torch.set_num_threads(4)
        self.model = SentenceTransformer(
            str(path), device="cpu", local_files_only=True, trust_remote_code=False
        )
        self.model.max_seq_length = 512
        self.identity = MODEL_ID + "@" + manifest["revision"] + ":cls:512:normalized:v1"
        self.lock = threading.RLock()
        self.queries = {}

    def encode(self, texts, query=False):
        with self.lock:
            started = time.monotonic()
            if query and tuple(texts) in self.queries:
                vectors = self.queries[tuple(texts)]
                return vectors, {"input_tokens": 0, "cache_hit": True, "seconds": 0.0}
            inputs = [QUERY_PREFIX + t if query else t for t in texts]
            # Count actual tokenizer IDs, including special tokens. Never estimate from chars.
            ids = self.model.tokenizer(inputs, truncation=True, max_length=512)[
                "input_ids"
            ]
            vectors = self.model.encode(
                inputs,
                batch_size=32,
                normalize_embeddings=True,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
            if query:
                if len(self.queries) >= 256:
                    self.queries.clear()
                self.queries[tuple(texts)] = vectors
            return vectors, {
                "input_tokens": sum(map(len, ids)),
                "cache_hit": False,
                "seconds": round(time.monotonic() - started, 4),
            }


@lru_cache(maxsize=2)
def get_embedder(directory):
    try:
        return LocalEmbedding(directory)
    except (ImportError, OSError, ValueError, KeyError) as exc:
        raise EmbeddingUnavailable("embedding_dependency_or_model_unavailable") from exc
