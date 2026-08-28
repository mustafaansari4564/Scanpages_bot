"""
Arabic -> Latin transliteration for Discord autocomplete labels, tuned for
classical Islamic book titles. Two layers:

1. A curated dictionary of common vocabulary/author names that appear
   constantly in this corpus (sahih, bukhari, tafsir, ibn, sharh, ...) --
   these come out correctly cased and vowelled, e.g. "Sahih al-Bukhari".
2. A per-letter fallback for anything not in the dictionary -- this stays
   rough (Arabic without diacritics has no marked short vowels), but only
   kicks in for words the dictionary doesn't know.

This still isn't scholarly-grade (no macrons, no vowel disambiguation for
unknown words), but common titles should read naturally.
"""

import re

# Common vocabulary and author-name components (keyed WITHOUT a leading
# "ال" -- the definite article is detected and re-added as "al-" for
# whichever key form applies).
_WORDS = {
    # hadith collections / major works
    "صحيح": "Sahih", "بخاري": "Bukhari", "مسلم": "Muslim",
    "ترمذي": "Tirmidhi", "نسائي": "Nasa'i", "ماجه": "Majah",
    "داود": "Dawud", "مسند": "Musnad", "سنن": "Sunan",
    "جامع": "Jami'", "موطأ": "Muwatta", "مستدرك": "Mustadrak",
    "معجم": "Mu'jam", "مصنف": "Musannaf",
    # common title vocabulary
    "كتاب": "Kitab", "باب": "Bab", "شرح": "Sharh", "حاشية": "Hashiyah",
    "تفسير": "Tafsir", "فقه": "Fiqh", "اصول": "Usul", "عقيدة": "Aqidah",
    "سنة": "Sunnah", "حديث": "Hadith", "فتح": "Fath", "باري": "Bari",
    "ضعيف": "Da'if", "متروك": "Matruk", "مجروحين": "Majruhin",
    "ثقات": "Thiqat", "رجال": "Rijal", "تاريخ": "Tarikh", "سير": "Siyar",
    "طبقات": "Tabaqat", "مناقب": "Manaqib", "فضائل": "Fada'il",
    "ادب": "Adab", "زهد": "Zuhd", "رقائق": "Raqa'iq", "توحيد": "Tawhid",
    "ايمان": "Iman", "دعاء": "Du'a", "زوائد": "Zawa'id", "بحر": "Bahr",
    "محيط": "Muhit", "امام": "Imam", "شيخ": "Shaykh", "نهاية": "Nihayah",
    "بداية": "Bidayah", "قاموس": "Qamus", "لسان": "Lisan", "عرب": "Arab",
    "زاد": "Zad", "معاد": "Ma'ad", "مدارج": "Madarij", "سالكين": "Salikin",
    "اعلام": "A'lam", "موقعين": "Muwaqqi'in", "احكام": "Ahkam",
    "سلطانية": "Sultaniyyah", "ولايات": "Wilayat", "دينية": "Diniyyah",
    "عظمة": "Azamah", "احاديث": "Ahadith", "قدسية": "Qudsiyyah",
    "اربعينية": "Arba'iniyyah",
    # famous author names / nisbas (with or without the article)
    "طبري": "Tabari", "كثير": "Kathir", "قرطبي": "Qurtubi",
    "ذهبي": "Dhahabi", "حجر": "Hajar", "نووي": "Nawawi", "غزالي": "Ghazali",
    "تيمية": "Taymiyyah", "قيم": "Qayyim", "جوزية": "Jawziyyah",
    "سيوطي": "Suyuti", "دارقطني": "Daraqutni", "بيهقي": "Bayhaqi",
    "حاكم": "Hakim", "طبراني": "Tabarani", "شافعي": "Shafi'i",
    "مالك": "Malik", "حنبل": "Hanbal", "حنيفة": "Hanifah", "قاري": "Qari",
    "اصبهاني": "Asbahani", "خطيب": "Khatib", "بغدادي": "Baghdadi",
    "شيباني": "Shaybani", "كشميري": "Kashmiri", "حويني": "Huwayni",
    "حزم": "Hazm", "رشد": "Rushd", "عربي": "Arabi", "جوزي": "Jawzi",
    "بطال": "Battal", "خطابي": "Khattabi", "اصفهاني": "Isfahani",
    "شريبيني": "Shirbini", "انصاري": "Ansari", "زكريا": "Zakariyya",
    "عقيلي": "Uqayli", "رازي": "Razi",
    # names / relational words
    "بن": "ibn", "ابن": "Ibn", "ابو": "Abu", "ابي": "Abi", "بنت": "Bint",
    "عبد": "Abd", "الله": "Allah", "محمد": "Muhammad", "احمد": "Ahmad",
    "علي": "Ali", "عمر": "Umar", "عثمان": "Uthman", "سلطان": "Sultan",
    "فضيل": "Fudayl", "ضبي": "Dabbi",
    # connectors / common short words
    "على": "'ala", "في": "fi", "من": "min", "الى": "ila", "مع": "ma'a",
    "فيض": "Fayd", "منحة": "Minhah",
}

