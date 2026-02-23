"""Quick script to test 20 different vehicles through the API."""
import httpx
import json
import time

API = "http://127.0.0.1:8000/api/v1/vehicle/lookup"

CARS = [
    {"make": "Ford", "model": "Fiesta", "year": 2019, "fuel_type": "Petrol", "model_variant": "1.0T EcoBoost"},
    {"make": "Ford", "model": "Fiesta", "year": 1980, "fuel_type": "Petrol", "model_variant": "1117 HC"},
    {"make": "Ford", "model": "Focus", "year": 2022, "fuel_type": "Diesel", "model_variant": "ST"},
    {"make": "BMW", "model": "3 Series", "year": 2023, "fuel_type": "Diesel", "model_variant": "320d"},
    {"make": "BMW", "model": "X5", "year": 2023, "fuel_type": "Petrol", "model_variant": "xDrive40i"},
    {"make": "Toyota", "model": "Yaris", "year": 2022, "fuel_type": "Hybrid"},
    {"make": "Toyota", "model": "Land Cruiser", "year": 2024, "fuel_type": "Diesel"},
    {"make": "Tesla", "model": "Model 3", "year": 2024},
    {"make": "Tesla", "model": "Model Y", "year": 2024},
    {"make": "Volkswagen", "model": "Golf", "year": 2023, "fuel_type": "Petrol", "model_variant": "GTI"},
    {"make": "Volkswagen", "model": "Golf", "year": 2023, "fuel_type": "Petrol", "model_variant": "R"},
    {"make": "Audi", "model": "A3", "year": 2022, "fuel_type": "Petrol", "model_variant": "35 TFSI"},
    {"make": "Mercedes-Benz", "model": "A-Class", "year": 2023, "fuel_type": "Diesel", "model_variant": "A200d"},
    {"make": "Nissan", "model": "Qashqai", "year": 2023, "fuel_type": "Hybrid"},
    {"make": "Range Rover", "model": "Sport", "year": 2024, "fuel_type": "Diesel"},
    {"make": "Vauxhall", "model": "Corsa", "year": 2024, "fuel_type": "Petrol"},
    {"make": "Vauxhall", "model": "Corsa", "year": 2024, "fuel_type": "Battery Electric"},
    {"make": "Hyundai", "model": "Ioniq 5", "year": 2024},
    {"make": "Kia", "model": "EV6", "year": 2024},
    {"make": "Porsche", "model": "911", "year": 2024, "fuel_type": "Petrol", "model_variant": "Carrera S"},
]

def main():
    start = time.time()
    with httpx.Client(timeout=120.0) as client:
        for i, car in enumerate(CARS, 1):
            label = f"{car['make']} {car['model']}"
            if car.get("model_variant"):
                label += f" {car['model_variant']}"
            if car.get("year"):
                label += f" ({car['year']})"
            if car.get("fuel_type"):
                label += f" [{car['fuel_type']}]"

            print(f"\n[{i}/20] ===== {label} =====")
            try:
                t0 = time.time()
                r = client.post(API, json=car)
                elapsed = time.time() - t0
                r.raise_for_status()
                d = r.json()

                wt = d.get("kerb_weight_kg") or "?"
                gw = d.get("gross_weight_kg") or "?"
                ln = d.get("length_mm") or "?"
                wd = d.get("width_mm") or "?"
                ht = d.get("height_mm") or "?"
                wb = d.get("wheelbase_mm") or "?"
                print(f"  Weight: {wt} kg (gross {gw} kg)")
                print(f"  Dimensions: L:{ln} W:{wd} H:{ht} WB:{wb} mm")

                if d.get("search_variant"):
                    print(f"  Variant searched: {d['search_variant']}")

                g = d.get("gov_data")
                if g:
                    parts = []
                    if g.get("matched_variant"):
                        parts.append(f"matched={g['matched_variant']}")
                    if g.get("fuel_type"):
                        parts.append(f"fuel={g['fuel_type']}")
                    if g.get("engine_size_cc"):
                        parts.append(f"engine={g['engine_size_cc']}cc")
                    if g.get("total_registered"):
                        parts.append(f"registered={g['total_registered']:,}")
                    if g.get("first_registered_year"):
                        parts.append(f"first_year={g['first_registered_year']}")
                    print(f"  Gov: {' | '.join(parts)}")
                    avail = g.get("available_variants")
                    if avail:
                        print(f"  Known variants: {len(avail)}")

                conf = d.get("confidence_score", "?")
                print(f"  Status: {d.get('status')} | Confidence: {conf} | {elapsed:.1f}s")
            except Exception as e:
                print(f"  ERROR: {e}")

    total = time.time() - start
    print(f"\n{'='*60}")
    print(f"Total time: {total:.1f}s for 20 lookups ({total/20:.1f}s avg)")

if __name__ == "__main__":
    main()
