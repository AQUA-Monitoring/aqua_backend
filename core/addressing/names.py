import re
import unicodedata


SMALL_WORDS = {
    "a",
    "ao",
    "aos",
    "as",
    "da",
    "das",
    "de",
    "do",
    "dos",
    "e",
    "em",
    "na",
    "nas",
    "no",
    "nos",
}
PLACEHOLDER_NEIGHBORHOOD = re.compile(r"^bairro\s+\d+$", re.IGNORECASE)
CANONICAL_NAMES = {
    "nova brasilia": "Nova Brasília",
    "sao marcos": "São Marcos",
    "ulysses guimaraes": "Ulysses Guimarães",
}


def normalized(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKD", value or "")
        .encode("ascii", "ignore")
        .decode()
        .casefold()
        .split()
    )


def canonical_name(value: str) -> str:
    formatted = []
    for index, word in enumerate((value or "").strip().split()):
        lower = word.casefold()
        if index > 0 and lower in SMALL_WORDS:
            formatted.append(lower)
        else:
            formatted.append(
                "-".join(part[:1].upper() + part[1:] for part in lower.split("-"))
            )
    result = " ".join(formatted)
    return CANONICAL_NAMES.get(normalized(result), result)


def is_placeholder_neighborhood(value: str) -> bool:
    return bool(PLACEHOLDER_NEIGHBORHOOD.fullmatch((value or "").strip()))
