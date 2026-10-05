"""Name lists and value variants used to build benchmark cases.

Variants come in two pools. ``dev`` variants appear in development cases
and may be used to build rule dictionaries. ``heldout`` variants appear
only in held-out cases, so they measure how a system copes with spellings
nobody listed in advance. The two pools never share a value.
"""

from __future__ import annotations

# (Latin, Arabic) pairs.
FIRST_NAMES = [
    ("Mohammed", "محمد"), ("Ahmed", "أحمد"), ("Abdullah", "عبدالله"),
    ("Khalid", "خالد"), ("Faisal", "فيصل"), ("Omar", "عمر"), ("Saud", "سعود"),
    ("Turki", "تركي"), ("Fahad", "فهد"), ("Yousef", "يوسف"), ("Ali", "علي"),
    ("Hassan", "حسن"), ("Nasser", "ناصر"), ("Majed", "ماجد"), ("Sultan", "سلطان"),
    ("Fatimah", "فاطمة"), ("Noura", "نورة"), ("Sara", "سارة"), ("Reem", "ريم"),
    ("Lama", "لمى"), ("Haya", "هيا"), ("Maha", "مها"), ("Abeer", "عبير"),
    ("Hind", "هند"), ("Lina", "لينا"), ("Ghada", "غادة"), ("Rana", "رنا"),
    ("Aisha", "عائشة"), ("Mariam", "مريم"), ("Jana", "جنى"),
]

LAST_NAMES = [
    ("Alharbi", "الحربي"), ("Alghamdi", "الغامدي"), ("Alzahrani", "الزهراني"),
    ("Alotaibi", "العتيبي"), ("Alqahtani", "القحطاني"), ("Alshehri", "الشهري"),
    ("Aldosari", "الدوسري"), ("Almutairi", "المطيري"), ("Alanazi", "العنزي"),
    ("Alshammari", "الشمري"), ("Aljuhani", "الجهني"), ("Albishri", "البشري"),
    ("Alomari", "العمري"), ("Alsulami", "السلمي"), ("Hamdan", "حمدان"),
    ("Khan", "خان"), ("Sharma", "شارما"), ("Smith", "سميث"), ("Haddad", "حداد"),
    ("Nasr", "نصر"), ("Farouk", "فاروق"),
]

COMPANIES = [
    ("Al Noor Trading", "شركة النور للتجارة"),
    ("Red Sea Logistics", "البحر الأحمر للخدمات اللوجستية"),
    ("Najd Contracting", "نجد للمقاولات"),
    ("Gulf Star Foods", "نجمة الخليج للأغذية"),
    ("Hejaz Medical Supplies", "الحجاز للمستلزمات الطبية"),
    ("Tihama Electronics", "تهامة للإلكترونيات"),
    ("Desert Rose Retail", "وردة الصحراء للتجزئة"),
    ("Oasis Water", "الواحة للمياه"),
    ("Summit Engineering", "القمة للهندسة"),
    ("Palm Valley Farms", "وادي النخيل للمزارع"),
    ("Blue Horizon Travel", "الأفق الأزرق للسفر"),
    ("Crescent Auto Parts", "الهلال لقطع الغيار"),
    ("Falcon Security", "الصقر للحراسات"),
    ("Sands Real Estate", "الرمال للعقارات"),
    ("Corniche Hospitality", "الكورنيش للضيافة"),
]

EMAIL_DOMAINS = ["gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"]

# Country weights for generated customers.
COUNTRY_WEIGHTS = {
    "SA": 55, "AE": 10, "KW": 5, "BH": 4, "QA": 4, "OM": 4,
    "EG": 6, "JO": 4, "US": 2, "GB": 2, "IN": 2, "PK": 2,
}

# Mobile number shapes: (prefix, number of random digits after it).
PHONE_SHAPES = {
    "SA": ("+9665", 8), "AE": ("+9715", 8), "KW": ("+9656", 7), "BH": ("+9733", 7),
    "QA": ("+9745", 7), "OM": ("+9689", 7), "EG": ("+2010", 8), "JO": ("+9627", 8),
    "US": ("+1415", 7), "GB": ("+447", 9), "IN": ("+919", 9), "PK": ("+923", 9),
}

