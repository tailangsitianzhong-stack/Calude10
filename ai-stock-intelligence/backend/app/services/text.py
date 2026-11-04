"""検索用の文字列の正規化。全角・半角、大文字・小文字の違いを吸収する。"""
import unicodedata


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return " ".join(s.lower().split())
