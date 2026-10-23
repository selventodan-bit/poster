"""Data-only localization packs; structural evidence is required alongside localized labels."""

import json
import re
from pathlib import Path


class Localizations:
    def __init__(self, extra: Path | None = None):
        self.words = json.loads((Path(__file__).parent / "data/locales.json").read_text(encoding="utf-8"))
        if extra:
            additional = json.loads(extra.read_text(encoding="utf-8"))
            if set(additional) - set(self.words):
                raise ValueError("Unknown localization role")
            for role, words in additional.items():
                if (
                    not isinstance(words, list)
                    or len(words) > 200
                    or any(not isinstance(w, str) or len(w) > 80 for w in words)
                ):
                    raise ValueError("Invalid localization pack")
                self.words[role].extend(words)

    def has(self, role, value):
        value = str(value).casefold().replace("_", " ").replace("-", " ")
        return any(
            re.search(r"(?<!\w)" + re.escape(w.casefold()) + r"(?!\w)", value) for w in self.words[role]
        )
