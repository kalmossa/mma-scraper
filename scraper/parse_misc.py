"""
parse_misc.py
Reads a division CSV file (e.g. "tables flyweights - Sheet 1.csv")
whose `misc` column contains the raw copy-paste of the 3 MMA sites,
separated by ///.

Automatically fills every empty column, then merges into
"final tables - fighters.csv".

USAGE:
    py parse_misc.py "tables flyweights - Sheet 1.csv"
    py parse_misc.py   (defaults to flyweight)
"""

import csv, json, re, sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent
FINAL_CSV = ROOT / "tables finales - fighters.csv"

#  mapping nationalite -> CODE PAYS + emoji drapeau

def _flag(iso2: str) -> str:
    """Convert an ISO-2 code into a flag emoji."""
    return ''.join(chr(0x1F1E6 + ord(c.upper()) - ord('A')) for c in iso2)

COUNTRY_MAP = {
    "United States":("USA",_flag("US")), "American":("USA",_flag("US")),
    "USA":("USA",_flag("US")), "US":("USA",_flag("US")),
    "Japan":("JPN",_flag("JP")), "Japanese":("JPN",_flag("JP")),
    "Brazil":("BRA",_flag("BR")), "Brazilian":("BRA",_flag("BR")),
    "Russia":("RUS",_flag("RU")), "Russian":("RUS",_flag("RU")),
    "Dagestan":("RUS",_flag("RU")),
    "Mexico":("MEX",_flag("MX")), "Mexican":("MEX",_flag("MX")),
    "United Kingdom":("GBR",_flag("GB")), "England":("GBR",_flag("GB")),
    "British":("GBR",_flag("GB")), "English":("GBR",_flag("GB")),
    "Scotland":("GBR",_flag("GB")), "Scottish":("GBR",_flag("GB")),
    "Northern Ireland":("GBR",_flag("GB")),
    "Canada":("CAN",_flag("CA")), "Canadian":("CAN",_flag("CA")),
    "Australia":("AUS",_flag("AU")), "Australian":("AUS",_flag("AU")),
    "France":("FRA",_flag("FR")), "French":("FRA",_flag("FR")),
    "Germany":("GER",_flag("DE")), "German":("GER",_flag("DE")),
    "China":("CHN",_flag("CN")), "Chinese":("CHN",_flag("CN")),
    "South Korea":("KOR",_flag("KR")), "Korea":("KOR",_flag("KR")),
    "Korean":("KOR",_flag("KR")),
    "Ireland":("IRL",_flag("IE")), "Irish":("IRL",_flag("IE")),
    "Poland":("POL",_flag("PL")), "Polish":("POL",_flag("PL")),
    "Netherlands":("NED",_flag("NL")), "Dutch":("NED",_flag("NL")),
    "Sweden":("SWE",_flag("SE")), "Swedish":("SWE",_flag("SE")),
    "Spain":("ESP",_flag("ES")), "Spanish":("ESP",_flag("ES")),
    "Argentina":("ARG",_flag("AR")), "Argentine":("ARG",_flag("AR")),
    "Italy":("ITA",_flag("IT")), "Italian":("ITA",_flag("IT")),
    "Kazakhstan":("KAZ",_flag("KZ")), "Kazakh":("KAZ",_flag("KZ")),
    "New Zealand":("NZL",_flag("NZ")),
    "Myanmar":("MMR",_flag("MM")), "Burma":("MMR",_flag("MM")),
    "Suriname":("SUR",_flag("SR")),
    "Czech Republic":("CZE",_flag("CZ")), "Czech":("CZE",_flag("CZ")),
    "Croatia":("CRO",_flag("HR")), "Croatian":("CRO",_flag("HR")),
    "Ukraine":("UKR",_flag("UA")), "Ukrainian":("UKR",_flag("UA")),
    "Belarus":("BLR",_flag("BY")),
    "Turkey":("TUR",_flag("TR")), "Turkish":("TUR",_flag("TR")),
    "Georgia":("GEO",_flag("GE")), "Georgian":("GEO",_flag("GE")),
    "Armenia":("ARM",_flag("AM")), "Azerbaijan":("AZE",_flag("AZ")),
    "Norway":("NOR",_flag("NO")), "Norwegian":("NOR",_flag("NO")),
    "Denmark":("DEN",_flag("DK")), "Danish":("DEN",_flag("DK")),
    "Finland":("FIN",_flag("FI")), "Finnish":("FIN",_flag("FI")),
    "Iceland":("ISL",_flag("IS")), "Icelandic":("ISL",_flag("IS")),
    "Switzerland":("SUI",_flag("CH")), "Swiss":("SUI",_flag("CH")),
    "Austria":("AUT",_flag("AT")), "Belgium":("BEL",_flag("BE")),
    "Portugal":("POR",_flag("PT")), "Portuguese":("POR",_flag("PT")),
    "Greece":("GRE",_flag("GR")), "Greek":("GRE",_flag("GR")),
    "Romania":("ROU",_flag("RO")), "Hungary":("HUN",_flag("HU")),
    "Serbia":("SRB",_flag("RS")), "Bosnia":("BIH",_flag("BA")),
    "Slovakia":("SVK",_flag("SK")), "Slovenia":("SLO",_flag("SI")),
    "Bulgaria":("BUL",_flag("BG")), "Moldova":("MDA",_flag("MD")),
    "Albania":("ALB",_flag("AL")),
    "Iran":("IRN",_flag("IR")), "Iranian":("IRN",_flag("IR")),
    "Israel":("ISR",_flag("IL")), "Israeli":("ISR",_flag("IL")),
    "Lebanon":("LBN",_flag("LB")),
    "Mongolia":("MGL",_flag("MN")), "Mongolian":("MGL",_flag("MN")),
    "Kyrgyzstan":("KGZ",_flag("KG")), "Uzbekistan":("UZB",_flag("UZ")),
    "Tajikistan":("TJK",_flag("TJ")),
    "Philippines":("PHI",_flag("PH")), "Filipino":("PHI",_flag("PH")),
    "Thailand":("THA",_flag("TH")), "Thai":("THA",_flag("TH")),
    "Vietnam":("VNM",_flag("VN")), "Vietnamese":("VNM",_flag("VN")),
    "Indonesia":("IDN",_flag("ID")), "Singapore":("SGP",_flag("SG")),
    "India":("IND",_flag("IN")), "Indian":("IND",_flag("IN")),
    "Pakistan":("PAK",_flag("PK")), "Afghanistan":("AFG",_flag("AF")),
    "Cameroon":("CMR",_flag("CM")), "Nigeria":("NGA",_flag("NG")),
    "Nigerian":("NGA",_flag("NG")),
    "South Africa":("RSA",_flag("ZA")), "Ghana":("GHA",_flag("GH")),
    "Kenya":("KEN",_flag("KE")), "Senegal":("SEN",_flag("SN")),
    "Egypt":("EGY",_flag("EG")), "Morocco":("MAR",_flag("MA")),
    "Tunisia":("TUN",_flag("TN")), "Algeria":("ALG",_flag("DZ")),
    "Chile":("CHI",_flag("CL")), "Colombia":("COL",_flag("CO")),
    "Colombian":("COL",_flag("CO")), "Peru":("PER",_flag("PE")),
    "Venezuela":("VEN",_flag("VE")), "Cuba":("CUB",_flag("CU")),
    "Cuban":("CUB",_flag("CU")), "Ecuador":("ECU",_flag("EC")),
    "Uruguay":("URU",_flag("UY")),
    "Angola":("ANG",_flag("AO")), "Angolan":("ANG",_flag("AO")),
    "Bolivia":("BOL",_flag("BO")), "Paraguay":("PAR",_flag("PY")),
    "Dominican Republic":("DOM",_flag("DO")), "Dominican":("DOM",_flag("DO")),
    "Puerto Rico":("PUR",_flag("PR")), "Jamaica":("JAM",_flag("JM")),
    "Trinidad and Tobago":("TTO",_flag("TT")),
    "Hawaii":("USA",_flag("US")),
    "Cape Verde":("CPV",_flag("CV")), "Mozambique":("MOZ",_flag("MZ")),
    "Ivory Coast":("CIV",_flag("CI")), "DR Congo":("COD",_flag("CD")),
    "Ethiopia":("ETH",_flag("ET")), "Sudan":("SUD",_flag("SD")),
    "Saudi Arabia":("KSA",_flag("SA")), "UAE":("UAE",_flag("AE")),
    "Bahrain":("BRN",_flag("BH")), "Qatar":("QAT",_flag("QA")),
    "Jordan":("JOR",_flag("JO")), "Iraq":("IRQ",_flag("IQ")),
    "Syria":("SYR",_flag("SY")), "Yemen":("YEM",_flag("YE")),
    "Palestine":("PLE",_flag("PS")),
    "Malaysia":("MAS",_flag("MY")), "Cambodia":("CAM",_flag("KH")),
    "Laos":("LAO",_flag("LA")), "Nepal":("NEP",_flag("NP")),
    "Bangladesh":("BAN",_flag("BD")), "Sri Lanka":("SRI",_flag("LK")),
    "Taiwan":("TPE",_flag("TW")), "Hong Kong":("HKG",_flag("HK")),
    "North Macedonia":("MKD",_flag("MK")), "Macedonia":("MKD",_flag("MK")),
    "Estonia":("EST",_flag("EE")), "Latvia":("LAT",_flag("LV")),
    "Lithuania":("LTU",_flag("LT")), "Luxembourg":("LUX",_flag("LU")),
    "Wales":("GBR",_flag("GB")), "Welsh":("GBR",_flag("GB")),
}

