"""Small data objects used by the canonical genre taxonomy."""

from dataclasses import dataclass, field


CATEGORIES = ('genre', 'theme', 'demographic')


@dataclass(frozen=True)
class GenreTag:
    """One stable canonical tag, independent of tracker wording."""

    id: str
    category: str

    def __post_init__(self):
        if self.category not in CATEGORIES:
            raise ValueError('Unknown genre category: %s' % self.category)


@dataclass
class GenreNormalization:
    """Normalized tags plus raw values retained only for diagnostics."""

    tags: list = field(default_factory=list)
    unknown_tags: dict = field(default_factory=dict)
