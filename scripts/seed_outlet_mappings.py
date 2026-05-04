"""
Script to seed outlet mappings from the Google Sheets outlet mapping data.
Names are aligned with pypinindia's district/taluk naming convention.
Usage: python scripts/seed_outlet_mappings.py
"""
import asyncio
import csv
import sys
from pathlib import Path
from io import StringIO

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).parent.parent / "SharedBackend" / "src"))

from config import get_settings, get_engine
from managers import OutletManager, OutletMappingManager, OutletSchema, OutletMappingSchema


# ---------------------------------------------------------------------------
# District name aliases: sheet name -> pypinindia canonical name
# ---------------------------------------------------------------------------
DISTRICT_ALIASES = {
    # Old spreadsheet spellings
    "bengaluru urban": "Bengaluru",
    "bengaluru rural": "Bengaluru Rural",
    "chikkmagaluru": "Chikkamagaluru",
    "dharwad/hubballi": "Dharwad",
    "raichuru": "Raichur",
    "yadagiri": "Yadgir",
    "chamarajanagara": "Chamarajanagara",
    "koppala": "Koppal",
    "vijayanagara": "Vijayanagara",
    # New spreadsheet spellings
    "chamrajnagar": "Chamarajanagara",
    "bangalore rural": "Bengaluru Rural",
    "chikkaballapur": "Chikkaballapura",
    "davangere": "Davanagere",
    "ramanagar": "Ramanagara",
    # "ballari" is used in the sheet for Vijayanagara district taluks
    "ballari": "Vijayanagara",
}

