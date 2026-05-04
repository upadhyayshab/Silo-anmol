from pypinindia import get_pincode_info
import json

pincodes = ["591230", "583230", "572218", "587121", "573211"]

for pin in pincodes:
    print(f"Pincode: {pin}")
    try:
        data = get_pincode_info(pin)
        print(json.dumps(data, indent=2))
    except Exception as e:
        print(f"Error: {e}")
    print("-" * 20)
