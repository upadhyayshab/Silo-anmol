"""add 35 crops to the scoring engine (data only)

Agro's crop list is the picker for both features; 35 crops on it had no
scores, so the Crop Guide could never recommend them. Each gets a crops row
and its zone (15), season (4), water (4) and soil (12) scores, built with the
same method as the original 104 ('CR V2 - Scoring_Reference_v4'): zones score
2 where the crop is a main crop, else 0, and season/water/soil follow the
same patterns. Values are an AI research draft (2026-10-18; sources per crop
in docs/2026-10-18_CROP_GUIDE_NEW_CROPS_RESEARCH.md) and need agronomist
review. Info pages for these crops are in app/data/crops.json.

Skips a crop whose name_en already exists, so it is safe to rerun. It reads
the table first, so it can't run in --sql (offline) mode; check counts
instead: crops goes from 104 to 139, crop_zone_scores from 1560 to 2085.

Revision ID: i1a2b3c4d5e6
Revises: h1a2b3c4d5e6
Create Date: 2026-10-18 00:00:00.000000
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from config import get_settings

revision = "i1a2b3c4d5e6"
down_revision = "h1a2b3c4d5e6"
branch_labels = None
depends_on = None

SCHEMA = get_settings().name


def _enum(name, *values):
    return postgresql.ENUM(*values, name=name, schema=SCHEMA, create_type=False)


CROPS_TABLE = sa.table(
    "crops",
    sa.column("uid", sa.String), sa.column("code", sa.Integer), sa.column("name_en", sa.String),
    *(sa.column(c, sa.String) for c in ("name_hn", "name_kn", "name_ta", "name_te", "name_ml", "name_mr",
                                         "name_gu", "name_bn", "name_pa", "name_or", "name_as")),
    sa.column("family", sa.String),
    sa.column("horizon", _enum("horizon_enum", "SEASONAL", "ANNUAL", "PERENNIAL")),
    sa.column("water_need", _enum("water_need_enum", "LOW", "MEDIUM", "HIGH")),
    sa.column("cost_class", _enum("cost_class_enum", "LOW", "MEDIUM", "HIGH")),
    sa.column("main_zones_mask", sa.SmallInteger), sa.column("note", sa.String),
    schema=SCHEMA,
)
ZONE_TABLE = sa.table("crop_zone_scores", sa.column("uid", sa.String), sa.column("crop_uid", sa.String),
                      sa.column("zone", sa.Integer), sa.column("score", sa.Integer), schema=SCHEMA)
SEASON_TABLE = sa.table("crop_season_scores", sa.column("uid", sa.String), sa.column("crop_uid", sa.String),
                        sa.column("season", _enum("season_enum", "KHARIF", "RABI", "ZAID", "LONGTERM")),
                        sa.column("score", sa.Integer), schema=SCHEMA)
WATER_TABLE = sa.table("crop_water_scores", sa.column("uid", sa.String), sa.column("crop_uid", sa.String),
                       sa.column("method", _enum("water_method_enum", "RAINFED", "SURFACE", "DRIP", "SPRINKLER")),
                       sa.column("score", sa.Integer), schema=SCHEMA)
SOIL_TABLE = sa.table("crop_soil_scores", sa.column("uid", sa.String), sa.column("crop_uid", sa.String),
                      sa.column("soil_class", sa.Integer), sa.column("score", sa.Integer), schema=SCHEMA)

# Frozen research data. zones: main agro-climatic zones (score 2, others 0);
# soil: scores for USDA texture classes 1..12.
CROPS = [
    {"code": 105, "name_en": "Oats", "family": "Fodder", "horizon": "SEASONAL", "water_need": "LOW", "cost_class": "LOW", "zones": [4, 5, 6, 8], "note": "Winter fodder; multi-cut", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122222012", "names": {"name_hn": "जई", "name_kn": "ಓಟ್ಸ್", "name_ta": "ஓட்ஸ்", "name_te": "ఓట్స్", "name_ml": "ഓട്സ്", "name_mr": "ओट्स", "name_gu": "ઓટ્સ", "name_bn": "ওটস", "name_pa": "ਜਵੀ", "name_or": "ଓଟ୍ସ", "name_as": "অ’টচ"}},
    {"code": 106, "name_en": "Rye", "family": "Cereal", "horizon": "SEASONAL", "water_need": "LOW", "cost_class": "LOW", "zones": [1, 6], "note": "Minor winter cereal", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122222012", "names": {"name_hn": "रई अनाज", "name_kn": "ರೈ ಧಾನ್ಯ", "name_ta": "ரை தானியம்", "name_te": "రై", "name_ml": "റൈ", "name_mr": "रई धान्य", "name_gu": "રાય અનાજ", "name_bn": "রাই শস্য", "name_pa": "ਰਾਈ ਅਨਾਜ", "name_or": "ରାଇ ଶସ୍ୟ", "name_as": "ৰাই শস্য"}},
    {"code": 107, "name_en": "Broad bean", "family": "Pulse", "horizon": "SEASONAL", "water_need": "LOW", "cost_class": "LOW", "zones": [2, 4, 5, 8], "note": "Rabi bean; Bihar and UP", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122222112", "names": {"name_hn": "बाकला", "name_kn": "ಅವರೆಕಾಯಿ", "name_ta": "அவரைக்காய்", "name_te": "బ్రాడ్ బీన్", "name_ml": "ബ്രോഡ് ബീൻ", "name_mr": "ब्रॉड बीन", "name_gu": "પહોળા બીન", "name_bn": "চওড়া শিম", "name_pa": "ਬਾਕਲਾ", "name_or": "ବ୍ରଡ୍ ବିନ୍", "name_as": "উৰহী"}},
    {"code": 108, "name_en": "Sugar beet", "family": "Commercial", "horizon": "SEASONAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [5, 6], "note": "Needs a sugar mill nearby", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122222012", "names": {"name_hn": "चुकंदर (शुगर बीट)", "name_kn": "ಸಕ್ಕರೆ ಬೀಟ್", "name_ta": "சர்க்கரை பீட்", "name_te": "చక్కెర బీట్", "name_ml": "പഞ്ചസാര ബീറ്റ്", "name_mr": "साखर बीट", "name_gu": "સુગર બીટ", "name_bn": "সুগার বিট", "name_pa": "ਚੁਕੰਦਰ", "name_or": "ସୁଗାର ବିଟ୍", "name_as": "চেনি বিট"}},
    {"code": 109, "name_en": "Chicory", "family": "Commercial", "horizon": "SEASONAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [5, 6, 13], "note": "Contract crop for coffee blends", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "कासनी", "name_kn": "ಚಿಕೋರಿ", "name_ta": "சிக்கரி", "name_te": "షికోరి", "name_ml": "ചിക്കറി", "name_mr": "चिकोरी", "name_gu": "ચિકોરી", "name_bn": "চিকোরি", "name_pa": "ਕਾਸਨੀ", "name_or": "ଚିକୋରି", "name_as": "চিকোৰী"}},
    {"code": 110, "name_en": "Mint", "family": "Commercial", "horizon": "SEASONAL", "water_need": "HIGH", "cost_class": "MEDIUM", "zones": [4, 5, 6], "note": "Menthol oil; UP leads", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 0, "ZAID": 2}, "water": {"DRIP": 2, "RAINFED": 0, "SPRINKLER": 1, "SURFACE": 2}, "soil": "112122222012", "names": {"name_hn": "पुदीना", "name_kn": "ಪುದೀನಾ", "name_ta": "புதினா", "name_te": "పుదీనా", "name_ml": "പുതിന", "name_mr": "पुदिना", "name_gu": "ફુદીનો", "name_bn": "পুদিনা", "name_pa": "ਪੁਦੀਨਾ", "name_or": "ମିଣ୍ଟ", "name_as": "পদিনা"}},
    {"code": 111, "name_en": "Indigo", "family": "Commercial", "horizon": "SEASONAL", "water_need": "LOW", "cost_class": "LOW", "zones": [10, 11], "note": "Natural dye; buyer tie-up needed", "season": {"KHARIF": 2, "LONGTERM": 0, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "001121122222", "names": {"name_hn": "नील", "name_kn": "ನೀಲಿ ಗಿಡ", "name_ta": "இண்டிகோ", "name_te": "ఇండిగో", "name_ml": "ഇൻഡിഗോ", "name_mr": "इंडिगो", "name_gu": "ઈન્ડિગો", "name_bn": "নীল", "name_pa": "ਨੀਲ", "name_or": "ଇଣ୍ଡିଗୋ", "name_as": "নীল"}},
    {"code": 112, "name_en": "Broccoli", "family": "Vegetable", "horizon": "SEASONAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [1, 2, 5, 6], "note": "Cool season; city markets", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122222012", "names": {"name_hn": "ब्रोकोली", "name_kn": "ಬ್ರೊಕೋಲಿ", "name_ta": "ப்ரோக்கோலி", "name_te": "బ్రోకలీ", "name_ml": "ബ്രോക്കോളി", "name_mr": "ब्रोकोली", "name_gu": "બ્રોકોલી", "name_bn": "ব্রকলি", "name_pa": "ਬਰੋਕਲੀ", "name_or": "ବ୍ରୋକୋଲି", "name_as": "ব্ৰকলি"}},
    {"code": 113, "name_en": "Lettuce", "family": "Vegetable", "horizon": "SEASONAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [1, 5, 6, 9, 10], "note": "Short salad crop; city markets", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122222012", "names": {"name_hn": "सलाद", "name_kn": "ಲೆಟಿಸ್", "name_ta": "கீரை", "name_te": "లెట్యూస్", "name_ml": "ലെറ്റസ്", "name_mr": "लेट्यूस", "name_gu": "લેટીસ", "name_bn": "লেটুস", "name_pa": "ਸਲਾਦ ਪੱਤਾ", "name_or": "ଲେଟୁସ୍", "name_as": "লেটুচ শাক"}},
    {"code": 114, "name_en": "Celery", "family": "Spice", "horizon": "SEASONAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [5, 6], "note": "Seed spice; Punjab leads", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122222012", "names": {"name_hn": "अजमोदा", "name_kn": "ಸೆಲರಿ", "name_ta": "செலரி", "name_te": "సెలెరీ", "name_ml": "സെലറി", "name_mr": "सेलेरी", "name_gu": "સેલરી", "name_bn": "সেলারি", "name_pa": "ਸੈਲਰੀ", "name_or": "ସେଲେରି", "name_as": "চেলেৰী"}},
    {"code": 115, "name_en": "Asparagus", "family": "Vegetable", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [1, 10], "note": "Perennial; hotel and export market", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "शतावरी", "name_kn": "ಶತಾವರಿ", "name_ta": "அஸ்பாரகஸ்", "name_te": "ఆస్పరాగస్", "name_ml": "ശതാവരിച്ചെടി", "name_mr": "शतावरी", "name_gu": "શતાવરીનો છોડ", "name_bn": "অ্যাসপারাগাস", "name_pa": "ਸ਼ਤਾਵਰੀ", "name_or": "ଅସପାରାଗସ୍", "name_as": "শতমূলী"}},
    {"code": 116, "name_en": "Artichoke", "family": "Vegetable", "horizon": "SEASONAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [1, 10], "note": "Niche; hotel market", "season": {"KHARIF": 0, "LONGTERM": 0, "RABI": 2, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122222012", "names": {"name_hn": "हाथी चक", "name_kn": "ಅರ್ಟಿಚೋಕ್", "name_ta": "கூனைப்பூ", "name_te": "ఆర్టిచోక్", "name_ml": "ആർട്ടികോക്ക്", "name_mr": "आर्टिचोक", "name_gu": "કાંટાળી ખાદ્ય વનસ્પતિ", "name_bn": "আর্টিকোক", "name_pa": "ਹਾਥੀ ਚੱਕ", "name_or": "ଆର୍ଟିଚୋକ୍", "name_as": "আৰ্টিচোক"}},
    {"code": 117, "name_en": "Yam", "family": "Vegetable", "horizon": "ANNUAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [2, 7, 11, 12], "note": "Tuber; Kerala, Odisha, NE", "season": {"KHARIF": 2, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "याम", "name_kn": "ತೂಗು ಗೆಣಸು", "name_ta": "யாம்", "name_te": "యమ్", "name_ml": "ചേന", "name_mr": "याम", "name_gu": "રતાળુ", "name_bn": "ইয়াম", "name_pa": "ਰਤਾਲੂ", "name_or": "ଯମ୍", "name_as": "কাঠ আলু"}},
    {"code": 118, "name_en": "Apple", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [1, 2], "note": "Hill orchard staple", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "सेब", "name_kn": "ಸೇಬು", "name_ta": "ஆப்பிள்", "name_te": "ఆపిల్", "name_ml": "ആപ്പിൾ", "name_mr": "सफरचंद", "name_gu": "સફરજન", "name_bn": "আপেল", "name_pa": "ਸੇਬ", "name_or": "ଆପଲ୍", "name_as": "আপেল"}},
    {"code": 119, "name_en": "Pear", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [1, 5, 6], "note": "Hills; Patharnakh in plains", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "नाशपाती", "name_kn": "ಮರಸೇಬು", "name_ta": "பேரிக்காய்", "name_te": "పియర్", "name_ml": "പിയർ", "name_mr": "नाशपाती", "name_gu": "નાસપતી", "name_bn": "নাশপাতি", "name_pa": "ਨਾਖ", "name_or": "ନାସପାତି", "name_as": "নাশপতি"}},
    {"code": 120, "name_en": "Peach", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [1, 5, 6], "note": "Hills; low-chill types in Punjab", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "आड़ू", "name_kn": "ಪೀಚ್ ಹಣ್ಣು", "name_ta": "பீச்", "name_te": "పీచ్", "name_ml": "പീച്ച്", "name_mr": "पीच", "name_gu": "પીચ", "name_bn": "পীচ", "name_pa": "ਆੜੂ", "name_or": "ପିଚ୍", "name_as": "নৰা বগৰী"}},
    {"code": 121, "name_en": "Plum", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [1, 6], "note": "Hills; Satluj Purple in plains", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "आलूबुखारा", "name_kn": "ಪ್ಲಮ್ ಹಣ್ಣು", "name_ta": "பிளம்", "name_te": "ప్లం", "name_ml": "പ്ലം", "name_mr": "मनुका", "name_gu": "આલુ", "name_bn": "আলুবোখারা", "name_pa": "ਆਲੂਬੁਖਾਰਾ", "name_or": "ପ୍ଲମ୍", "name_as": "আলুবখৰা"}},
    {"code": 122, "name_en": "Apricot", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "LOW", "cost_class": "MEDIUM", "zones": [1], "note": "Cold-dry hills (Ladakh, Kinnaur)", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "खुबानी", "name_kn": "ಜಲ್ದಾರು ಹಣ್ಣು", "name_ta": "பாதாமி", "name_te": "నేరేడు పండు", "name_ml": "ആപ്രിക്കോട്ട്", "name_mr": "जर्दाळू", "name_gu": "જરદાળુ", "name_bn": "এপ্রিকট", "name_pa": "ਖੁਰਮਾਨੀ", "name_or": "ଆପ୍ରିକୋଟ", "name_as": "এপ্ৰিকট"}},
    {"code": 123, "name_en": "Cherry", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [1], "note": "High hills only (Kashmir, Shimla)", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "चेरी", "name_kn": "ಚೆರ್ರಿ", "name_ta": "செர்ரி", "name_te": "చెర్రీ", "name_ml": "ചെറി", "name_mr": "चेरी", "name_gu": "ચેરી", "name_bn": "চেরি", "name_pa": "ਚੈਰੀ", "name_or": "ଚେରି", "name_as": "চেৰী"}},
    {"code": 124, "name_en": "Almond", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [1], "note": "Kashmir dry-fruit tree", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "बादाम", "name_kn": "ಬಾದಾಮಿ", "name_ta": "பாதாம்", "name_te": "బాదం", "name_ml": "ബദാം", "name_mr": "बदाम", "name_gu": "બદામ", "name_bn": "কাঠবাদাম", "name_pa": "ਬਦਾਮ", "name_or": "ବାଦାମ", "name_as": "কাঠবাদাম"}},
    {"code": 125, "name_en": "Walnut", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "LOW", "cost_class": "MEDIUM", "zones": [1, 2], "note": "Hill dry-fruit tree", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "अखरोट", "name_kn": "ಆಕ್ರೋಟ್", "name_ta": "வால்நட்", "name_te": "వాల్నట్", "name_ml": "വാൽനട്ട്", "name_mr": "अक्रोड", "name_gu": "અખરોટ", "name_bn": "আখরোট", "name_pa": "ਅਖਰੋਟ", "name_or": "ଅଖରୋଟ", "name_as": "আখৰোট"}},
    {"code": 126, "name_en": "Hazelnut", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [1], "note": "Very little grown in India", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "हेज़लनट", "name_kn": "ಹೇಜೆಲ್ ನಟ್", "name_ta": "ஹேசல்நட்", "name_te": "హాజెల్ నట్", "name_ml": "ഹാസൽനട്ട്", "name_mr": "हेझलनट", "name_gu": "હેઝલનટ", "name_bn": "হেজেলনাট", "name_pa": "ਪਹਾੜੀ ਬਦਾਮ", "name_or": "ହାଜେଲନଟ୍", "name_as": "হেজেলনাট"}},
    {"code": 127, "name_en": "Chestnut", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "LOW", "cost_class": "MEDIUM", "zones": [1, 2], "note": "Minor hill nut", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "शाहबलूत", "name_kn": "ಚೆಸ್ಟ್ ನಟ್", "name_ta": "கஷ்கொட்டை", "name_te": "చెస్ట్‌నట్", "name_ml": "ചെസ്റ്റ്നട്ട്", "name_mr": "चेस्टनट", "name_gu": "ચેસ્ટનટ", "name_bn": "চেস্টনাট", "name_pa": "ਸ਼ਾਹਬਲੂਤ", "name_or": "ଚେଷ୍ଟନଟ୍", "name_as": "চেষ্টনাট"}},
    {"code": 128, "name_en": "Blackberry", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [1, 2], "note": "Minor hill berry", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "ब्लैकबेरी", "name_kn": "ಬ್ಲ್ಯಾಕ್ ಬೆರ್ರಿ", "name_ta": "கருப்பட்டி", "name_te": "బ్లాక్బెర్రీస్", "name_ml": "ബ്ലാക്ക്ബെറികൾ", "name_mr": "ब्लॅकबेरी", "name_gu": "બ્લેકબેરી", "name_bn": "ব্ল্যাকবেরি", "name_pa": "ਜਾਮਣ", "name_or": "ବ୍ଲାକବେରୀ", "name_as": "ব্লেকবেৰী"}},
    {"code": 129, "name_en": "Blueberry", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [1, 9, 10], "note": "Needs acidic soil; mostly under tunnels", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "ब्लूबेरी", "name_kn": "ಬ್ಲೂ ಬೆರ್ರಿ", "name_ta": "புளுபெர்ரி", "name_te": "బ్లూబెర్రీ", "name_ml": "ഞാവൽപഴം", "name_mr": "ब्लूबेरी", "name_gu": "બ્લુબેરી", "name_bn": "ব্লুবেরি", "name_pa": "ਨੀਲਬਦਰੀ", "name_or": "ବ୍ଲୁବେରୀ", "name_as": "ব্লুবেৰী"}},
    {"code": 130, "name_en": "Fig", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "LOW", "cost_class": "MEDIUM", "zones": [9, 10, 13], "note": "Pune and north Karnataka", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "001121122222", "names": {"name_hn": "अंजीर", "name_kn": "ಅಂಜೂರ", "name_ta": "படம்", "name_te": "అత్తి", "name_ml": "അത്തിപ്പഴം", "name_mr": "अंजीर", "name_gu": "ફિગ", "name_bn": "চিত্র", "name_pa": "ਅੰਜੀਰ", "name_or": "ଚିତ୍ର", "name_as": "ডুমুৰ"}},
    {"code": 131, "name_en": "Date palm", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [13, 14], "note": "Kutch and west Rajasthan", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "001121122222", "names": {"name_hn": "खजूर", "name_kn": "ಖರ್ಜೂರ", "name_ta": "தேதிகள்", "name_te": "తేదీలు", "name_ml": "തീയതികൾ", "name_mr": "तारखा", "name_gu": "તારીખો", "name_bn": "তারিখ", "name_pa": "ਖਜੂਰ", "name_or": "ତାରିଖ", "name_as": "খেজুৰ"}},
    {"code": 132, "name_en": "Olive", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [6, 14], "note": "Rajasthan trials; oil yield still low", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "001121122222", "names": {"name_hn": "जैतून", "name_kn": "ಆಲಿವ್", "name_ta": "ஆலிவ்", "name_te": "ఆలివ్", "name_ml": "ഒലിവ്", "name_mr": "ऑलिव्ह", "name_gu": "ઓલિવ", "name_bn": "জলপাই", "name_pa": "ਜੈਤੂਨ", "name_or": "ଅଲିଭ୍", "name_as": "জলফাই"}},
    {"code": 133, "name_en": "Avocado", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "HIGH", "zones": [2, 10, 12], "note": "Southern hills (Coorg, Nilgiris), Sikkim", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "एवोकैडो", "name_kn": "ಬೆಣ್ಣೆ ಹಣ್ಣು", "name_ta": "அவகேடோ", "name_te": "అవకాడో", "name_ml": "അവോക്കാഡോ", "name_mr": "एवोकॅडो", "name_gu": "એવોકાડો", "name_bn": "অ্যাভোকাডো", "name_pa": "ਐਵੋਕਾਡੋ", "name_or": "ଆଭୋକାଡୋ", "name_as": "এভোকেডো"}},
    {"code": 134, "name_en": "Grapefruit", "family": "Fruit", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [2, 6], "note": "Minor citrus; Punjab and NE", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "112122122112", "names": {"name_hn": "चकोतरा", "name_kn": "ಚಕ್ಕೋತ", "name_ta": "கிரேப்ஃப்ரூட்", "name_te": "గ్రేప్‌ఫ్రూట్", "name_ml": "ചെറുമധുരനാരങ്ങ", "name_mr": "ग्रेपफ्रूट", "name_gu": "ગ્રેપફ્રૂટ", "name_bn": "জাম্বুরা", "name_pa": "ਚਕੋਤਰਾ", "name_or": "ଗ୍ରେପଫ୍ରୁଟ୍", "name_as": "ৰবাব টেঙা"}},
    {"code": 135, "name_en": "Cacao", "family": "Plantation", "horizon": "PERENNIAL", "water_need": "HIGH", "cost_class": "MEDIUM", "zones": [10, 11, 12], "note": "Intercrop under coconut/arecanut", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 0, "SPRINKLER": 1, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "कोको (Cacao)", "name_kn": "ಕೋಕೋ", "name_ta": "கோகோ (கோகோ)", "name_te": "కోకో (కోకో)", "name_ml": "കൊക്കോ (കൊക്കോ)", "name_mr": "कोको (कोको)", "name_gu": "કોકો (કોકો)", "name_bn": "কোকো (কোকো)", "name_pa": "ਕੋਕੋ", "name_or": "କାକୋ (କୋକୋ)", "name_as": "কোকো"}},
    {"code": 136, "name_en": "Clove", "family": "Spice", "horizon": "PERENNIAL", "water_need": "HIGH", "cost_class": "HIGH", "zones": [10, 12], "note": "Humid hill tracts", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 0, "SPRINKLER": 1, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "लौंग", "name_kn": "ಲವಂಗ", "name_ta": "கிராம்பு", "name_te": "లవంగం", "name_ml": "ഗ്രാമ്പൂ", "name_mr": "लवंग", "name_gu": "લવિંગ", "name_bn": "লবঙ্গ", "name_pa": "ਲੌਂਗ", "name_or": "ଲବଙ୍ଗ", "name_as": "লং"}},
    {"code": 137, "name_en": "Nutmeg", "family": "Spice", "horizon": "PERENNIAL", "water_need": "HIGH", "cost_class": "HIGH", "zones": [12], "note": "Kerala midlands; shade crop", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 0, "SPRINKLER": 1, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "जायफल", "name_kn": "ಜಾಯಿಕಾಯಿ", "name_ta": "ஜாதிக்காய்", "name_te": "జాజికాయ", "name_ml": "ജാതിക്ക", "name_mr": "जायफळ", "name_gu": "જાયફળ", "name_bn": "জায়ফল", "name_pa": "ਜਾਇਫਲ", "name_or": "ଜାଇଫଳ", "name_as": "জায়ফল"}},
    {"code": 138, "name_en": "Cinnamon", "family": "Spice", "horizon": "PERENNIAL", "water_need": "MEDIUM", "cost_class": "MEDIUM", "zones": [10, 12], "note": "Bark spice; Western Ghats", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 1, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "दालचीनी", "name_kn": "ದಾಲ್ಚಿನ್ನಿ", "name_ta": "இலவங்கப்பட்டை", "name_te": "దాల్చిన చెక్క", "name_ml": "കറുവപ്പട്ട", "name_mr": "दालचिनी", "name_gu": "તજ", "name_bn": "দারুচিনি", "name_pa": "ਦਾਲਚੀਨੀ", "name_or": "ଡାଳଚିନି", "name_as": "দালচেনি"}},
    {"code": 139, "name_en": "Saffron", "family": "Spice", "horizon": "PERENNIAL", "water_need": "LOW", "cost_class": "HIGH", "zones": [1], "note": "Kashmir karewa soils only", "season": {"KHARIF": 0, "LONGTERM": 2, "RABI": 0, "ZAID": 0}, "water": {"DRIP": 2, "RAINFED": 2, "SPRINKLER": 2, "SURFACE": 2}, "soil": "011121122122", "names": {"name_hn": "केसर", "name_kn": "ಕೇಸರಿ", "name_ta": "குங்குமப்பூ", "name_te": "కుంకుమ పువ్వు", "name_ml": "കുങ്കുമപ്പൂവ്", "name_mr": "केशर", "name_gu": "કેસર", "name_bn": "জাফরান", "name_pa": "ਕੇਸਰ", "name_or": "କେସର", "name_as": "কেচৰ"}},
]


def _rows(crop):
    uid = f"crop-{crop['code']:03d}"
    code = f"{crop['code']:03d}"
    zones = [{"uid": f"crop-zone-{code}-{z:02d}", "crop_uid": uid, "zone": z, "score": 2 if z in crop["zones"] else 0}
             for z in range(1, 16)]
    seasons = [{"uid": f"crop-season-{code}-{s.lower()}", "crop_uid": uid, "season": s, "score": v}
               for s, v in crop["season"].items()]
    water = [{"uid": f"crop-water-{code}-{m.lower()}", "crop_uid": uid, "method": m, "score": v}
             for m, v in crop["water"].items()]
    soil = [{"uid": f"crop-soil-{code}-{i:02d}", "crop_uid": uid, "soil_class": i, "score": int(v)}
            for i, v in enumerate(crop["soil"], start=1)]
    row = {
        "uid": uid, "code": crop["code"], "name_en": crop["name_en"], **crop["names"],
        "family": crop["family"], "horizon": crop["horizon"], "water_need": crop["water_need"],
        "cost_class": crop["cost_class"], "main_zones_mask": sum(1 << (z - 1) for z in crop["zones"]),
        "note": crop["note"],
    }
    return row, zones, seasons, water, soil


def upgrade():
    bind = op.get_bind()
    existing = {name.lower() for (name,) in bind.execute(sa.text(f'SELECT name_en FROM "{SCHEMA}".crops')).fetchall()}
    new = [c for c in CROPS if c["name_en"].lower() not in existing]
    if not new:
        return
    parts = [_rows(c) for c in new]
    op.bulk_insert(CROPS_TABLE, [p[0] for p in parts])
    op.bulk_insert(ZONE_TABLE, [r for p in parts for r in p[1]])
    op.bulk_insert(SEASON_TABLE, [r for p in parts for r in p[2]])
    op.bulk_insert(WATER_TABLE, [r for p in parts for r in p[3]])
    op.bulk_insert(SOIL_TABLE, [r for p in parts for r in p[4]])


def downgrade():
    uids = [f"crop-{c['code']:03d}" for c in CROPS]
    for table in ("crop_zone_scores", "crop_season_scores", "crop_water_scores", "crop_soil_scores"):
        op.get_bind().execute(sa.text(f'DELETE FROM "{SCHEMA}".{table} WHERE crop_uid = ANY(:uids)'), {"uids": uids})
    op.get_bind().execute(sa.text(f'DELETE FROM "{SCHEMA}".crops WHERE uid = ANY(:uids)'), {"uids": uids})
