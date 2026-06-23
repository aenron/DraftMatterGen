from abc import ABC, abstractmethod
from pathlib import Path


class DocumentParser(ABC):
    @abstractmethod
    async def extract(self, path: Path) -> str:
        """Extract readable text from a document."""