# Taluk name aliases: (normalized_district_lower, taluk_sheet_lower) -> pypinindia canonical name
TALUK_ALIASES = {
    # Bagalkot
    ("bagalkot", "bagalkote"): "Bagalkot",
    ("bagalkot", "mudhola"): "Mudhol",
    ("bagalkot", "rabkavi banhatti"): "Rabkavi Banhatti",
    # Belagavi
    ("belagavi", "chikkodi"): "Chikodi",
    ("belagavi", "rayabaga"): "Raibag",
    ("belagavi", "kagawada"): "Kagawad",
    ("belagavi", "mudalgi"): "Mudalgi",
    # Bengaluru
    ("bengaluru", "bengaluru"): "Bengaluru South",
    # Chamarajanagara
    ("chamarajanagara", "chamrajnagar"): "Chamarajanagara",
    ("chamarajanagara", "chamarajanagara"): "Chamarajanagara",
    # Chikkamagaluru
    ("chikkamagaluru", "chikkamagaluru"): "Chikmagalur",
    ("chikkamagaluru", "chikmagalur"): "Chikmagalur",
    ("chikkamagaluru", "kaduru"): "Kadur",
    # Davanagere
    ("davanagere", "davanagere"): "Davangere",
    ("davanagere", "davangere"): "Davangere",
    ("davanagere", "nyamathi"): "Nyamathi",
    # Dharwad
    ("dharwad", "hubballi (rural)"): "Hubli Rural",
    ("dharwad", "hubli (rural)"): "Hubli Rural",
    ("dharwad", "hubballi (urban)"): "Hubli",
    ("dharwad", "hubli(urban)"): "Hubli",
    ("dharwad", "hubli (urban)"): "Hubli",
    ("dharwad", "kundagolu"): "Kundgol",
    ("dharwad", "kundgol"): "Kundgol",
    ("dharwad", "alnavara"): "Alnavar",
    ("dharwad", "navalgund"): "Navalgund",
    ("dharwad", "navalgunda"): "Navalgund",
    # Gadag
    ("gadag", "naragunda"): "Naragund",
    ("gadag", "naragund"): "Naragund",
    ("gadag", "rona"): "Ron",
    ("gadag", "gajendragada"): "Gajendragad",
    ("gadag", "shiratti"): "Shirahatti",    # typo in sheet -> correct name
    # Hassan
    ("hassan", "arasikere"): "Arsikere",
    ("hassan", "arsikere"): "Arsikere",
    ("hassan", "channarayapattana"): "Channarayapatna",
    ("hassan", "channarayapatna"): "Channarayapatna",
    ("hassan", "holenarsipura"): "Holenarasipura",
    ("hassan", "holenarsipur"): "Holenarasipura",
    ("hassan", "arakalagudu"): "Arakalagud",
    ("hassan", "arakalagud"): "Arakalagud",
    ("hassan", "aluru"): "Alur",
    ("hassan", "beluru"): "Belur",
    # Haveri
    ("haveri", "hangala"): "Hangal",
    ("haveri", "savanuru"): "Savanur",
    ("haveri", "hirekeruru"): "Hirekerur",
    ("haveri", "shiggavi"): "Shiggaon",
    ("haveri", "rattihalli"): "Rannebennur",
    # Kalaburagi
    ("kalaburagi", "afzalpura"): "Afzalpur",
    ("kalaburagi", "alanda"): "Aland",
    ("kalaburagi", "chitapura"): "Chittapur",
    ("kalaburagi", "jevargi"): "Jewargi",
    ("kalaburagi", "kamalapura"): "Kamalapura",
    ("kalaburagi", "gulbarga"): "Kalaburagi",   # old city name -> canonical
    # Kolar
    ("kolar", "bangarapete"): "Bangarapet",
    ("kolar", "maluru"): "Malur",
    ("kolar", "mulabagilu"): "Mulbagal",
    ("kolar", "srinivasapura"): "Srinivaspur",
    ("kolar", "srinivaspur"): "Srinivaspur",
    ("kolar", "srinivasapur"): "Srinivaspur",
    ("kolar", "kolar gold fields"): "Kolar Gold Fields",
    # Koppal
    ("koppal", "koppala"): "Koppala",
    # Mandya
    ("mandya", "srirangapattana"): "Srirangapatna",
    ("mandya", "shrirangapattana"): "Srirangapatna",
    ("mandya", "krishnarajapete"): "Krishnarajpet",
    ("mandya", "krishnarajapet"): "Krishnarajpet",
    ("mandya", "madduru"): "Maddur",
    ("mandya", "maddur"): "Maddur",
    # Mysuru
    ("mysuru", "hunasuru"): "Hunsur",
    ("mysuru", "krishnarajanagara"): "K R Nagar",
    ("mysuru", "nanjanagodu"): "Nanjangud",
    ("mysuru", "heggadadevanakote"): "H D Kote",
    ("mysuru", "piriyapattana"): "Periyapatna",
    ("mysuru", "saraguru"): "Saragur",
    ("mysuru", "saligrama"): "Saligrama",
    # Raichur
    ("raichur", "raichuru"): "Raichur",
    ("raichur", "sindhanuru"): "Sindhanur",
    ("raichur", "lingasaguru"): "Lingasugur",
    # Ramanagara
    ("ramanagara", "channapattana"): "Channapatna",
    ("ramanagara", "channapatna"): "Channapatna",
    ("ramanagara", "ramanagar"): "Ramanagara",
    ("ramanagara", "ramanagara"): "Ramanagara",
    # Shivamogga
    ("shivamogga", "shivamogga"): "Shimoga",
    ("shivamogga", "shimoga"): "Shimoga",
    ("shivamogga", "bhadravathi"): "Bhadravati",
    ("shivamogga", "shikaripura"): "Shikarpur",
    ("shivamogga", "soraba"): "Sorab",
    ("shivamogga", "sagar"): "Sagar",
    # Tumakuru
    ("tumakuru", "tumakuru"): "Tumkur",
    ("tumakuru", "tumkur"): "Tumkur",
    ("tumakuru", "chikkanayakanahalli"): "C N Halli",
    ("tumakuru", "c.n.hally"): "C N Halli",
    # Udupi
    ("udupi", "bynduru"): "Karkala",
    ("udupi", "brahmavara"): "Udupi",
    ("udupi", "kapu"): "Udupi",
    ("udupi", "hebri"): "Udupi",
    # Uttara Kannada
    ("uttara kannada", "karwara"): "Karwar",
    ("uttara kannada", "honnavara"): "Honnavar",
    # Vijayanagara
    ("vijayanagara", "hosapete"): "Hospet",
    ("vijayanagara", "hospet"): "Hospet",
    ("vijayanagara", "hoovina hadagali"): "Huvinahadagali",
    ("vijayanagara", "huvinahadagali"): "Huvinahadagali",
    ("vijayanagara", "kotturu"): "Kotturu",
    # Yadgir
    ("yadgir", "yadagiri"): "Yadgir",
    ("yadgir", "gurmitkala"): "Gurmitkal",
}


