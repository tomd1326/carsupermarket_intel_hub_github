import os
import sys
import json
import requests

# Add parent directory to path to find utils module
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

from utils.secrets_manager_client import get_secret

def get_autotrader_secrets():
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    return get_secret(f'{env}/carsupermarket/car_pricer/api_access')

def authenticate_autotrader():
    secrets = get_autotrader_secrets()
    host = "https://api.autotrader.co.uk"
    auth_url = f"{host}/authenticate"
    auth_data = {
        'key': secrets['AUTOTRADER_KEY'],
        'secret': secrets['AUTOTRADER_SECRET']
    }
    response = requests.post(auth_url, data=auth_data)
    response.raise_for_status()
    return response.json().get('access_token')

def get_vehicle_data(vrm, mileage, access_token):
    secrets = get_autotrader_secrets()
    host = "https://api.autotrader.co.uk"
    vehicles_api_url = f"{host}/vehicles"
    headers = {'Authorization': f'Bearer {access_token}'}
    params = {
        'advertiserId': secrets['ADVERTISER_ID'],
        'registration': vrm,
        'valuations': 'true',
        'vehicleMetrics': 'true',
        'odometerReadingMiles': mileage,
        'includeFirstRegistrationDate': 'true',
        'competitors': 'false',
        'features': 'true'
    }
    response = requests.get(vehicles_api_url, params=params, headers=headers)
    response.raise_for_status()
    return response.json()

def get_valuation_data(derivative_id, first_registration_date, mileage, access_token):
    secrets = get_autotrader_secrets()
    host = "https://api.autotrader.co.uk"
    valuations_api_url = f"{host}/valuations?advertiserId={secrets['ADVERTISER_ID']}"
    headers = {'Authorization': f'Bearer {access_token}'}
    payload = {
        "vehicle": {
            "derivativeId": derivative_id,
            "firstRegistrationDate": first_registration_date,
            "odometerReadingMiles": mileage
        },
        "features": []
    }
    response = requests.post(valuations_api_url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()

def main():
    vrm = "EF71NWO"
    mileage = 30925

    access_token = authenticate_autotrader()
    vehicles_data = get_vehicle_data(vrm, mileage, access_token)

    # Write vehicles API output
    output_dir = r"C:\Users\tom.drayson\OneDrive - Motor Depot"
    os.makedirs(output_dir, exist_ok=True)
    vehicles_file = os.path.join(output_dir, f"vehicles_output_{vrm}.json")
    with open(vehicles_file, "w") as f:
        json.dump(vehicles_data, f, indent=2)
    print(f"Vehicles API JSON saved to: {vehicles_file}")

    # Get required fields for Valuations API
    vehicle_info = vehicles_data.get('vehicle', {})
    derivative_id = vehicle_info.get('derivativeId', '')
    first_registration_date = vehicle_info.get('firstRegistrationDate', '')

    valuations_data = get_valuation_data(derivative_id, first_registration_date, mileage, access_token)

    # Write valuations API output
    valuations_file = os.path.join(output_dir, f"valuations_output_{vrm}.json")
    with open(valuations_file, "w") as f:
        json.dump(valuations_data, f, indent=2)
    print(f"Valuations API JSON saved to: {valuations_file}")

if __name__ == "__main__":
    main()