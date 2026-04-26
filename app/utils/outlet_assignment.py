"""
Outlet assignment utilities - replaces Google Sheets macro with database-based mapping.

This module provides auto-assign outlet functionality that:
1. Looks up district/taluk to outlet mapping from database
2. Falls back to Hassan outlet if no mapping found (with first active outlet as final safety)
"""
from typing import Optional, Tuple
from datetime import datetime
from config import get_engine

from managers import OutletManager, OutletMappingManager

# Mirrors the aliases used in seed_outlet_mappings.py so raw order values
# (which may use xlsx/local spellings) resolve to the same canonical names
# that were stored in the DB during seeding.
DISTRICT_ALIASES = {
    # Sheet spelling variants (kept for backward compat with old order data)
    "bengaluru urban": "Bengaluru",
    "bengaluru rural": "Bengaluru Rural",
    "bangalore rural": "Bengaluru Rural",
    "chikkmagaluru": "Chikkamagaluru",
    "dharwad/hubballi": "Dharwad",
    "raichuru": "Raichur",
    "yadagiri": "Yadgir",
    "chamarajanagara": "Chamarajanagara",
    "koppala": "Koppal",
    "vijayanagara": "Vijayanagara",
    "chamrajnagar": "Chamarajanagara",
    "chikkaballapur": "Chikkaballapura",
    "davangere": "Davanagere",
    "ramanagar": "Ramanagara",
    "ballari": "Vijayanagara",
    # pypinindia returns old census names for some districts — map to the
    # canonical names stored in DB during seeding.
    "tumkur": "Tumakuru",
    "shimoga": "Shivamogga",
    "mysore": "Mysuru",
    "gulbarga": "Kalaburagi",
    "bellary": "Ballari",
    "bijapur": "Vijayapura",
    "bangalore": "Bengaluru",
    "belgaum": "Belagavi",
}

TALUK_ALIASES = {
    ("bagalkot", "bagalkote"): "Bagalkot",
    ("bagalkot", "mudhola"): "Mudhol",
    ("bagalkot", "rabkavi banhatti"): "Rabkavi Banhatti",
    ("belagavi", "chikkodi"): "Chikodi",
    ("belagavi", "rayabaga"): "Raibag",
    ("belagavi", "kagawada"): "Kagawad",
    ("belagavi", "mudalgi"): "Mudalgi",
    ("bengaluru", "bengaluru"): "Bengaluru South",
    ("chikkamagaluru", "chikkamagaluru"): "Chikmagalur",
    ("chikkamagaluru", "kaduru"): "Kadur",
    ("davanagere", "davanagere"): "Davangere",
    ("davanagere", "nyamathi"): "Nyamathi",
    ("dharwad", "hubballi (rural)"): "Hubli Rural",
    ("dharwad", "hubballi (urban)"): "Hubli",
    ("dharwad", "kundagolu"): "Kundgol",
    ("dharwad", "alnavara"): "Alnavar",
    ("gadag", "naragunda"): "Naragund",
    ("gadag", "rona"): "Ron",
    ("gadag", "gajendragada"): "Gajendragad",
    ("hassan", "arasikere"): "Arsikere",
    ("hassan", "channarayapattana"): "Channarayapatna",
    ("hassan", "holenarsipura"): "Holenarasipura",
    ("hassan", "arakalagudu"): "Arakalagud",
    ("hassan", "aluru"): "Alur",
    ("hassan", "beluru"): "Belur",
    ("haveri", "hangala"): "Hangal",
    ("haveri", "savanuru"): "Savanur",
    ("haveri", "hirekeruru"): "Hirekerur",
    ("haveri", "shiggavi"): "Shiggaon",
    ("haveri", "rattihalli"): "Rannebennur",
    ("kalaburagi", "afzalpura"): "Afzalpur",
    ("kalaburagi", "alanda"): "Aland",
    ("kalaburagi", "chitapura"): "Chittapur",
    ("kalaburagi", "jevargi"): "Jewargi",
    ("kalaburagi", "kamalapura"): "Kamalapura",
    ("kolar", "bangarapete"): "Bangarapet",
    ("kolar", "maluru"): "Malur",
    ("kolar", "mulabagilu"): "Mulbagal",
    ("kolar", "srinivasapura"): "Srinivaspur",
    ("kolar", "kolar gold fields"): "Kolar Gold Fields",
    ("koppal", "koppala"): "Koppala",
    ("mandya", "srirangapattana"): "Srirangapatna",
    ("mandya", "krishnarajapete"): "Krishnarajpet",
    ("mysuru", "hunasuru"): "Hunsur",
    ("mysuru", "krishnarajanagara"): "K R Nagar",
    ("mysuru", "nanjanagodu"): "Nanjangud",
    ("mysuru", "heggadadevanakote"): "H D Kote",
    ("mysuru", "piriyapattana"): "Periyapatna",
    ("mysuru", "saraguru"): "Saragur",
    ("mysuru", "saligrama"): "Saligrama",
    ("raichur", "raichuru"): "Raichur",
    ("raichur", "sindhanuru"): "Sindhanur",
    ("raichur", "lingasaguru"): "Lingasugur",
    ("ramanagara", "channapattana"): "Channapatna",
    ("shivamogga", "shivamogga"): "Shimoga",
    ("shivamogga", "bhadravathi"): "Bhadravati",
    ("shivamogga", "shikaripura"): "Shikarpur",
    ("shivamogga", "soraba"): "Sorab",
    ("tumakuru", "tumakuru"): "Tumkur",
    ("tumakuru", "tumkur"): "Tumkur",
    ("tumakuru", "chikkanayakanahalli"): "C N Halli",
    ("tumakuru", "c.n.halli"): "C N Halli",
    ("tumakuru", "c.n. halli"): "C N Halli",
    ("tumakuru", "c.n.hally"): "C N Halli",
    ("udupi", "bynduru"): "Karkala",
    ("udupi", "brahmavara"): "Udupi",
    ("udupi", "kapu"): "Udupi",
    ("udupi", "hebri"): "Udupi",
    ("uttara kannada", "karwara"): "Karwar",
    ("uttara kannada", "honnavara"): "Honnavar",
    ("vijayanagara", "hosapete"): "Hospet",
    ("vijayanagara", "hoovina hadagali"): "Huvinahadagali",
    ("vijayanagara", "kotturu"): "Kotturu",
    ("yadgir", "yadagiri"): "Yadgir",
    ("yadgir", "gurmitkala"): "Gurmitkal",
}