CURRENCY_WEIGHTS = {
    "SAR": 60, "AED": 10, "USD": 10, "KWD": 4, "BHD": 4, "QAR": 4, "OMR": 4, "EUR": 4,
}
STATUS_WEIGHTS = {"PENDING": 10, "SHIPPED": 15, "DELIVERED": 60, "CANCELLED": 8, "RETURNED": 7}
CHANNEL_WEIGHTS = {"ONLINE": 45, "STORE": 35, "PHONE": 10, "PARTNER": 10}

MONTH_ABBREVIATIONS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

ARABIC_INDIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

# kind -> canonical value -> {"dev": [...], "heldout": [...]}
VARIANTS: dict[str, dict[str, dict[str, list[str]]]] = {
    "country_code": {
        "SA": {"dev": ["KSA", "Saudi Arabia", "Saudi", "السعودية"],
               "heldout": ["Kingdom of Saudi Arabia", "Saudia", "المملكة العربية السعودية", "K.S.A"]},
        "AE": {"dev": ["UAE", "United Arab Emirates", "الإمارات"],
               "heldout": ["U.A.E.", "Emirates"]},
        "KW": {"dev": ["Kuwait", "الكويت"], "heldout": ["State of Kuwait"]},
        "BH": {"dev": ["Bahrain", "البحرين"], "heldout": ["Kingdom of Bahrain"]},
        "QA": {"dev": ["Qatar", "قطر"], "heldout": ["State of Qatar"]},
        "OM": {"dev": ["Oman", "عمان"], "heldout": ["Sultanate of Oman"]},
        "EG": {"dev": ["Egypt", "مصر"], "heldout": ["Arab Republic of Egypt"]},
        "JO": {"dev": ["Jordan", "الأردن"], "heldout": ["Hashemite Kingdom of Jordan"]},
        "US": {"dev": ["USA", "United States"], "heldout": ["U.S.", "America"]},
        "GB": {"dev": ["UK", "United Kingdom"], "heldout": ["Great Britain", "Britain"]},
        "IN": {"dev": ["India", "الهند"], "heldout": ["Bharat"]},
        "PK": {"dev": ["Pakistan", "باكستان"], "heldout": ["Islamic Republic of Pakistan"]},
    },
    "city": {
        "Jeddah": {"dev": ["Jedda", "JED", "جدة"], "heldout": ["Jiddah", "Juddah"]},
        "Riyadh": {"dev": ["RUH", "الرياض", "Riyad"], "heldout": ["Ar Riyadh"]},
        "Makkah": {"dev": ["Mecca", "مكة"], "heldout": ["Makkah Al Mukarramah"]},
        "Madinah": {"dev": ["Medina", "المدينة"], "heldout": ["Al Madinah Al Munawwarah"]},
        "Dammam": {"dev": ["DMM", "الدمام"], "heldout": ["Ad Dammam"]},
        "Khobar": {"dev": ["Al Khobar", "الخبر"], "heldout": ["Al-Khubar"]},
        "Taif": {"dev": ["الطائف"], "heldout": ["At Taif"]},
        "Abha": {"dev": ["أبها"], "heldout": ["Abhaa"]},
        "Tabuk": {"dev": ["تبوك"], "heldout": ["Tabouk"]},
        "Dubai": {"dev": ["دبي", "DXB"], "heldout": ["Dubayy"]},
        "Abu Dhabi": {"dev": ["أبوظبي", "AUH"], "heldout": ["Abu Zabi"]},
        "Sharjah": {"dev": ["الشارقة"], "heldout": ["Al Sharjah"]},
        "Kuwait City": {"dev": ["Kuwait"], "heldout": ["Al Kuwait"]},
        "Manama": {"dev": ["المنامة"], "heldout": ["Al Manamah"]},
        "Doha": {"dev": ["الدوحة"], "heldout": ["Ad Dawhah"]},
        "Muscat": {"dev": ["مسقط"], "heldout": ["Masqat"]},
        "Cairo": {"dev": ["القاهرة"], "heldout": ["Al Qahirah"]},
        "Alexandria": {"dev": ["الإسكندرية"], "heldout": ["Iskandariya"]},
        "Amman": {"dev": ["عمّان"], "heldout": ["Amman City"]},
        "New York": {"dev": ["NYC", "New York City"], "heldout": ["NY"]},
        "London": {"dev": ["لندن"], "heldout": ["Greater London"]},
        "Mumbai": {"dev": ["Bombay"], "heldout": ["Mumbai City"]},
        "Karachi": {"dev": ["كراتشي"], "heldout": ["Karachi City"]},
    },
    "customer_type": {
        "INDIVIDUAL": {"dev": ["Individual", "Person", "فرد", "B2C"],
                       "heldout": ["Retail customer", "Private"]},
        "BUSINESS": {"dev": ["Business", "Company", "شركة", "B2B"],
                     "heldout": ["Corporate", "Enterprise account"]},
    },
    "boolean": {
        "true": {"dev": ["Y", "Yes", "Active", "1", "نعم"],
                 "heldout": ["T", "TRUE", "Enabled", "فعال"]},
        "false": {"dev": ["N", "No", "Inactive", "0", "لا"],
                  "heldout": ["F", "FALSE", "Disabled", "غير فعال"]},
    },
    "status": {
        "PENDING": {"dev": ["pending", "Awaiting shipment", "قيد الانتظار"],
                    "heldout": ["Open - not yet processed", "On hold"]},
        "SHIPPED": {"dev": ["shipped", "Dispatched", "تم الشحن"],
                    "heldout": ["In transit", "Out for delivery"]},
        "DELIVERED": {"dev": ["delivered", "Delivered to client", "تم التسليم"],
                      "heldout": ["Received by customer", "Completed"]},
        "CANCELLED": {"dev": ["cancelled", "Canceled", "ملغي"],
                      "heldout": ["Voided", "Order withdrawn"]},
        "RETURNED": {"dev": ["returned", "Returned - damaged", "مرتجع"],
                     "heldout": ["RMA processed", "Refunded after return"]},
    },
    "channel": {
        "ONLINE": {"dev": ["Web", "E-commerce", "online"], "heldout": ["Mobile app", "Website order"]},
        "STORE": {"dev": ["In-store", "Branch", "POS"], "heldout": ["Showroom", "Walk-in"]},
        "PHONE": {"dev": ["Call center", "Telephone"], "heldout": ["Hotline"]},
        "PARTNER": {"dev": ["Reseller", "Partner"], "heldout": ["Distributor", "Marketplace"]},
    },
    "currency": {
        "SAR": {"dev": ["SR", "ريال", "Riyal"], "heldout": ["S.R.", "ر.س"]},
        "AED": {"dev": ["Dhs", "Dirham"], "heldout": ["د.إ", "UAE Dirham"]},
        "USD": {"dev": ["$", "US$"], "heldout": ["USD$", "Dollar"]},
        "EUR": {"dev": ["€", "Euro"], "heldout": ["EURO", "eur."]},
        "KWD": {"dev": ["KD"], "heldout": ["د.ك"]},
        "BHD": {"dev": ["BD"], "heldout": ["د.ب"]},
        "QAR": {"dev": ["QR"], "heldout": ["ر.ق"]},
        "OMR": {"dev": ["RO"], "heldout": ["ر.ع"]},
    },
}

# Tokens that stand in for a missing value.
DISGUISED_MISSING = {
    "dev": ["N/A", "-", "unknown", "لا يوجد", "null"],
    "heldout": ["none", "?", "TBD", "غير متوفر", "n.a."],
}


def variants(kind: str, canonical: str, pool: str) -> list[str]:
    return VARIANTS[kind].get(canonical, {}).get(pool, [])
