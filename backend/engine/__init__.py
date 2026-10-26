"""Native llama.cpp inference engine and integrated model manager."""

from engine.catalog import CATALOG, get_entry
from engine.service import EngineService

__all__ = ["CATALOG", "EngineService", "get_entry"]