def normalize_location(district: Optional[str], taluk: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Apply DISTRICT_ALIASES and TALUK_ALIASES to match the canonical names stored
    in the DB (which were seeded using the same alias tables).

    Returns (normalized_district_lowercase, normalized_taluk_lowercase).
    """
    if not district:
        return district, taluk

    norm_district = DISTRICT_ALIASES.get(district.lower(), district)

    norm_taluk = taluk
    if taluk:
        norm_taluk = TALUK_ALIASES.get(
            (norm_district.lower(), taluk.lower()), taluk
        )

    return norm_district.lower(), norm_taluk.lower() if norm_taluk else norm_taluk


async def auto_assign_outlet(
    engine,
    order_id: Optional[str] = None,
    district: Optional[str] = None,
    pincode: Optional[str] = None,
    state: Optional[str] = None,
    taluk: Optional[str] = None
) -> Optional[object]:
    """
    Auto-assign outlet based on database mapping.

    Priority: district + taluk > pincode (resolves district/taluk from pincode)
    Falls back to Hassan outlet if no mapping found.

    Args:
        engine: Database engine
        order_id: Optional order ID for CRM activity pushes
        district: District name (optional, can be resolved from pincode)
        pincode: Pincode (optional but recommended for automatic resolution)
        state: State name (optional, defaults to 'Karnataka')
        taluk: Taluk name (optional, can be resolved from pincode)

    Returns:
        Outlet object if found/assigned, None otherwise
    """
    from pypinindia import get_district, get_pincode_info

    outlet_manager = OutletManager(engine)
    outlet_mapping_manager = OutletMappingManager(engine)

    try:
        # Resolve district from pincode if not provided
        if not district and pincode:
            try:
                from pypinindia import get_district
                resolved_district = get_district(pincode)
                district = resolved_district[0] if isinstance(resolved_district, list) and resolved_district else resolved_district
            except Exception:
                pass

        # Resolve taluk from pincode if not provided
        if not taluk and pincode:
            try:
                from pypinindia import get_pincode_info
                data = get_pincode_info(pincode)
                if data:
                    taluk = data[0].get('taluk')
            except Exception:
                pass

        # Resolve district from taluk if we have taluk but not district
        if taluk and not district:
            try:
                from pypinindia import get_pincode_info
                data = get_pincode_info(pincode) if pincode else None
                if data and isinstance(data, list) and len(data) > 0:
                    taluk_info = next((t for t in data if t.get('taluk', '').lower() == taluk.lower()), None)
                    if taluk_info:
                        district = taluk_info.get('district')
            except Exception:
                pass

        # Normalize inputs for database matching — apply alias tables so that
        # raw order spellings (e.g. "Bengaluru Urban", "Chikkmagaluru") resolve
        # to the canonical names stored during seeding.
        state = (state or "karnataka").lower()
        district, taluk = normalize_location(district, taluk)

        print(f"DEBUG: Looking up outlet mapping for state='{state}', district='{district}', taluk='{taluk}'")

        # Step 1: Try to find exact match with state, district, and taluk
        mapping = None
        if district and taluk:
            mapping = await outlet_mapping_manager.fetch_all(
                filters={
                    "state": state,
                    "district": district,
                    "taluk": taluk,
                    "is_active": True
                }
            )
            if mapping and mapping.items:
                print(f"MATCH: Found exact match: {state} -> {district} -> {taluk}")

        # Step 2: If no exact match, try district only (without taluk)
        if not mapping or not mapping.items:
            if district:
                mapping = await outlet_mapping_manager.fetch_all(
                    filters={
                        "state": state,
                        "district": district,
                        "taluk": None,
                        "is_active": True
                    }
                )
                if mapping and mapping.items:
                    print(f"MATCH: Found district-only match: {state} -> {district}")

        # Step 3: If still no match, try district with empty taluk string
        if not mapping or not mapping.items:
            if district:
                mapping = await outlet_mapping_manager.fetch_all(
                    filters={
                        "state": state,
                        "district": district,
                        "is_active": True
                    }
                )
                # Filter out records where taluk is not None and not empty string
                if mapping and mapping.items:
                    mapping.items = [m for m in mapping.items if not m.taluk or m.taluk == ""]
                if mapping and mapping.items:
                    print(f"MATCH: Found district match (empty taluk): {state} -> {district}")

        # Get all active outlets
        all_outlets = await outlet_manager.fetch_all(filters={"is_active": True})

        if not all_outlets.items:
            print(f"ERROR: No active outlets found in database")
            return None

        # Process mapping results
        if mapping and mapping.items:
            # Try to find outlet for each mapping
            for mapping_entry in mapping.items:
                outlet = await outlet_manager.fetch(mapping_entry.outlet_id)
                if outlet and outlet.is_active:
                    print(f"MATCH: Assigned outlet: {outlet.outlet_name}")
                    return outlet

        # Fallback: If no mapping found, use Hassan outlet as fallback
        print(f"FALLBACK: No outlet mapping found, using fallback logic")
        
        # Try to find Hassan outlet specifically
        hassan_outlet = next((o for o in all_outlets.items if "hassan" in o.outlet_name.lower()), None)
        
        if hassan_outlet:
            print(f"FALLBACK: Fallback outlet (Hassan): {hassan_outlet.outlet_name}")
            return hassan_outlet
            
        # Final fallback: use the first active outlet if Hassan not found
        first_outlet = all_outlets.items[0]
        print(f"FALLBACK: Fallback outlet (First Active): {first_outlet.outlet_name}")
        return first_outlet

    except Exception as e:
        print(f"ERROR: Error in outlet assignment: {str(e)}")
        return None


async def push_outlet_not_assigned(engine, order_id: str, district: Optional[str], pincode: Optional[str], reason: str = ""):
    """
    Push a DELIVERY_STATUS activity to CRM indicating no outlet was assigned.
    """
    from managers import CustomerOrderManager, CustomerOrderSchema, OrderItemManager, OrderItemSchema
    from utils.constants import ActivityType
    from services import CRMService

    crm_service = CRMService()
    order_manager = CustomerOrderManager(engine)
    order_item_manager = OrderItemManager(engine)

    try:
        unassigned_order = await order_manager.fetch(
            order_id,
            joins=[(CustomerOrderSchema.items, OrderItemSchema.manager)]
        )
        unassigned_dump = unassigned_order.model_dump()
        unassigned_dump["assigned_at"] = None
        unassigned_dump["order_status"] = "Not Assigned"
        unassigned_dump["outlet_assignment_error"] = (
            f"No outlet found for district='{district}', pincode='{pincode}'"
            + (f": {reason}" if reason else "")
        )
        crm_result = await crm_service.push_activity({
            "delivery_data": unassigned_dump,
            "activity_event": ActivityType.DELIVERY_STATUS
        })
        print(f"CRM: CRM outlet-not-assigned activity: {crm_result}")
    except Exception as crm_err:
        print(f"FALLBACK: CRM push_activity (outlet not assigned) failed: {str(crm_err)}")


async def push_outlet_assigned(engine, order_id: str, district: Optional[str], pincode: Optional[str]):
    """
    Push a DELIVERY_STATUS activity to CRM indicating outlet was assigned.
    """
    from managers import CustomerOrderManager, CustomerOrderSchema, OrderItemManager, OrderItemSchema, OutletManager, OutletSchema
    from utils.constants import ActivityType
    from services import CRMService

    crm_service = CRMService()
    order_manager = CustomerOrderManager(engine)
    order_item_manager = OrderItemManager(engine)
    outlet_manager = OutletManager(engine)

    try:
        order = await order_manager.fetch(
            order_id,
            joins=[
                (CustomerOrderSchema.assigned_outlet, OutletSchema.manager),
                (CustomerOrderSchema.items, OrderItemSchema.manager)
            ]
        )
        order_dump = order.model_dump()
        order_dump["assigned_at"] = datetime.utcnow().isoformat()
        order_dump["order_status"] = "Assigned"
        crm_result = await crm_service.push_activity({
            "delivery_data": order_dump,
            "activity_event": ActivityType.DELIVERY_STATUS
        })
        print(f"CRM: CRM outlet-assigned activity: {crm_result}")
    except Exception as crm_err:
        print(f"FALLBACK: CRM push_activity (outlet assigned) failed: {str(crm_err)}")