def lookup_country(nat: str) -> tuple[str, str]:
    """nationality -> (country_code3, emoji). Match insensitive to suffixes."""
    if not nat: return ("", "")
    nat_clean = nat.strip().strip(",.").strip()
    if nat_clean in COUNTRY_MAP:
        return COUNTRY_MAP[nat_clean]
    # Also try without capital letters
    for k, v in COUNTRY_MAP.items():
        if k.lower() == nat_clean.lower():
            return v
    # Match partiel (ex: "Born in Tokyo, Japan")
    for k, v in COUNTRY_MAP.items():
        if k.lower() in nat_clean.lower():
            return v
    return ("", "")

def lookup_by_iso2(iso2: str) -> tuple[str, str, str]:
    """ISO-2 code -> (country_name, country_code3, flag_emoji). First match in COUNTRY_MAP."""
    if not iso2 or len(iso2) < 2: return ("", "", "")
    target = _flag(iso2.upper()[:2])
    for name, (code3, flag) in COUNTRY_MAP.items():
        if flag == target:
            return (name, code3, flag)
    return ("", "", "")

COLUMNS = [
    "id","ufc_id","fightmatrix_id","mma_com_id",
    "name","nickname","gender","date_of_birth","age","nationality",
    "country_code","flag_emoji","photo_url","photo_thumbnail_url",
    "weight_class_current","weight_class_origin","weight_class_history_text",
    "height_inches","reach_inches","stance",
    "first_sport",
    "record_total_wins","record_total_losses","record_total_draws","record_total_nc",
    "record_ufc_wins","record_ufc_losses","record_ufc_draws",
    "record_other_wins","record_other_losses","record_other_draws",
    "wins_by_ko_tko","wins_by_submission","wins_by_decision",
    "losses_by_ko_tko","losses_by_submission","losses_by_decision",
    "split_decision_wins","split_decision_losses",
    "career_debut_date","last_fight_date","days_inactive",
    "is_active","current_league","is_ufc_champion","ufc_title_match_win","total_fights",
    "win_percentage_int","finish_rate_int","current_streak","last_5_results",
        "fightmatrix_rating_points","fightmatrix_big_league_record",
    "fightmatrix_540_metric","fightmatrix_quality_perf_pct",
    "ufc_official_rank","ufc_p4p_rank",
    "created_at","updated_at","last_scraped_at",
    "data_source","data_quality_score","is_verified",
    "notes","fight_history","misc",
]

