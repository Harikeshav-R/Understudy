"""Playbook library, embedding, pgvector retrieval, and LLM confirmation package."""

from understudy.playbook.api import PlaybookLibrary
from understudy.playbook.confirmation import (
    ConfirmationResult,
    PlaybookConfirmer,
    build_confirmation_prompt,
    parse_confirmation_response,
)
from understudy.playbook.embeddings import (
    OpenRouterEmbeddingClient,
    normalize_vector,
)
from understudy.playbook.fakes import FakePlaybookLibrary
from understudy.playbook.retriever import (
    PlaybookMatchResult,
    PlaybookRetriever,
)
from understudy.playbook.signature import (
    build_signature_text,
    deterministic_signature_embedding,
)
from understudy.playbook.write import (
    PlaybookWriter,
    write_playbook,
)

__all__ = [
    "ConfirmationResult",
    "FakePlaybookLibrary",
    "OpenRouterEmbeddingClient",
    "PlaybookConfirmer",
    "PlaybookLibrary",
    "PlaybookMatchResult",
    "PlaybookRetriever",
    "PlaybookWriter",
    "build_confirmation_prompt",
    "build_signature_text",
    "deterministic_signature_embedding",
    "normalize_vector",
    "parse_confirmation_response",
    "write_playbook",
]
