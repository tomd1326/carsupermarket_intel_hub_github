"""
Investigative script — makes live AT /vehicles and /valuations calls for a single VRM
and saves a single compiled JSON with only the fields we need to tests/.

Usage (from repo root, with AWS credentials active):
    cd source_code/router/backend
    python ../../../tests/test_autotrader_vrm.py
"""

import json
import sys
import os
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'source_code', 'router', 'backend'))

from utils.secrets_manager_client import get_secret

VRM = 'AP71EKW'
MILEAGE = 50000  # placeholder — adjust if you know the actual mileage

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def get_headers(session, auth_url, key, secret):
    response = session.post(auth_url, data={'key': key, 'secret': secret})
    response.raise_for_status()
    token = response.json()['access_token']
    return {'Authorization': f'Bearer {token}'}


def main():
    secrets = get_secret('dev/carsupermarket/car_pricer/api_access')
    advertiser_id = secrets['ADVERTISER_ID']
    key = secrets['AUTOTRADER_KEY']
    secret = secrets['AUTOTRADER_SECRET']

    host = 'https://api.autotrader.co.uk'
    session = requests.Session()
    headers = get_headers(session, f'{host}/authenticate', key, secret)

    # /vehicles
    vehicles_resp = session.get(f'{host}/vehicles', params={
        'advertiserId': advertiser_id,
        'registration': VRM,
        'valuations': 'true',
        'vehicleMetrics': 'true',
        'odometerReadingMiles': MILEAGE,
        'includeFirstRegistrationDate': 'true',
        'competitors': 'false',
        'features': 'true',
    }, headers=headers)
    vehicles_resp.raise_for_status()
    vehicles_data = vehicles_resp.json()

    vehicle_info = vehicles_data.get('vehicle', {})
    metrics = vehicles_data.get('vehicleMetrics', {}).get('retail', {})

    derivative_id = vehicle_info.get('derivativeId', '')
    first_registration_date = vehicle_info.get('firstRegistrationDate', '')
    factory_fitted_features = [
        {'name': f['name'].strip()}
        for f in vehicles_data.get('features', [])
        if f.get('factoryFitted') is True and f.get('name')
    ]

    # /valuations — uses stored derivative + features, no /vehicles call needed on refresh
    valuations_resp = session.post(
        f'{host}/valuations?advertiserId={advertiser_id}',
        json={
            'vehicle': {
                'derivativeId': derivative_id,
                'firstRegistrationDate': first_registration_date,
                'odometerReadingMiles': MILEAGE,
            },
            'features': factory_fitted_features,
        },
        headers=headers,
    )
    valuations_resp.raise_for_status()
    valuations_data = valuations_resp.json()

    result = {
        'vrm': VRM,
        'mileage': MILEAGE,
        'make': vehicle_info.get('make', ''),
        'model': vehicle_info.get('model', ''),
        'derivative': vehicle_info.get('derivative', ''),
        'derivative_id': derivative_id,
        'first_registration_date': first_registration_date,
        'rating': metrics.get('rating', {}).get('value'),
        'days_to_sell': metrics.get('daysToSell', {}).get('value'),
        'market_condition': metrics.get('marketCondition', {}).get('value'),
        'factory_fitted_features': [f['name'] for f in factory_fitted_features],
        'retail_valuation_gbp': valuations_data.get('valuations', {}).get('retail', {}).get('amountGBP'),
    }

    print(json.dumps(result, indent=2))

    out_path = os.path.join(TESTS_DIR, f'{VRM}_compiled.json')
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f'\nSaved: {out_path}')


if __name__ == '__main__':
    main()
