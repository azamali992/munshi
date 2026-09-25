"""Text normalisation for matching (never for display): Urdu script, Roman Urdu
and English, often in one message.

`fold()` is the only way text reaches a keyword or entity matcher. It is lossy on
purpose -- it maps visually or phonetically interchangeable characters onto one
form so that what people actually type matches what the catalogue says:

  * NFKD, then every combining mark dropped: Arabic harakat, the hamza and madda
    marks (so آ/أ/إ -> ا, ۂ -> ہ, ؤ -> و, ۓ -> ے), Latin accents (é -> e).
  * Arabic-keyboard letters onto their Urdu forms: ي ى ې -> ی, ك -> ک,
    ه ە ة ۃ -> ہ. And, for matching only: ھ -> ہ (people type either for
    the aspirate), ے -> ی, ں -> ن.
  * Urdu-Indic (۰-۹) and Arabic-Indic (٠-٩) digits -> 0-9; ٬ -> , and ٫ -> .
  * Urdu punctuation (، ۔ ؟ ؛) -> ASCII; tatweel, ZWJ/ZWNJ and bidi marks removed.
  * lower case, whitespace collapsed.

`phonetic()` is a Roman-Urdu sound key for fuzzy name matching: Chaudhry,
Chaudhary, Chowdhry, Choudhry and Chaudry all key to 'codri'/'codari', which are
one edit apart. `romanize()` gives Urdu-script tokens a rough Latin form so a
name typed in Urdu can meet a name stored in Roman letters."""
from __future__ import annotations

import re
import unicodedata

_URDU_DIGITS = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")} | {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_MAP = str.maketrans({
    "ي": "ی", "ى": "ی", "ې": "ی", "ۍ": "ی", "ئ": "ی", "ے": "ی",
    "ك": "ک", "ڪ": "ک",
    "ه": "ہ", "ە": "ہ", "ة": "ہ", "ۃ": "ہ", "ۀ": "ہ", "ۂ": "ہ", "ھ": "ہ",
    "ں": "ن", "ٱ": "ا", "ء": "",
    "٬": ",", "٫": ".", "،": ",", "۔": ".", "؟": "?", "؛": ";",
    "ـ": "", "‌": "", "‍": "", "‎": "", "‏": "", "؜": "", " ": " ",
    "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "×": "x",
}) | _URDU_DIGITS


def fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = unicodedata.normalize("NFKC", s).translate(_MAP)
    return re.sub(r"\s+", " ", s.lower()).strip()


def is_urdu(text: str) -> bool:
    """True when most of the letters are Arabic-script: the reply should be in Urdu too."""
    urdu = sum(1 for ch in text if "؀" <= ch <= "ۿ")
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return urdu > 0 and urdu >= latin


def words(folded: str) -> list[str]:
    return re.findall(r"[a-z]+|[؀-ۿ]+|\d+(?:\.\d+)?", folded)


# ------------------------------------------------------------------ Roman-Urdu sound key
def phonetic(word: str) -> str:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    w = w.replace("ch", "C").replace("sh", "S")
    w = re.sub(r"(?<=[bdgjkptrC])h", "", w)            # aspirates: dh->d, kh->k, bh->b, th->t ...
    w = w.replace("v", "w").replace("q", "k")            # Vehari/Wehari, Qasim/Kasim
    for a, b in (("ow", "o"), ("ou", "o"), ("au", "o"), ("aw", "o"), ("ee", "i"), ("ea", "i"), ("oo", "u"), ("aa", "a")):
        w = w.replace(a, b)
    w = w[0] + w[1:].replace("y", "i")
    w = re.sub(r"(.)\1+", r"\1", w)                    # doubled letters
    return w.lower()


def skeleton(word: str) -> str:
    """Consonant skeleton (first letter kept): Urdu script omits short vowels, so a
    romanised Urdu word and its Roman spelling usually agree only on consonants."""
    p = phonetic(word)
    return p[:1] + re.sub(r"[aeiou]", "", p[1:]) if p else ""


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Damerau-Levenshtein (optimal string alignment), stopping early past `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
        if min(prev) > cap:
            return cap + 1
    return prev[-1]


def sounds_like(a: str, b: str) -> bool:
    """Two Roman tokens are the same word, allowing for spelling: exact, same sound key,
    or one edit apart on the sound key when both are long enough for that to be safe."""
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    pa, pb = phonetic(a), phonetic(b)
    if pa == pb:
        return True
    return min(len(pa), len(pb)) >= 5 and edit_distance(pa, pb, 1) <= 1


# ------------------------------------------------------------------ Urdu script -> Roman
_LETTERS = {
    "ا": "a", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s", "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ڈ": "d",
    "ذ": "z", "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s", "ض": "z", "ط": "t", "ظ": "z", "ع": "a",
    "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "گ": "g", "ل": "l", "م": "m", "ن": "n", "و": "o", "ہ": "h", "ی": "i",
}
# Words common in Pakistani trade names, and the Roman spelling they are stored under. Folded keys.
_URDU_WORDS = {w: r for r, ws in {
    "chaudhry": ["چوہدری", "چودہری", "چوہدرى", "چودری"], "farms": ["فارمز"], "farm": ["فارم"], "malik": ["ملک"], "agro": ["ایگرو", "اگرو"],
    "store": ["اسٹور", "سٹور", "اسٹورز", "سٹورز", "سٹور"], "rana": ["رانا"], "brothers": ["برادرز", "برادرس"], "haji": ["حاجی"],
    "sons": ["سنز", "سنس"], "green": ["گرین"], "valley": ["ویلی", "ویلى"], "seeds": ["سیڈز", "سیڈس"], "seed": ["سیڈ"], "punjab": ["پنجاب"],
    "mart": ["مارٹ"], "bhatti": ["بہٹی", "بھٹی"], "kisan": ["کسان"], "shalimar": ["شالیمار"], "agri": ["ایگری"],
    "centre": ["سنٹر", "سینٹر"], "barakah": ["البرکہ", "البرکات", "برکہ", "برکت", "البرکت"], "traders": ["ٹریڈرز", "ٹریڈرس"],
    "new": ["نیو"], "dost": ["دوست"], "fauji": ["فوجی"], "engro": ["اینگرو", "انگرو"], "ali": ["علی"], "akbar": ["اکبر"],
    "group": ["گروپ"], "fertilizer": ["فرٹیلائزر", "فرٹیلائزرز"], "multan": ["ملتان"], "vehari": ["وہاڑی", "وہاری"],
    "khan": ["خان"], "sheikh": ["شیخ"], "butt": ["بٹ"], "ahmad": ["احمد"], "enterprises": ["انٹرپرائزز"], "company": ["کمپنی"],
}.items() for w in ws}
_URDU_WORDS = {fold(k): v for k, v in _URDU_WORDS.items()}


def romanize(token: str) -> str:
    """A folded Urdu-script token in Latin letters: the stored Roman spelling for common
    trade-name words, else a letter-by-letter transliteration (short vowels are absent in
    Urdu script, so compare that with `skeleton`)."""
    if token in _URDU_WORDS:
        return _URDU_WORDS[token]
    return "".join(_LETTERS.get(ch, "") for ch in token)


def has_urdu_word(token: str) -> bool:
    return token in _URDU_WORDS