# ---------------------------------------------------------------------------
# Mapping data from Google Sheets (merged cells filled, duplicates removed,
# typos corrected). Outlet column = outlet name to assign; empty = use taluk
# name as outlet, or fall back to Hassan outlet.
# ---------------------------------------------------------------------------
GOOGLE_SHEET_CSV = """State,District,Taluk,Outlet
Karnataka,Bagalkot,Bagalkot,
Karnataka,Bagalkot,Jamkhandi,Moodalgi
Karnataka,Bagalkot,Mudhol,Moodalgi
Karnataka,Bagalkot,Badami,
Karnataka,Bagalkot,Bilagi,
Karnataka,Bagalkot,Hunagunda,
Karnataka,Bagalkot,Ilkal,
Karnataka,Bagalkot,Guledgudda,
Karnataka,Ballari,Ballari,
Karnataka,Ballari,Kurugodu,
Karnataka,Ballari,Kampli,
Karnataka,Ballari,Sanduru,
Karnataka,Ballari,Siraguppa,
Karnataka,Belagavi,Belagavi,
Karnataka,Belagavi,Athani,Moodalgi
Karnataka,Belagavi,Bailhongal,
Karnataka,Belagavi,Chikodi,Moodalgi
Karnataka,Belagavi,Gokak,Moodalgi
Karnataka,Belagavi,Khanapura,
Karnataka,Belagavi,Nippani,
Karnataka,Belagavi,Raibag,Moodalgi
Karnataka,Belagavi,Savadatti,
Karnataka,Belagavi,Ramadurga,
Karnataka,Belagavi,Hukkeri,
Karnataka,Belagavi,Kitturu,
Karnataka,Bengaluru,Bengaluru,
Karnataka,Bengaluru,Kengeri,
Karnataka,Bengaluru,Krishnarajapura,
Karnataka,Bengaluru,Anekal,
Karnataka,Bengaluru,Yelahanka,
Karnataka,Bangalore Rural,Nelamangala,
Karnataka,Bangalore Rural,Doddaballapura,
Karnataka,Bangalore Rural,Devanahalli,
Karnataka,Bangalore Rural,Hosakote,
Karnataka,Bidar,Aurad,
Karnataka,Bidar,Basavakalyana,
Karnataka,Bidar,Bhalki,
Karnataka,Bidar,Bidar,
Karnataka,Bidar,Chitgoppa,
Karnataka,Bidar,Hulsuru,
Karnataka,Bidar,Humnabad,
Karnataka,Bidar,Kamalanagara,
Karnataka,Chamrajnagar,Chamrajnagar,
Karnataka,Chamrajnagar,Gundlupete,
Karnataka,Chamrajnagar,Kollegala,
Karnataka,Chamrajnagar,Yelanduru,
Karnataka,Chamrajnagar,Hanuru,
Karnataka,Chikkaballapur,Chikkaballapura,
Karnataka,Chikkaballapur,Bagepalli,
Karnataka,Chikkaballapur,Chintamani,
Karnataka,Chikkaballapur,Gauribidanuru,
Karnataka,Chikkaballapur,Gudibanda,
Karnataka,Chikkaballapur,Sidlaghatta,
Karnataka,Chikkaballapur,Cheluru,
Karnataka,Chikkamagaluru,Chikmagalur,Kadur
Karnataka,Chikkamagaluru,Kadur,Kadur
Karnataka,Chikkamagaluru,Koppa,Theerthalli
Karnataka,Chikkamagaluru,Mudigere,Kadur
Karnataka,Chikkamagaluru,Narasimharajapura,Kadur
Karnataka,Chikkamagaluru,Sringeri,Theerthalli
Karnataka,Chikkamagaluru,Tarikere,Kadur
Karnataka,Chitradurga,Chitradurga,Hosdurga
Karnataka,Chitradurga,Challakere,Hosdurga
Karnataka,Chitradurga,Hiriyur,Hosdurga
Karnataka,Chitradurga,Holalkere,Channagiri
Karnataka,Chitradurga,Hosadurga,Hosdurga
Karnataka,Chitradurga,Molakalmuru,
Karnataka,Dakshina Kannada,Mangaluru,
Karnataka,Dakshina Kannada,Ullal,
Karnataka,Dakshina Kannada,Mulki,
Karnataka,Dakshina Kannada,Moodbidri,
Karnataka,Dakshina Kannada,Bantwala,
Karnataka,Dakshina Kannada,Belathangadi,
Karnataka,Dakshina Kannada,Putturu,
Karnataka,Dakshina Kannada,Sulya,
Karnataka,Dakshina Kannada,Kadaba,
Karnataka,Davangere,Davangere,Honnali
Karnataka,Davangere,Harihara,Honnali
Karnataka,Davangere,Channagiri,Channagiri
Karnataka,Davangere,Honnali,Honnali
Karnataka,Davangere,Jagaluru,Channagiri
Karnataka,Dharwad,Kalghatgi,Hubli
Karnataka,Dharwad,Dharwad,Hubli
Karnataka,Dharwad,Hubli (Rural),Hubli
Karnataka,Dharwad,Hubli(Urban),Hubli
Karnataka,Dharwad,Kundgol,Navalgunda
Karnataka,Dharwad,Navalgund,Navalgunda
Karnataka,Gadag,Gadag,Gadag
Karnataka,Gadag,Naragund,Navalgunda
Karnataka,Gadag,Mundaragi,Gadag
Karnataka,Gadag,Ron,Gadag
Karnataka,Gadag,Shirahatti,Gadag
Karnataka,Hassan,Hassan,Salgame
Karnataka,Hassan,Arsikere,
Karnataka,Hassan,Channarayapatna,
Karnataka,Hassan,Holenarsipur,Arkalgudu
Karnataka,Hassan,Sakleshpura,Salgame
Karnataka,Hassan,Alur,Salgame
Karnataka,Hassan,Arakalagud,Arkalgudu
Karnataka,Hassan,Belur,Salgame
Karnataka,Haveri,Ranibennur,Haveri
Karnataka,Haveri,Byadgi,Haveri
Karnataka,Haveri,Hangal,Haveri
Karnataka,Haveri,Haveri,Haveri
Karnataka,Haveri,Savanur,Haveri
Karnataka,Haveri,Hirekerur,Haveri
Karnataka,Haveri,Shiggaon,Haveri
Karnataka,Kalaburagi,Kalaburagi,
Karnataka,Kalaburagi,Afzalpur,
Karnataka,Kalaburagi,Aland,
Karnataka,Kalaburagi,Chincholi,
Karnataka,Kalaburagi,Chittapur,
Karnataka,Kalaburagi,Jewargi,
Karnataka,Kalaburagi,Sedam,
Karnataka,Kalaburagi,Gulbarga,
Karnataka,Kalaburagi,Kalgi,
Karnataka,Kalaburagi,Yedrami,
Karnataka,Kodagu,Madikeri,
Karnataka,Kodagu,Somawarapete,
Karnataka,Kodagu,Virajapete,
Karnataka,Kodagu,Ponnammapete,
Karnataka,Kodagu,Kushalnagara,
Karnataka,Kolar,Kolar,kolar
Karnataka,Kolar,Bangarapet,kolar
Karnataka,Kolar,Malur,kolar
Karnataka,Kolar,Srinivaspur,kolar
Karnataka,Kolar,Mulbagal,kolar
Karnataka,Koppal,Koppala,
Karnataka,Koppal,Gangavathi,
Karnataka,Koppal,Kushtagi,
Karnataka,Koppal,Yelaburga,
Karnataka,Koppal,Kanakagiri,
Karnataka,Koppal,Karatagi,
Karnataka,Koppal,Kukanuru,
Karnataka,Mandya,Mandya,Madduru
Karnataka,Mandya,Maddur,Madduru
Karnataka,Mandya,Malavalli,Madduru
Karnataka,Mandya,Shrirangapattana,Mysuru
Karnataka,Mandya,Krishnarajapet,Jaknahalli
Karnataka,Mandya,Nagamangala,Jaknahalli
Karnataka,Mandya,Pandavapura,Jaknahalli
Karnataka,Mysuru,Mysuru,mysuru
Karnataka,Mysuru,Hunsur,hunsur
Karnataka,Mysuru,K R Nagar,hunsur
Karnataka,Mysuru,Nanjangud,mysuru
Karnataka,Mysuru,H D Kote,hunsur
Karnataka,Mysuru,Periyapatna,hunsur
Karnataka,Mysuru,T Narasipura,mysuru
Karnataka,Raichur,Raichuru,
Karnataka,Raichur,Sindhanuru,
Karnataka,Raichur,Manvi,
Karnataka,Raichur,Devadurga,
Karnataka,Raichur,Lingasaguru,
Karnataka,Raichur,Mudgal,
Karnataka,Raichur,Maski,
Karnataka,Raichur,Sirawara,
Karnataka,Ramanagar,Ramanagar,Ramnagara
Karnataka,Ramanagar,Magadi,Ramnagara
Karnataka,Ramanagar,Kanakapura,Kanakapura
Karnataka,Ramanagar,Channapatna,Ramnagara
Karnataka,Shivamogga,Shimoga,Shivamogga
Karnataka,Shivamogga,Sagar,sagara
Karnataka,Shivamogga,Bhadravati,Shivamogga
Karnataka,Shivamogga,Hosanagara,sagara
Karnataka,Shivamogga,Shikarpur,sagara
Karnataka,Shivamogga,Sorab,sagara
Karnataka,Shivamogga,Tirthahalli,Theerthalli
Karnataka,Tumakuru,Tumkur,Sira
Karnataka,Tumakuru,C.n.hally,Sira
Karnataka,Tumakuru,Kunigal,
Karnataka,Tumakuru,Madhugiri,Sira
Karnataka,Tumakuru,Sira,Sira
Karnataka,Tumakuru,Tipturu,
Karnataka,Tumakuru,Gubbi,Sira
Karnataka,Tumakuru,Koratagere,Sira
Karnataka,Tumakuru,Pavagada,Sira
Karnataka,Tumakuru,Turuvekere,Sira
Karnataka,Udupi,Udupi,Udupi
Karnataka,Udupi,Karkala,Udupi
Karnataka,Udupi,Kundapura,Udupi
Karnataka,Uttara Kannada,Karwara,
Karnataka,Uttara Kannada,Sirsi,
Karnataka,Uttara Kannada,Joida,
Karnataka,Uttara Kannada,Dandeli,
Karnataka,Uttara Kannada,Bhatkal,
Karnataka,Uttara Kannada,Kumta,
Karnataka,Uttara Kannada,Ankola,
Karnataka,Uttara Kannada,Haliyal,Hubli
Karnataka,Uttara Kannada,Honnavara,
Karnataka,Uttara Kannada,Mundagod,Hubli
Karnataka,Uttara Kannada,Siddapura,
Karnataka,Uttara Kannada,Yellapura,
Karnataka,Vijayapura,Vijayapura,
Karnataka,Vijayapura,Indi,
Karnataka,Vijayapura,Basavana Bagewadi,
Karnataka,Vijayapura,Sindgi,
Karnataka,Vijayapura,Muddebihala,
Karnataka,Vijayapura,Talikote,
Karnataka,Vijayapura,Devara Hipparagi,
Karnataka,Vijayapura,Chadchana,
Karnataka,Vijayapura,Tikote,
Karnataka,Vijayapura,Babaleshwara,
Karnataka,Vijayapura,Kolhara,
Karnataka,Vijayapura,Nidagundi,
Karnataka,Vijayapura,Alamela,
Karnataka,Yadgir,Yadagiri,
Karnataka,Yadgir,Shahapura,
Karnataka,Yadgir,Surapura,
Karnataka,Yadgir,Gurmitkala,
Karnataka,Yadgir,Vadagera,
Karnataka,Yadgir,Hunsagi,
Karnataka,ballari,Hospet,Harapanahalli
Karnataka,ballari,Hagaribommanahalli,Harapanahalli
Karnataka,ballari,Harapanahalli,Harapanahalli
Karnataka,ballari,Huvinahadagali,Harapanahalli
Karnataka,ballari,Kudligi,Harapanahalli
"""