# One-letter proclitics Arabic attaches directly to the next word with no
# space (bi-, li-, wa-, fa-, ka- -- roughly "with/by", "for/to", "and",
# "so/then", "like/as"). Tried only when the whole word isn't a direct hit.
_PROCLITICS = {"و": "wa", "ف": "fa", "ب": "bi", "ل": "li", "ك": "ka"}

_LETTER_MAP = {
    "ا": "a", "أ": "a", "إ": "i", "آ": "aa", "ء": "'",
    "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h",
    "خ": "kh", "د": "d", "ذ": "dh", "ر": "r", "ز": "z",
    "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t",
    "ظ": "z", "ع": "'", "غ": "gh", "ف": "f", "ق": "q",
    "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h",
    "و": "w", "ي": "y", "ة": "h", "ى": "a", "ئ": "'", "ؤ": "'",
    "َ": "", "ِ": "", "ُ": "", "ً": "", "ٍ": "", "ٌ": "",
    "ْ": "", "ّ": "", "ٰ": "",
}

_TOKEN_RE = re.compile(r"([\u0600-\u06FF]+|[^\u0600-\u06FF]+)")


def _letter_fallback(word: str) -> str:
    return "".join(_LETTER_MAP.get(ch, ch) for ch in word).title()


def _normalize(word: str) -> str:
    """Normalizes hamza-variant alifs to plain alif for dictionary lookup
    only (real Arabic text is inconsistent about which form is used)."""
    return word.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")


def _lookup(word: str) -> str | None:
    """Direct dictionary lookup, handling a leading 'ال' article. Returns
    None (not a fallback string) if this exact word isn't recognized."""
    word = _normalize(word)
    has_article = word.startswith("ال") and len(word) > 2
    core = word[2:] if has_article else word
    found = _WORDS.get(core)
    if found is None:
        return None
    return f"al-{found}" if has_article else found


def _transliterate_word(word: str) -> str:
    direct = _lookup(word)
    if direct is not None:
        return direct
    # "لل" = li- + al- fused (the article's alif elides after li-)
    if _normalize(word).startswith("لل") and len(word) > 2:
        found = _WORDS.get(_normalize(word)[2:])
        if found is not None:
            return f"li-al-{found}"
    if len(word) > 1 and word[0] in _PROCLITICS:
        stripped = _lookup(word[1:])
        if stripped is not None:
            return f"{_PROCLITICS[word[0]]}-{stripped}"
    return _letter_fallback(word)


def transliterate(text: str) -> str:
    """Best-effort Arabic -> Latin, using the curated dictionary where
    possible and falling back to a rough letter mapping otherwise."""
    if not text:
        return ""
    parts = []
    for token in _TOKEN_RE.findall(text):
        if re.match(r"[\u0600-\u06FF]", token):
            parts.append(_transliterate_word(token))
        else:
            parts.append(token)
    result = "".join(parts)
    return " ".join(result.split())
