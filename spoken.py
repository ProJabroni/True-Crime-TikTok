#!/usr/bin/env python3
"""Write numbers, dates, years, times and money out in words, the way a narrator says them.

    "On August 31, 1985 at 7:30 p.m." -> "On August thirty-first, nineteen eighty-five at seven thirty P M."

Text-to-speech voices read digits flatly ("August three one"), which sounds robotic.
"""
import re

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
        "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
          "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
MONTH_FULL = {m[:3].lower(): m for m in ["January", "February", "March", "April", "May", "June", "July",
                                         "August", "September", "October", "November", "December"]}
COUNT_WORDS = (r"people|persons|men|women|children|victims|officers|agents|dollars|pounds|euros|miles|kilometers|"
               r"kilometres|feet|meters|metres|yards|times|rounds|shots|hours|days|years|minutes|pages|homes|cars|"
               r"items|pieces|tips|calls|letters|witnesses|suspects|cases|counts|charges")


def words(n):
    """Cardinal number in words: 1234 -> 'one thousand two hundred thirty-four'."""
    n = int(n)
    if n < 0:
        return "minus " + words(-n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("-" + ONES[n % 10] if n % 10 else "")
    if n < 1000:
        return ONES[n // 100] + " hundred" + (" " + words(n % 100) if n % 100 else "")
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= size:
            return words(n // size) + " " + name + (" " + words(n % size) if n % size else "")
    return str(n)


def ordinal(n):
    """31 -> 'thirty-first'."""
    w = words(n)
    head, sep, last = w.rpartition("-") if "-" in w.split(" ")[-1] else w.rpartition(" ")
    special = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
               "nine": "ninth", "twelve": "twelfth"}
    if last in special:
        last = special[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    return head + sep + last


def year(y):
    """1985 -> 'nineteen eighty-five', 1905 -> 'nineteen oh five', 2008 -> 'two thousand eight', 2019 -> 'twenty nineteen'."""
    y = int(y)
    if 2000 <= y <= 2009:
        return "two thousand" + (" " + words(y % 100) if y % 100 else "")
    hi, lo = divmod(y, 100)
    if lo == 0:
        return words(hi) + " hundred"
    if lo < 10:
        return words(hi) + " oh " + words(lo)
    return words(hi) + " " + words(lo)


def _decimal(num):
    num = num.replace(",", "")
    if "." in num:
        whole, frac = num.split(".", 1)
        return words(int(whole or 0)) + " point " + " ".join(ONES[int(d)] for d in frac)
    return words(int(num))


def spoken(text):
    t = text

    # Month + day (+ suffix): "August 31" / "Aug. 31st" -> "August thirty-first"
    def month_day(m):
        month = MONTH_FULL.get(m.group(1)[:3].lower(), m.group(1))
        return f"{month} {ordinal(int(m.group(2)))}"
    t = re.sub(r"\b(" + MONTHS + r")\.? (\d{1,2})(?:st|nd|rd|th)?\b(?!:)", month_day, t)

    # Day + month: "31 August" / "31st of August" -> "the thirty-first of August"
    def day_month(m):
        month = MONTH_FULL.get(m.group(2)[:3].lower(), m.group(2))
        return f"the {ordinal(int(m.group(1)))} of {month}"
    t = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)?(?: of)? (" + MONTHS + r")\b\.?", day_month, t)

    # Decades: "1970s" -> "nineteen seventies", "the '80s" -> "the eighties"
    def decade(m):
        w = year(m.group(1) + "0")
        return (w[:-1] + "ies") if w.endswith("y") else w + "s"
    t = re.sub(r"\b(1\d\d|20\d)0s\b", decade, t)
    t = re.sub(r"'(\d)0s\b", lambda m: (TENS[int(m.group(1))][:-1] + "ies") if int(m.group(1)) > 1 else m.group(0), t)

    # Money: "$1.5 million" -> "one point five million dollars", "$500" -> "five hundred dollars"
    def money(m):
        amount, scale = m.group(1), m.group(2)
        return _decimal(amount) + (" " + scale if scale else "") + " dollars"
    t = re.sub(r"\$\s?(\d[\d,]*(?:\.\d+)?)(?:\s?(million|billion|thousand))?", money, t)
    t = re.sub(r"£\s?(\d[\d,]*(?:\.\d+)?)(?:\s?(million|billion|thousand))?",
               lambda m: _decimal(m.group(1)) + (" " + m.group(2) if m.group(2) else "") + " pounds", t)
    t = re.sub(r"€\s?(\d[\d,]*(?:\.\d+)?)(?:\s?(million|billion|thousand))?",
               lambda m: _decimal(m.group(1)) + (" " + m.group(2) if m.group(2) else "") + " euros", t)

    # Percent
    t = re.sub(r"(\d+(?:\.\d+)?)\s?(%|percent\b)", lambda m: _decimal(m.group(1)) + " percent", t)

    # Times: "7:30 p.m." -> "seven thirty P M", "10:05" -> "ten oh five"
    def clock(m):
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
        out = words(h) + ("" if mi == 0 else " oh " + words(mi) if mi < 10 else " " + words(mi))
        if mi == 0 and not ap:
            out += " o'clock"
        if ap:
            out += " A M" if ap.lower().startswith("a") else " P M"
        return out
    t = re.sub(r"\b(\d{1,2}):(\d{2})(?:\s?([aApP]\.?[mM](?:\.(?![\s]|$))?)(?![A-Za-z]))?", clock, t)
    t = re.sub(r"\b(\d{1,2})\s?([aApP])\.?[mM]\b(?:\.(?![\s]|$))?", lambda m: words(int(m.group(1))) + (" A M" if m.group(2).lower() == "a" else " P M"), t)

    # Ordinals: "21st" -> "twenty-first"
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal(int(m.group(1))), t)

    # Years: 4-digit 1100-2099 not followed by a counted noun ("1985" yes, "1500 people" no)
    t = re.sub(r"\b(1[1-9]\d\d|20\d\d)\b(?!,\d)(?!\s+(?:" + COUNT_WORDS + r")\b)", lambda m: year(m.group(1)), t)

    t = re.sub(r"([AP] M)\. (?=[a-z])", r"\1 ", t)  # "seven thirty A M. she left" -> no stray full stop

    # Anything else: "1,200" -> "one thousand two hundred", "3.5" -> "three point five"
    t = re.sub(r"\d[\d,]*(?:\.\d+)?", lambda m: _decimal(m.group(0)) if re.search(r"\d", m.group(0)) else m.group(0), t)
    return t


if __name__ == "__main__":
    import sys
    print(spoken(" ".join(sys.argv[1:])))
