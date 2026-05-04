from pypinindia import get_pincode_info, get_district
import json

pincodes = ["573211", "591230"]

for pin in pincodes:
    print(f"Pincode: {pin}")
    try:
        data = get_pincode_info(pin)
        print(f"get_pincode_info: {json.dumps(data[0] if data else {}, indent=2)}")
        dist = get_district(pin)
        print(f"get_district: {dist}")
    except Exception as e:
        print(f"Error: {e}")
    print("-" * 20)