#  utils

def _fill(row, key, value):
    """Only replace if the cell is empty."""
    if key in row and not str(row.get(key, "")).strip():
        row[key] = str(value)

def parse_record(text):
    m = re.search(r"(\d+)-(\d+)(?:-(\d+))?", text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return None, None, None

def height_to_inches(text):
    m = re.search(r"(\d+)'\s*(\d+)", text)
    if m:
        return int(m.group(1)) * 12 + int(m.group(2))
    return ""

def reach_to_inches(text):
    m = re.search(r'([\d.]+)"', text)
    if m:
        return float(m.group(1))
    m = re.search(r"([\d.]+)\s*cm", text, re.I)
    if m:
        return round(float(m.group(1)) / 2.54, 1)
    return ""

def fmt_date(text):
    """Normalize to YYYY-MM-DD from M/DD/YYYY or YYYY-MM-DD."""
    text = text.strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return text

def days_since(date_str):
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
        try:
            return (date.today() - datetime.strptime(date_str.strip(), fmt).date()).days
        except ValueError:
            pass
    return ""

# SOURCE detection

def detect_source(text):
    if re.search(r"Issue Date:.*Official Release", text):
        return "fightmatrix"
    if re.search(r"Fighter Details.*(?:Given Name|Nickname):", text, re.S):
        return "tapology"
    if re.search(r"(?:Given Name|Nickname):\s*\n", text) and "Date of Birth" in text:
        return "tapology"
    if re.search(r"Finish Rate|Record BreakDown|Career Length|Avg\. Fights/Year|Quickest Turnaround", text):
        return "underground"
    return "unknown"

#  parser fightmatrix

def parse_fightmatrix(text, row):
    # Pro Record
    m = re.search(r"Pro Record:\s*([\d]+-[\d]+-[\d]+)", text)
    if m:
        w, l, d = parse_record(m.group(1))
        if w is not None:
            _fill(row, "record_total_wins", w)
            _fill(row, "record_total_losses", l)
            _fill(row, "record_total_draws", d)
            _fill(row, "total_fights", w + l + d)

    # Birth Date
    m = re.search(r"Birth Date:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        _fill(row, "date_of_birth", m.group(1))

    # Pro Debut Date
    m = re.search(r"Pro Debut Date:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        _fill(row, "career_debut_date", m.group(1))

    # last fight date: M/DD/YYYY
    m = re.search(r"Last Fight Date:\s*([\d/]+)", text)
    if m:
        _fill(row, "last_fight_date", fmt_date(m.group(1)))

    # last 5: W W W W W
    m = re.search(r"Last 5:\s*([WLD ]+)", text)
    if m:
        results = re.findall(r"[WLD]", m.group(1))[:5]
        _fill(row, "last_5_results", ",".join(results))
        if results:
            char = results[0]
            cnt = sum(1 for r in results if r == char)
            _fill(row, "current_streak", f"{cnt}{char}")

    # Rating Points
    m = re.search(r"Rating Points:\s*(\d+)", text)
    if m:
        _fill(row, "fightmatrix_rating_points", m.group(1))

    # 'Big League' Record
    m = re.search(r"'Big League' Record:\s*([\d]+-[\d]+-[\d]+)", text)
    if m:
        _fill(row, "fightmatrix_big_league_record", m.group(1))

    # 540 Metric
    m = re.search(r"540 Metric:\s*([\d.]+)", text)
    if m:
        _fill(row, "fightmatrix_540_metric", m.group(1))

    # Quality Perf. %
    m = re.search(r"Quality Perf\.?\s*%:\s*([\d.]+)", text)
    if m:
        _fill(row, "fightmatrix_quality_perf_pct", m.group(1))

    # Win Finish %
    m = re.search(r"Win Finish\s*%:\s*([\d.]+)", text)
    if m:
        _fill(row, "finish_rate_int", str(int(float(m.group(1)))))

    # Longest Win Streak: 7 (2024-2026, active) -> current_streak if active
    m = re.search(r"Longest Win Streak:\s*(\d+)[^)]*active\)", text, re.I)
    if m:
        row["current_streak"] = f"{m.group(1)}W"  # override (FM has the real number)
    m2 = re.search(r"Longest Loss Streak:\s*(\d+)[^)]*active\)", text, re.I)
    if m2:
        row["current_streak"] = f"{m2.group(1)}L"

    # UFC record: 10-1-0 , 1 NC
    m = re.search(r"UFC Record:\s*([\d]+-[\d]+-[\d]+)(?:\s*,\s*(\d+)\s*NC)?", text)
    if m:
        w, l, d = parse_record(m.group(1))
        if w is not None:
            _fill(row, "record_ufc_wins", w)
            _fill(row, "record_ufc_losses", l)
            _fill(row, "record_ufc_draws", d)
        if m.group(2):
            _fill(row, "record_total_nc", m.group(2))

    # Current Ranking: #1 Flyweight -> ufc_official_rank  (with or without a space after the colon)
    m = re.search(r"Current Ranking:\s*#(\d+)\s+\w", text)
    if m:
        _fill(row, "ufc_official_rank", m.group(1))

    # #N Pound-For-Pound → ufc_p4p_rank
    m = re.search(r"#(\d+)\s+Pound-For-Pound", text)
    if m:
        _fill(row, "ufc_p4p_rank", m.group(1))

    # Association → notes
    m = re.search(r"Association:\s*(.+?)(?:Pro Debut|$)", text, re.M)
    if m:
        assoc = m.group(1).strip()
        if assoc:
            notes = str(row.get("notes", "")).strip()
            tag = f"Team: {assoc}"
            if tag not in notes:
                row["notes"] = f"{notes} | {tag}".strip(" |") if notes else tag

    # gender from slug
    if not row.get("gender"):
        row["gender"] = "M"

#  parser tapology

def parse_tapology(text, row):
    # Nickname (plusieurs formats Tapology)
    m = re.search(r'(?:Nickname|"The [^"]+")[\s\S]{0,5}?(?:Nickname:\s*)?([^\n"]{2,40})', text)
    m2 = re.search(r'Nickname:\s*\n?\s*([^\n"]{2,40})', text)
    nick = (m2 or m)
    if nick:
        val = nick.group(1).strip().strip('"')
        if val.lower() not in ("n/a", "", "pro mma record"):
            _fill(row, "nickname", val)

    # Age | Date of Birth: "Age: 24 | Date of Birth: 2001 Oct 10"
    m = re.search(r"Age:\s*(\d+)\s*\|.*?Date of Birth:\s*(\d{4})\s+(\w+)\s+(\d+)", text)
    if m:
        _fill(row, "age", m.group(1))
        try:
            dob = datetime.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)}", "%Y %B %d")
            _fill(row, "date_of_birth", dob.strftime("%Y-%m-%d"))
        except ValueError:
            pass

    # Height: stored in the format "5'4\"" (string, as in the examples)
    m = re.search(r"Height:\s*(\d+)'\s*(\d+)", text)
    if m:
        _fill(row, "height_inches", f"{m.group(1)}'{m.group(2)}\"")

    # Reach: format "63.0\"" (with the trailing double quote)
    m = re.search(r'Reach:\s*([\d.]+)\s*"', text)
    if m:
        _fill(row, "reach_inches", f'{m.group(1)}"')

    # Stance : Orthodox / Southpaw / Switch
    m = re.search(r"Stance:?\s*(Orthodox|Southpaw|Switch|Open Stance)", text, re.I)
    if m:
        _fill(row, "stance", m.group(1).title())

    # Weight Class : Flyweight / Bantamweight / ...
    m = re.search(r"Weight Class:?\s*(\w[\w ]*?)(?:\s*\||\s*Last|\n)", text)
    if m:
        wc = m.group(1).strip()
        if wc and len(wc) < 40:
            _fill(row, "weight_class_current", wc)
            _fill(row, "weight_class_origin", wc)  # default = current

    # Foundation Style -> first_sport
    m = re.search(r"Foundation Style:?\s*([\w\- ]{2,30})", text)
    if m:
        st = m.group(1).strip()
        if st.lower() not in ("n/a", "none", "look-see-do"):
            _fill(row, "first_sport", st)

    # Born: City, Country  -> nationality + country_code + flag_emoji
    m = re.search(r"Born:\s*(.+?)(?:\n|Fighting out of)", text, re.S)
    if m:
        parts = [p.strip() for p in m.group(1).strip().split(",")]
        if parts:
            country = parts[-1].strip()
            _fill(row, "nationality", country)
            cc, flag = lookup_country(country)
            if cc:
                _fill(row, "country_code", cc)
                _fill(row, "flag_emoji",   flag)

    # Win methods: Tapology formats them differently depending on the fighter.
    # We isolate the "Pro MMA Statistics" section to avoid matching
    # the "Submission/Decision" words in the fight history.
    stats_start = text.find("Pro MMA Statistics")
    stats_end   = text.find("MMA Record By Promotion")
    if stats_start < 0:
        stats_start = text.find("Pro MMA Stats")
    if stats_end < 0 or stats_end < stats_start:
        stats_end = stats_start + 3000 if stats_start >= 0 else len(text)
    stats_zone = text[stats_start:stats_end] if stats_start >= 0 else text

    # Flexible format: "KO/TKO ... N wins ... N loss" (multi-line OK)
    m = re.search(r"KO/TKO[\s\S]{0,80}?(\d+)\s+wins?[,\s]+(\d+)\s+loss",
                  stats_zone, re.I)
    if m:
        _fill(row, "wins_by_ko_tko",   m.group(1))
        _fill(row, "losses_by_ko_tko", m.group(2))

    m = re.search(r"Submission[\s\S]{0,80}?(\d+)\s+wins?[,\s]+(\d+)\s+loss",
                  stats_zone, re.I)
    if m:
        _fill(row, "wins_by_submission",   m.group(1))
        _fill(row, "losses_by_submission", m.group(2))

    m = re.search(r"Decision[\s\S]{0,80}?(\d+)\s+wins?[,\s]+(\d+)\s+loss",
                  stats_zone, re.I)
    if m:
        _fill(row, "wins_by_decision",   m.group(1))
        _fill(row, "losses_by_decision", m.group(2))

    # UFC record from promotion breakdown: "UFC 10 win 1 loss 0 draw 0 no contest"
    m = re.search(r"UFC\s+(\d+)\s+win\s+(\d+)\s+loss\s+(\d+)\s+draw\s+(\d+)\s+no contest", text, re.I)
    if m:
        _fill(row, "record_ufc_wins", m.group(1))
        _fill(row, "record_ufc_losses", m.group(2))
        _fill(row, "record_ufc_draws", m.group(3))

    # last fight: may 09, 2026 in UFC
    m = re.search(r"Last Fight:\s*(.+?)\s+in\s+\w", text)
    if m:
        _fill(row, "last_fight_date", fmt_date(m.group(1).strip()))

    # photo URL
    m = re.search(r"(https?://images\.tapology\.com/\S+\.(?:jpg|jpeg|png|gif)\S*)", text, re.I)
    if m:
        _fill(row, "photo_url", m.group(1).rstrip(")\"'"))

    # Pro Record from Tapology stats: "Record: 17-2-0"
    m = re.search(r"(?:Pro MMA )?Record:\s*([\d]+-[\d]+-[\d]+)", text)
    if m:
        w, l, d = parse_record(m.group(1))
        if w is not None:
            _fill(row, "record_total_wins", w)
            _fill(row, "record_total_losses", l)
            _fill(row, "record_total_draws", d)
            _fill(row, "total_fights", w + l + d)

#  parser MMA.COM / underground

def parse_underground(text, row):
    # Height: "5' 5""
    m = re.search(r"Height\s+(\d+)'\s*(\d+)", text)
    if m:
        _fill(row, "height_inches", int(m.group(1)) * 12 + int(m.group(2)))

    # Age
    m = re.search(r"^Age\s+(\d+)", text, re.M)
    if m:
        _fill(row, "age", m.group(1))

    # record breakdown table: wins N N N / losses N N N
    m = re.search(
        r"Record BreakDown\s*[-–]\s*KO/TKO\s+SUB\s+DEC\s*"
        r"Wins\s+(\d+)\s+(\d+)\s+(\d+)\s*"
        r"Losses\s+(\d+)\s+(\d+)\s+(\d+)",
        text, re.I | re.S
    )
    if m:
        _fill(row, "wins_by_ko_tko",     m.group(1))
        _fill(row, "wins_by_submission", m.group(2))
        _fill(row, "wins_by_decision",   m.group(3))
        _fill(row, "losses_by_ko_tko",     m.group(4))
        _fill(row, "losses_by_submission", m.group(5))
        _fill(row, "losses_by_decision",   m.group(6))

    # Finish Rate: 62.5%
    m = re.search(r"Finish Rate\s+([\d.]+)%", text)
    if m:
        _fill(row, "finish_rate_int", str(int(float(m.group(1)))))

    # Current Streak: "6 Wins" or "1 Loss" - Underground has the real number, it overrides
    m = re.search(r"Current Streak\s+(\d+)\s+(Wins?|Losses?)", text)
    if m:
        char = "W" if "Win" in m.group(2) else "L"
        row["current_streak"] = f"{m.group(1)}{char}"

    # Pro Record: "17-2-0 (Win-Loss-Draw)"
    m = re.search(r"Pro Record\s+([\d]+-[\d]+-[\d]+)", text)
    if m:
        w, l, d = parse_record(m.group(1))
        if w is not None:
            _fill(row, "record_total_wins", w)
            _fill(row, "record_total_losses", l)
            _fill(row, "record_total_draws", d)

# league/promotion detection (from fight_history)
#
# Strategy: 100% dynamic, never a hard-coded list of leagues.
# We extract the prefix of the event text (= the league name) by cutting
# before the first number, "vs", "-" or ":" that marks the fight title.
#
# Exemples :
#   "UFC 326 Holloway vs. Oliveira 2"  -> prefixe "UFC"      -> "UFC"
#   "Bellator 300 - McKee vs. Pitbull" -> prefixe "Bellator" -> "Bellator"
#   "Ares 38 Zebo vs. Mustafaev"       -> prefixe "Ares"     -> "Ares"
#   "PFL World Tournament 2024 - SF"   -> prefixe "PFL World Tournament"
#   "HXMMA Hexagone MMA Series 4"      -> prefixe "HXMMA Hexagone MMA Series"
# "Rousey vs. Carano"                -> prefix "Rousey"   -> rejected (no league)
#
# A few minimal normalizations for the only case where the brand has many
# sub-series (UFC Fight Night, UFC on ESPN, Road to UFC, DWCS = all UFC).
# This is the ONLY exception: for everything else, the prefix is returned as is.

# English weekdays + months (FM/Tapology stick them to the event name)
_DATE_SUFFIX_RE = re.compile(
    r"\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b.*$",
    re.IGNORECASE,
)

# The prefix = everything before the 1st event number, "vs", "-" or ":".
# The `(?:...)` captures the delimiter without returning it.
_PREFIX_RE = re.compile(
    r"^\s*(.+?)\s*(?:\s+\d|\s+vs\b|\s*[-:]|$)",
    re.IGNORECASE,
)

# Keywords showing that the extracted "prefix" is actually a fighter name
# (= malformed event, with no league name). E.g. "Rousey vs. Carano". We reject
# if the prefix is short (1-2 words) AND no token suggests an organization
# (FC, MMA, Championship, Fight, Pro, Series, Tournament, League, ...).
_ORG_TOKENS = re.compile(
    r"\b(?:FC|MMA|UFC|PFL|ONE|KSW|RIZIN|BKFC|ACA|ACB|WEC|LFA|TUF|EMC|CFP|MFC|"
    r"Championship|Fighting|League|Tournament|Series|Cagefighting|Combat|"
    r"Promotion|Warriors|Bellator|PRIDE|Strikeforce|Invicta|Octagon|Oktagon|"
    r"Cage|Arena|Affliction|DREAM|Shooto|Maktub|Brave|Eagle|Ares|HXMMA|"
    r"Hexagone|DWCS|Contender|Future|Katana|Curitiba|Brazilian|Big\s+Shot)\b",
    re.IGNORECASE,
)


def detect_league_from_event(event_text: str) -> str:
    """
    Dynamically extract the league name from the text of an event.

    Algorithm:
      1. Remove the date suffix ("Saturday, March 7th 2026 ...").
      2. Capture the prefix before the 1st number / "vs" / "-" / ":".
      3. Minimal normalization: everything starting with "UFC" -> "UFC",
         "PFL ..." -> "PFL", "Road to UFC" / "DWCS" / "TUF" -> "UFC".
      4. If the prefix looks like a fighter name (short, without an
         organization token), reject -> "".
      5. Otherwise return the prefix as is (no lookup table,
         no invention: "Ares FC" stays "Ares FC", "Ares" stays "Ares").

    Return "" if no league can be identified.
    """
    if not event_text:
        return ""

    # 1. Strip date suffix
    s = _DATE_SUFFIX_RE.sub("", event_text).strip()
    if not s:
        return ""

    # 2. Extract prefix before number/vs/-/:
    m = _PREFIX_RE.match(s)
    if not m:
        return ""
    prefix = m.group(1).strip(" -:|.,")
    if not prefix:
        return ""

    # 3. Minimal normalizations: if the prefix contains a UFC or PFL keyword,
    # canonicalize to the parent brand (UFC Fight Night, Road to UFC, DWCS, TUF,
    #    PFL Europe, PFL World Tournament... -> "UFC" / "PFL").
    pl = prefix.lower()
    if (
        re.search(r"\bufc\b", pl)
        or re.search(r"\bdwcs\b|\btuf\b", pl)
        or "contender series" in pl
        or "the ultimate fighter" in pl
    ):
        return "UFC"
    if re.search(r"\bpfl\b", pl) or pl == "professional fighters league":
        return "PFL"

    # 4. Reject malformed events (prefix = fighter name)
    # Heuristic: <= 2 words AND no recognized organization token.
    n_words = len(prefix.split())
    if n_words <= 2 and not _ORG_TOKENS.search(prefix):
        return ""

    # 5. Everything else: return the prefix as is
    return prefix


def detect_current_league(row: dict) -> str:
    """
    Determine a fighter's current league from their fight_history.

    Rules (in order):
      1. is_active == "FALSE" -> "" (retired / out of the circuit -> NULL in the database).
      2. empty / unreadable fight_history -> "".
      3. Otherwise -> league of the most recent fight (fights[0].event).
         If not identifiable, leave "" (NEVER guess).

    The most recent fight is enough: even if a fighter went from UFC -> PFL,
    their "current league" is that of their LAST fight. The full history
    stays in fight_history for the record.
    """
    if str(row.get("is_active", "")).strip().upper() == "FALSE":
        return ""

    fh_raw = row.get("fight_history")
    if not fh_raw:
        return ""

    try:
        fh = json.loads(fh_raw) if isinstance(fh_raw, str) else fh_raw
    except (json.JSONDecodeError, TypeError):
        return ""

    if not isinstance(fh, dict):
        return ""

    fights = fh.get("fights") or []
    if not fights:
        return ""

    most_recent = fights[0]
    # The label can be in event, date (FM sometimes maps it the other way round) or raw (Tapology/UG)
    blob = " ".join(str(most_recent.get(k) or "") for k in ("event", "date", "raw"))
    return detect_league_from_event(blob)


# derived computations

def compute_derived(row):
    today = date.today().isoformat()

    # Age recomputed from DOB (overwrites a potentially wrong value)
    if row.get("date_of_birth"):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y"):
            try:
                dob = datetime.strptime(str(row["date_of_birth"]).strip(), fmt).date()
                t = date.today()
                yrs = t.year - dob.year - ((t.month, t.day) < (dob.month, dob.day))
                if 10 < yrs < 90:
                    row["age"] = str(yrs)
                break
            except ValueError:
                continue

    # days_inactive
    if not row.get("days_inactive") and row.get("last_fight_date"):
        row["days_inactive"] = days_since(row["last_fight_date"])

    # is_active: always recompute from days_inactive when available.
    # warning: scrape() sets _fill(is_active, "TRUE") before compute_derived.
    # Without this rework, the "if not row.get('is_active')" guard was always False
    # → Brock Lesnar (3601 jours inactif) restait is_active=TRUE.
    # 1095 days ~ 3 years without fighting = considered inactive/retired.
    try:
        _di = int(str(row.get("days_inactive", "")).strip() or 0)
        if _di > 0:
            row["is_active"] = "FALSE" if _di > 1095 else "TRUE"
        elif not row.get("is_active"):
            row["is_active"] = "TRUE"
    except (ValueError, TypeError):
        if not row.get("is_active"):
            row["is_active"] = "TRUE"

    # current_league: current league/promotion deduced from the most recent fight.
    # NULL if is_active=FALSE (fighter out of the circuit) or if the event matches no
    # known pattern (obscure regional). Never invented: if we cannot identify it,
    # we set "" and the database keeps NULL.
    detected_league = detect_current_league(row)
    if detected_league:
        row["current_league"] = detected_league
    elif str(row.get("is_active", "")).strip().upper() == "FALSE":
        # Forced to empty (= NULL in the database) for inactive fighters: we do not want
        # an old "UFC" value to persist for someone retired 5 years ago.
        row["current_league"] = ""

    # record_other_*
    try:
        tw = int(row.get("record_total_wins",   0) or 0)
        uw = int(row.get("record_ufc_wins",     0) or 0)
        tl = int(row.get("record_total_losses", 0) or 0)
        ul = int(row.get("record_ufc_losses",   0) or 0)
        td = int(row.get("record_total_draws",  0) or 0)
        ud = int(row.get("record_ufc_draws",    0) or 0)
        _fill(row, "record_other_wins",   max(0, tw - uw))
        _fill(row, "record_other_losses", max(0, tl - ul))
        _fill(row, "record_other_draws",  max(0, td - ud))
    except (ValueError, TypeError):
        pass

    # total_fights
    if not row.get("total_fights"):
        try:
            row["total_fights"] = (
                int(row.get("record_total_wins",   0) or 0) +
                int(row.get("record_total_losses", 0) or 0) +
                int(row.get("record_total_draws",  0) or 0)
            )
        except (ValueError, TypeError):
            pass

    # win_percentage_int
    if not row.get("win_percentage_int"):
        try:
            w = int(row.get("record_total_wins", 0) or 0)
            t = int(row.get("total_fights", 0) or 0)
            if t > 0:
                row["win_percentage_int"] = round(w / t * 100)
        except (ValueError, TypeError):
            pass

    # weight_class_origin: if empty, default = current.
    if not row.get("weight_class_origin") and row.get("weight_class_current"):
        row["weight_class_origin"] = row["weight_class_current"]

    # weight_class_origin from fight_history: detects real division changes.
    # E.g. Harrison (85% of fights at Women Featherweight, current UFC = Women's Bantamweight).
    # FM injects the opponent's weight into the opponent field ("Jane Doe #5 Women Featherweight").
    # If the majority (>55%) of the tagged fights are in a division different from the current one,
    # we set it as the origin.
    # Normalization: FM writes "Women Bantamweight" (no apostrophe), we add "'s".
    _strip_apos = lambda s: re.sub(r"['’]", "", s).lower().strip()
    wcc = str(row.get("weight_class_current", "")).strip()
    if row.get("fight_history") and wcc:
        try:
            fh = json.loads(row["fight_history"])
            wc_counts: Counter = Counter()
            for f in fh.get("fights", []):
                opp = f.get("opponent", "")
                wm = re.search(r"(Women[\w'’-]*\s+\w+weight|\b\w+weight)\b", opp, re.I)
                if wm:
                    wc_raw = wm.group(1).strip()
                    # Add "'s" if "Women" has no apostrophe
                    wc_norm = re.sub(r"(?i)^Women\b(?!')", "Women's", wc_raw)
                    wc_norm = " ".join(w.capitalize() for w in wc_norm.split())
                    wc_counts[wc_norm] += 1
            if wc_counts:
                most_common, freq = wc_counts.most_common(1)[0]
                total_tagged = sum(wc_counts.values())
                # Override if the majority of the tagged fights are in a different division
                if freq / total_tagged > 0.55 and _strip_apos(most_common) != _strip_apos(wcc):
                    row["weight_class_origin"] = most_common
        except Exception:
            pass

    # split_decision_wins / split_decision_losses: derived from fight_history.
    # We count the fights whose method/date/event contains "Split".
    # If fight_history is empty or the JSON is corrupted -> leave the values as they are
    # (0 by default on INSERT in the database, or the existing value on update).
    if row.get("fight_history"):
        try:
            fh = json.loads(row["fight_history"])
            fights = fh.get("fights") or []
            sd_w = 0
            sd_l = 0
            for f in fights:
                # The word "Split" can be in method, date (FM sometimes maps it the other way round) or event
                blob = f"{f.get('method','')} {f.get('date','')} {f.get('event','')} {f.get('raw','')}".lower()
                if "split" in blob and "decision" in blob:
                    r = (f.get("result") or "").upper().strip()
                    if r == "W":
                        sd_w += 1
                    elif r == "L":
                        sd_l += 1
            row["split_decision_wins"]   = sd_w
            row["split_decision_losses"] = sd_l
        except (json.JSONDecodeError, ValueError, KeyError, AttributeError):
            pass

    # NOTE: recent_form_score is NOT computed here: it is derived from
    # last_5_results wherever it is needed, so that a single formula is
    # maintained and nothing has to be recomputed after a re-scrape.
    # The value stored in the database is left untouched.
    # (ancien scrape), l'API l'override systematiquement.

    # corrections derivees (gender, streak, champion, age)

    # Gender from weight_class: "Women Flyweight" -> F (overrides the default M)
    wc_lc = str(row.get("weight_class_current", "")).lower()
    if any(w in wc_lc for w in ("women", "female", "feminin")):
        row["gender"] = "F"

    # current_streak fallback from last_5_results.
    # FM only sets current_streak if the active streak is the LONGEST of the
    # career; for most fighters we therefore get nothing, and we keep "0".
    # We derive it from last_5_results (format "X,Y,Z,..."; rightmost = most recent).
    streak_existing = str(row.get("current_streak", "")).strip()
    if not streak_existing or streak_existing == "0":
        last_5 = str(row.get("last_5_results", "")).strip()
        if last_5:
            results = [r.strip().upper() for r in last_5.split(",")
                       if r.strip().upper() in ("W", "L", "D")]
            if results:
                most_recent = results[-1]
                if most_recent in ("W", "L"):
                    count = 1
                    for r in reversed(results[:-1]):
                        if r == most_recent:
                            count += 1
                        else:
                            break
                    row["current_streak"] = f"{count}{most_recent}"

    # is_ufc_champion: if rank=0 (champion convention) but champion not detected,
    # force TRUE. Safeguard against UFC-FR pages where the "belt" badge is missing.
    if str(row.get("ufc_official_rank", "")).strip() == "0":
        row["is_ufc_champion"] = "TRUE"

    # ufc_title_match_win: if champion but value 0/empty -> force to 1 minimum.
    # Logic: a champion necessarily won AT LEAST the fight in which they took the belt.
    # Safeguard for scrapes WITHOUT an FightMatrix URL (the only source that parses
    # "Title Bouts: X-Y-Z"). E.g. Joshua Van scraped with Tapology+UFC-FR only ->
    # match_win = 0 by default -> the safeguard sets it to 1 (he IS champion).
    # The real count (win + defenses) will be retrieved on the next scrape with FM.
    if str(row.get("is_ufc_champion", "")).upper() == "TRUE":
        try:
            current_mw = int(str(row.get("ufc_title_match_win", "")).strip() or 0)
            if current_mw < 1:
                row["ufc_title_match_win"] = "1"
        except (ValueError, TypeError):
            row["ufc_title_match_win"] = "1"

    # Age: if the age is absurd (<15 or >90), clean it. Avoids age=1 when the scraper
    # captures a stray digit and DOB is empty (the database trigger does not recompute
    # the age without a DOB, so a bogus value would persist).
    try:
        age_val = int(str(row.get("age", "")).strip() or 0)
        if age_val and (age_val < 15 or age_val > 90):
            row["age"] = ""
    except (ValueError, TypeError):
        pass

    # timestamps
    _fill(row, "created_at", today)
    row["updated_at"] = today
    _fill(row, "last_scraped_at", today)
    _fill(row, "is_verified", "FALSE")
    # is_ufc_champion: do not set "FALSE" by default if we have no rank.
    # Reasoning: if we did not scrape UFC-FR/FM, we do not know whether the fighter
    # is champion -> leave empty -> database DEFAULT FALSE on INSERT, no overwriting
    # of an existing TRUE on UPDATE (ALWAYS_REFRESH + default="FALSE" = champion bug).
    # We set FALSE ONLY if there is an explicit rank > 0 (positive proof: ranked but not #C).
    _rank_cd = str(row.get("ufc_official_rank", "")).strip()
    if not row.get("is_ufc_champion") and _rank_cd and _rank_cd != "0":
        row["is_ufc_champion"] = "FALSE"
    _fill(row, "ufc_title_match_win", "0")

    # If we have a nationality but no country code, try the lookup
    if row.get("nationality") and not row.get("country_code"):
        cc, flag = lookup_country(row["nationality"])
        if cc:
            row["country_code"] = cc
            row["flag_emoji"]   = flag

    # data_quality_score
    if not row.get("data_quality_score"):
        score = 30
        for f in ["date_of_birth", "height_inches", "reach_inches", "nationality"]:
            if row.get(f): score += 10
        for f in ["wins_by_ko_tko", "finish_rate_int", "fightmatrix_rating_points", "last_5_results"]:
            if row.get(f): score += 5
        row["data_quality_score"] = min(score, 95)

# processing of ONE row

def process_row(row):
    misc = str(row.get("misc", "")).strip()
    if not misc:
        return row

    blocks = [b.strip() for b in misc.split("///") if b.strip()]
    sources_found = []

    for block in blocks:
        src = detect_source(block)
        sources_found.append(src)
        if src == "fightmatrix":
            parse_fightmatrix(block, row)
        elif src == "tapology":
            parse_tapology(block, row)
        elif src == "underground":
            parse_underground(block, row)
        else:
            # Try everything
            parse_fightmatrix(block, row)
            parse_tapology(block, row)
            parse_underground(block, row)

    compute_derived(row)

    # Make sure all the columns exist
    for col in COLUMNS:
        row.setdefault(col, "")

    return row

#  MAIN

def main():
    if len(sys.argv) > 1:
        src_path = Path(sys.argv[1])
        if not src_path.is_absolute():
            src_path = ROOT / src_path
    else:
        src_path = ROOT / "tables flyweights - Feuille 1.csv"

    if not src_path.exists():
        print(f"File not found: {src_path}")
        sys.exit(1)

    print(f"\nReading: {src_path.name}")

    # The CSV has 2 header rows: row1=categories, row2=real column names
    with open(src_path, encoding="utf-8-sig", newline="") as f:
        raw_reader = list(csv.reader(f))

    cat_header = raw_reader[0] if raw_reader else []
    fieldnames = raw_reader[1] if len(raw_reader) > 1 else []
    raw_rows   = [dict(zip(fieldnames, r + ['']*(len(fieldnames)-len(r))))
                  for r in raw_reader[2:]]

    # Make sure all the final columns are present
    for col in COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)

    processed = []
    filled_count = 0

    for i, row in enumerate(raw_rows):
        has_misc = bool(str(row.get("misc", "")).strip())
        result = process_row(dict(row))
        processed.append(result)
        if has_misc:
            filled_count += 1
            name = result.get("name", f"row {i+2}")
            print(f"  ✓ {name} → dob={result.get('date_of_birth','-')} | "
                  f"record={result.get('record_total_wins','-')}-{result.get('record_total_losses','-')} | "
                  f"ko={result.get('wins_by_ko_tko','-')} sub={result.get('wins_by_submission','-')} dec={result.get('wins_by_decision','-')} | "
                  f"fm_pts={result.get('fightmatrix_rating_points','-')} | "
                  f"quality={result.get('fightmatrix_quality_perf_pct','-')}%")

    # Rewrite the source file with the double header intact
    with open(src_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(cat_header)
        writer.writerow(fieldnames)
        for d in processed:
            writer.writerow([d.get(c, '') for c in fieldnames])

    print(f"\n  → {filled_count} fighters processed | {src_path.name} updated")

    # Merge into final tables - fighters.csv
    if FINAL_CSV.exists():
        with open(FINAL_CSV, encoding="utf-8-sig", newline="") as f:
            final_raw = list(csv.reader(f))
        final_cat  = final_raw[0] if final_raw else []
        final_cols = final_raw[1] if len(final_raw) > 1 else COLUMNS
        final_rows = [dict(zip(final_cols, r + ['']*(len(final_cols)-len(r))))
                      for r in final_raw[2:]]

        existing_names = {r.get("name","").lower().strip() for r in final_rows if r.get("name","").strip()}
        ids = [int(r.get("id","0") or 0) for r in final_rows if str(r.get("id","")).isdigit()]
        next_id = (max(ids) + 1) if ids else 1
        added = 0

        for row in processed:
            name = row.get("name","").lower().strip()
            if name and name not in existing_names and str(row.get("misc","")).strip():
                row["id"] = next_id
                next_id += 1
                final_rows.append(row)
                existing_names.add(name)
                added += 1

        if added:
            with open(FINAL_CSV, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(final_cat)
                writer.writerow(final_cols)
                for d in final_rows:
                    writer.writerow([d.get(c,'') for c in final_cols])
            print(f"  -> {added} new fighters added in {FINAL_CSV.name}")
        else:
            print(f"  -> Nothing to merge (already present or empty misc)")
    else:
        print(f"  /!\\ {FINAL_CSV.name} not found")

    print("\nDone.")

if __name__ == "__main__":
    main()
