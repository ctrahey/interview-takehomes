"""Inference clients: one live (Fireworks), one replaying fixtures (D8)."""

from t2s_core.clients.fireworks import FireworksClient
from t2s_core.clients.recorded import RecordedClient, RecordingClient, request_key

__all__ = ["FireworksClient", "RecordedClient", "RecordingClient", "request_key"]