def normalize_district(district: str) -> str:
    """Map sheet district name to pypinindia canonical name."""
    return DISTRICT_ALIASES.get(district.lower(), district)


def normalize_taluk(district: str, taluk: str) -> str:
    """Map sheet taluk name to pypinindia canonical name.
    Uses the normalized district (lowercase) as key so lookups are consistent."""
    norm_district = normalize_district(district).lower()
    key = (norm_district, taluk.lower())
    return TALUK_ALIASES.get(key, taluk)


async def get_outlet_by_name(outlet_manager, outlet_name):
    """Find an outlet by its name (case-insensitive, then first-word fallback)."""
    if not outlet_name:
        return None

    outlets = await outlet_manager.fetch_all(filters={"is_active": True})

    for outlet in outlets.items:
        if outlet.outlet_name.lower() == outlet_name.lower():
            return outlet

    outlet_first_word = outlet_name.lower().split()[0]
    for outlet in outlets.items:
        first_word = outlet.outlet_name.lower().split()[0] if outlet.outlet_name else ""
        if first_word == outlet_first_word:
            return outlet

    return None


async def seed_outlet_mappings(engine):
    """Seed outlet mappings from the sheet CSV data."""
    outlet_manager = OutletManager(engine)
    outlet_mapping_manager = OutletMappingManager(engine)

    reader = csv.DictReader(StringIO(GOOGLE_SHEET_CSV))
    rows = list(reader)
    print(f"Processing {len(rows)} rows...")

    hassan_outlet = await get_outlet_by_name(outlet_manager, "Hassan")
    if hassan_outlet:
        print(f"✅ Found fallback outlet: {hassan_outlet.outlet_name}")
    else:
        print("⚠️ Warning: 'Hassan' fallback outlet not found")

    created_count = 0
    skipped_count = 0
    outlet_mappings = []

    for row in rows:
        state = row.get("State", "Karnataka").strip()
        district_raw = row.get("District", "").strip()
        taluk_raw = row.get("Taluk", "").strip()
        outlet_name = row.get("Outlet", "").strip()

        if not district_raw:
            skipped_count += 1
            continue

        district = normalize_district(district_raw)
        taluk = normalize_taluk(district_raw, taluk_raw) if taluk_raw else taluk_raw

        if not outlet_name and taluk_raw:
            outlet_name = taluk_raw

        outlet = await get_outlet_by_name(outlet_manager, outlet_name)

        if not outlet:
            if hassan_outlet:
                print(f"  ℹ️ Outlet not found: '{outlet_name}' (district={district}, taluk={taluk}). Falling back to {hassan_outlet.outlet_name}")
                outlet = hassan_outlet
            else:
                print(f"  ⚠️ Outlet not found: '{outlet_name}' (district={district}, taluk={taluk}). Skipping.")
                skipped_count += 1
                continue

        outlet_mappings.append({
            "state": state.lower(),
            "district": district.lower(),
            "taluk": taluk.lower() if taluk else None,
            "outlet_id": outlet.uid,
            "is_active": True,
        })

    # Deduplicate on (state, district, taluk)
    seen = set()
    unique_mappings = []
    for m in outlet_mappings:
        key = (m["state"], m["district"], m["taluk"] or "")
        if key not in seen:
            seen.add(key)
            unique_mappings.append(m)

    print(f"Unique mappings to create: {len(unique_mappings)}")

    print("Clearing existing mappings...")
    existing = await outlet_mapping_manager.fetch_all(filters={})
    if existing.items:
        for item in existing.items:
            await outlet_mapping_manager.delete(item.uid)

    print("Creating mappings...")
    for mapping_data in unique_mappings:
        try:
            mapping_obj = OutletMappingSchema(**mapping_data)
            await outlet_mapping_manager.create(mapping_obj)
            created_count += 1
        except Exception as e:
            print(f"  ❌ Failed: {mapping_data} — {e}")

    print(f"\n✅ Seed complete: {created_count} created, {skipped_count} skipped")
    return created_count, skipped_count


async def main():
    settings = get_settings()
    engine = get_engine(settings.name)

    print("Initializing database connection...")
    await engine.dispose()

    print("\nStarting outlet mapping seed...")
    created, skipped = await seed_outlet_mappings(engine)

    if created > 0:
        print(f"\n✅ Successfully seeded {created} outlet mappings!")
    else:
        print("\n⚠️ No mappings were created. Check for errors above.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
