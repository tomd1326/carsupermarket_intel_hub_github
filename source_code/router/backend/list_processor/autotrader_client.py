import datetime
import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import threading
import functools
from inspect import signature
import pandas as pd

import requests
from utils.secrets_manager_client import get_secret
from list_processor.columns_mapper import BaseColumnsMapper
from utils.rate_limiter import RateLimiter
from utils.singleton import Singleton
from utils.cache import cache


logger = logging.getLogger(__name__)


def _unwrap_vehicles_result(payload: dict) -> dict:
    """
    Return the object holding `vehicle`, `vehicleMetrics`, `features` etc.

    Autotrader is moving the Vehicles API payload from the root into a
    `results` array — `{"vehicle": {...}}` becomes
    `{"results": [{"vehicle": {...}}], "totalResults": 1}`. Both shapes are
    served until the root objects are removed on 28th October 2026, so prefer
    results[0] and fall back to the root. Once the old shape is gone the
    fallback simply stops being taken.
    """
    results = (payload or {}).get('results')
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return results[0]
    return payload or {}


def _factory_fitted_features(features: list) -> tuple[list, list]:
    """
    Split the features list into the factory-fitted ones, as (payload, names).

    `names` is the plain list for display and storage — genuinely VIN-detected
    extras, shown to flag "Autotrader confirmed this is fitted".

    `payload` is NOT what should be sent to /valuations on its own — see
    _valuation_features_payload for that. It is kept here only because some
    callers want just the fitted subset by itself (its shape happens to match
    the valuations feature format, [{"name": ...}]).

    On this account Autotrader does not resolve VIN spec for most vehicles, so
    `factoryFitted` is null on nearly every feature and both lists come back
    empty. That is expected, not a fault — see optional_features() for the
    list of extras a user can pick from instead.
    """
    fitted_features = []
    fitted_names = []
    for feature in features or []:
        if feature.get('factoryFitted') is True:
            feature_name = feature.get('name')
            if feature_name:
                name = feature_name.strip()
                fitted_names.append(name)
                fitted_features.append({"name": name})
    return fitted_features, fitted_names


def _valuation_features_payload(features: list) -> list:
    """
    The features payload to send to /valuations for this vehicle's own spec.

    Confirmed against Autotrader's docs and a live call (2026-08-21, VIN
    YV1XZK7V6P2046295 @ 66,811 miles — reproduced Portal's £20,418 exactly):
    the `features` field is not an additive list of extras on top of a base
    price. Per the docs, "This will need to be a list of all features for the
    vehicle" — sending only the factory-fitted Optional items (or any other
    partial list) prices the car as if every omitted feature, Standard
    included, were absent, and comes back identical to the empty/base request.

    So this sends every Standard feature (fitted to every car of the
    derivative) plus any Optional feature Autotrader has flagged
    factoryFitted: true for this VIN — the full spec, not just the extras.
    """
    payload = []
    for feature in features or []:
        name = feature.get('name')
        if not name:
            continue
        if feature.get('type') == 'Standard' or feature.get('factoryFitted') is True:
            payload.append({"name": name.strip()})
    return payload


# The API's own category strings, mapped to the names the Autotrader Portal
# shows and the order it lists them in. The API returns no ordering of its own,
# and the Portal renames several, so both live here rather than in the page.
# Anything not listed keeps its API name and sorts to the end alphabetically.
_FEATURE_CATEGORIES = [
    ('Drivers Assistance',       'Driver convenience'),
    ('Exterior',                 'Exterior features'),
    ('Interior',                 'Interior features'),
    ('Paint',                    'Paint'),
    ('Safety and Security',      'Safety'),
    ('Audio and Communications', 'Audio and communications'),
    ('Illumination',             'Illumination'),
    ('Performance',              'Performance'),
    ('Upholstery',               'Upholstery'),
]
_CATEGORY_LABEL = {api: label for api, label in _FEATURE_CATEGORIES}
_CATEGORY_ORDER = {api: i for i, (api, _) in enumerate(_FEATURE_CATEGORIES)}


def optional_features(features: list) -> list:
    """
    The optional extras that can be fitted to this derivative, for the picker
    on the Assess Vehicle page.

    Autotrader marks each feature Standard (fitted to every car of this
    derivative) or Optional (an extra that may or may not be on this one).
    Only the Optional ones are worth choosing between. rarityRating and
    valueRating come straight from the API and drive the Rare/Valuable badges,
    matching what the Autotrader Portal shows.

    Ordered by category as the Portal orders them, then by name within each.
    """
    out = []
    for feature in features or []:
        if feature.get('type') != 'Optional':
            continue
        name = (feature.get('name') or '').strip()
        if not name:
            continue
        api_category = feature.get('category') or 'Other'
        out.append({
            'name':          name,
            'category':      _CATEGORY_LABEL.get(api_category, api_category),
            'rarity_rating': feature.get('rarityRating'),
            'value_rating':  feature.get('valueRating'),
            # Unknown categories sort after the known ones, alphabetically.
            '_order':        _CATEGORY_ORDER.get(api_category, len(_FEATURE_CATEGORIES)),
        })
    out.sort(key=lambda f: (f['_order'], f['category'], f['name']))
    for f in out:
        del f['_order']
    return out


class AutoTraderClient(metaclass=Singleton):
    def __init__(self):
        # Basic urls
        host = "https://api.autotrader.co.uk"
        # host = 'https://api-sandbox.autotrader.co.uk'
        self.auth_url = f"{host}/authenticate"
        self.vehicles_api_url = f"{host}/vehicles"
        self.valuations_api_url = f"{host}/valuations"

        # Auth data
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')

        self.advertiser_id = secrets['ADVERTISER_ID']
        self.key = secrets['AUTOTRADER_KEY']
        self.secret = secrets['AUTOTRADER_SECRET']

        # Auth tokens
        self.access_token = None
        self.token_expiration_time = None
        self.auth_lock = Lock()

        # Rate limiter
        self.limiter = RateLimiter(rate_limit=60, time_window=1)

        # Request counters and error log (reset per process_file call)
        self._counter_lock = threading.Lock()
        self._vehicles_requests = 0
        self._valuations_requests = 0
        self._vehicles_errors = 0
        self._valuations_errors = 0
        self._rate_limit_hits = 0
        self._abort = False
        self._error_log: list = []

    def get_access_token(self, session) -> str:
        with self.auth_lock:
            if self.token_expiration_time is None or self.token_expiration_time < datetime.datetime.now():
                with self.limiter:
                    response = session.post(self.auth_url, data={'key': self.key, 'secret': self.secret})
                    if response.status_code == 200:
                        auth_data = response.json()
                        # parse the .expires attribute (format 'Mon Apr 29 16:05:50 UTC 2024')
                        # expiration_time = datetime.datetime.strptime(auth_data.get('.expires'), '%a %b %d %H:%M:%S UTC %Y')
                        self.token_expiration_time = datetime.datetime.now() + datetime.timedelta(minutes=15)
                        self.access_token = auth_data.get('access_token')
                    else:
                        raise Exception(f'Authentication failed with status code: {response.status_code}')

            return self.access_token

    @cache(ignore_args=['session', 'headers'], ex=60*60*24)
    def competitor_request(self, session, headers, competitor_link):
        with self.limiter:
            stock_response = session.get(competitor_link, headers=headers)
            if stock_response.status_code == 200:
                stock_response_json = stock_response.json()
                total_results = stock_response_json.get('totalResults', 'N/A')
                return total_results
            else:
                return 'N/A'

    @cache(ignore_args=['session', 'headers'], ex=60*60*24)
    def send_vehicles_post_request(self, session, headers, vrm, mileage, lookup_by='registration'):
        """
        `lookup_by` selects which identifier is sent — the API rejects both at
        once with "Must supply exactly one of vin or registration". Tested
        2026-08-21: a VIN lookup returns exactly the same features and
        valuation as the VRM one on this account, so there is no reason to
        prefer it. Kept for if Autotrader ever enables VIN spec.
        """
        if lookup_by not in ('registration', 'vin'):
            raise ValueError("lookup_by must be either 'registration' or 'vin'")
        vehicles_params = {
            'advertiserId': self.advertiser_id,
            lookup_by: vrm,
            'valuations': 'true',
            'vehicleMetrics': 'true',
            'odometerReadingMiles': mileage,
            'includeFirstRegistrationDate': 'true',
            'competitors': 'false',
            'features': 'true'
        }

        with self.limiter:
            with self._counter_lock:
                self._vehicles_requests += 1
            vehicles_response = session.get(self.vehicles_api_url, params=vehicles_params, headers=headers)
            if vehicles_response.status_code == 200:
                return vehicles_response.json()
            else:
                with self._counter_lock:
                    self._vehicles_errors += 1
                    sc = vehicles_response.status_code
                    error_type = 'NOT_FOUND' if sc == 404 else ('RATE_LIMITED' if sc == 429 else f'HTTP_{sc}')
                    self._error_log.append({
                        'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                        'api': 'AT Vehicles',
                        'vrm': vrm,
                        'mileage': mileage,
                        'cap_id': None,
                        'status_code': sc,
                        'error_type': error_type,
                        'detail': vehicles_response.text[:200],
                    })
                    if sc == 429:
                        self._rate_limit_hits += 1
                        if self._rate_limit_hits >= self._AT_RATE_LIMIT_ABORT:
                            self._abort = True
                            logger.error("[AT] Aborting: %d rate limit (429) responses received", self._rate_limit_hits)
                logger.debug(f"Vehicles API error for VRM {vrm}: Status code {vehicles_response.status_code}", extra={'extra_vrm': vrm})
                return None

    @cache(ignore_args=['session', 'headers'], ex=60*60*24)
    def send_valuations_post_request(self, session, headers, vrm, derivative_id, first_registration_date, mileage, factory_fitted_features):
        valuations_url = f"{self.valuations_api_url}?advertiserId={self.advertiser_id}"
        valuations_payload = {
            "vehicle": {
                "derivativeId": derivative_id,
                "firstRegistrationDate": first_registration_date,
                "odometerReadingMiles": mileage,
            },
            "features": factory_fitted_features
        }

        with self.limiter:
            with self._counter_lock:
                self._valuations_requests += 1
            valuations_response = session.post(valuations_url, json=valuations_payload, headers=headers)

            if valuations_response.status_code == 200:
                return valuations_response.json()
            else:
                with self._counter_lock:
                    self._valuations_errors += 1
                    sc = valuations_response.status_code
                    self._error_log.append({
                        'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                        'api': 'AT Valuations',
                        'vrm': vrm,
                        'mileage': mileage,
                        'cap_id': None,
                        'status_code': sc,
                        'error_type': 'NOT_FOUND' if sc == 404 else ('RATE_LIMITED' if sc == 429 else f'HTTP_{sc}'),
                        'detail': valuations_response.text[:200],
                    })
                logger.debug(f"Valuations API error for VRM {vrm}: Status code {valuations_response.status_code}", extra={'extra_vrm': vrm})
                return None

    def valuation_with_features(self, session, headers, vrm, derivative_id,
                                first_registration_date, mileage, feature_names,
                                all_features=None, average_valuation=None):
        """
        The three retail figures, as (average, base, with_spec).

        - average:   average spec for this derivative. Pass in the inline retail
                     figure from the /vehicles response as `average_valuation`;
                     it is already fetched, so there is no call for it here.
        - base:      no extras at all — /valuations with an empty feature list.
        - with_spec: Standard features plus the chosen Optional extras. None
                     when nothing is selected.

        Confirmed 2026-08-21 against Autotrader's docs and a live call (VIN
        YV1XZK7V6P2046295 @ 66,811 miles): `features` is not additive extras on
        top of a base price — the docs say "This will need to be a list of all
        features for the vehicle". Sending only the chosen Optional names
        (without every Standard feature) prices the car as if it had no
        Standard equipment at all and returns the same figure as the empty/base
        request. `all_features` — the vehicle's full feature list from
        /vehicles — is required so this can include every Standard feature
        alongside the selected Optional ones; without it, with_spec silently
        matches base for every vehicle, which is the bug this replaces.

        The Autotrader Portal measures its "Spec: £N above/below average"
        against the AVERAGE, not the base.

        Only used when someone picks a spec on the Assess Vehicle page. The bulk
        supplier list runs never reach this, so their timings are unchanged.
        """
        def _retail(data):
            if not data:
                return None
            return data.get('valuations', {}).get('retail', {}).get('amountGBP')

        base = _retail(self.send_valuations_post_request(
            session, headers, vrm, derivative_id, first_registration_date, mileage, []
        ))

        with_spec = None
        if feature_names:
            standard_names = {
                f['name'].strip() for f in (all_features or [])
                if f.get('type') == 'Standard' and f.get('name')
            }
            # Sorted so the same spec always produces the same cache key — the
            # decorator keys on str() of the arguments, which is order-sensitive.
            chosen = {n.strip() for n in feature_names if n and n.strip()}
            features = [{"name": n} for n in sorted(standard_names | chosen)]
            with_spec = _retail(self.send_valuations_post_request(
                session, headers, vrm, derivative_id, first_registration_date, mileage, features
            ))

        return average_valuation, base, with_spec

    @classmethod
    def concatenate_description(cls, make, model, derivative) -> str:
        make = make if make is not None else ''
        model = model if model is not None else ''
        derivative = derivative if derivative is not None else ''
        description = ' '.join(part for part in [make, model, derivative] if part)
        return description

    @classmethod
    def log_api_response(cls, api_name, vrm, status_code, json_data, error_only=False):
        if json_data is not None and status_code != 200:
            try:
                error_messages = json_data.get('messages', [])
                for message in error_messages:
                    logger.error(f'{api_name} API call for VRM {vrm} failed: {message}', extra={'extra_vrm': vrm})
            except Exception as e:
                logger.error(
                    f'{api_name} API call for VRM {vrm} failed with status code: {status_code} and error: {e}',
                    extra={'extra_vrm': vrm})
        elif json_data is not None and not error_only:
            logger.debug(f'{api_name} API call for VRM {vrm} succeeded with status code: {status_code}',
                         extra={'extra_vrm': vrm})

    _AT_RATE_LIMIT_ABORT = 10  # abort if this many 429s received

    def process_row(self, row: dict, session, column_mapper: BaseColumnsMapper) -> dict:
        if self._abort:
            return None

        vehicle = column_mapper.map_row(row)

        if vehicle is None:
            return None

        # Initialize features list

        # Iterate over row keys to find VRM and Mileage regardless of case and exact match
        # for key in row.keys():
        #     lower_key = key.lower()
        #     if 'vrm' in lower_key or 'reg' in lower_key and not vrm:
        #         vrm = row[key]
        #     if 'mile' in lower_key and not mileage:
        #         mileage = row[key]


        try:
            # Initialize all variables to default values
            first_registration_date = ''
            make = ''
            model = ''
            derivative = ''
            derivative_id = ''
            market_condition = ''
            rating = ''
            original_days_to_sell = ''
            valuations_retail_valuation = 'N/A'
            total_results = 'N/A'
            description = ''
            factory_fitted_feature_names = []
            valuation_features = []
            features_data = []
            vehicle_info = {}

            vehicles_data = None  # Initialize vehicles_data to None

            access_token = self.get_access_token(session)

            headers = {'Authorization': f'Bearer {access_token}'}

            vehicles_data = self.send_vehicles_post_request(session, headers, vehicle.vrm, vehicle.mileage)

            # Check if vehicles_data is None and handle it without logging an error
            if vehicles_data is not None:
                vehicles_result = _unwrap_vehicles_result(vehicles_data)
                vehicle_info = vehicles_result.get('vehicle', {})
                vehicle_metrics = vehicles_result.get('vehicleMetrics', {}).get('retail', {})
                make = vehicle_info.get('make', '')
                model = vehicle_info.get('model', '')
                derivative = vehicle_info.get('derivative', '')
                derivative_id = vehicle_info.get('derivativeId', '')
                first_registration_date = vehicle_info.get('firstRegistrationDate', '')
                description = self.concatenate_description(make, model, derivative)
                rating = vehicle_metrics.get('rating', {}).get('value', 'N/A')
                original_days_to_sell = vehicle_metrics.get('daysToSell', {}).get('value', 'N/A')
                market_condition = vehicle_metrics.get('marketCondition', {}).get('value', 'N/A')

                features_data = vehicles_result.get('features', [])
                _, factory_fitted_feature_names = _factory_fitted_features(features_data)
                valuation_features = _valuation_features_payload(features_data)

            # Valuations API call — requires derivative_id, first_registration_date and mileage
            valuations_data = None
            if vehicles_data is not None and derivative_id and first_registration_date and vehicle.mileage:
                valuations_data = self.send_valuations_post_request(
                    session, headers, vehicle.vrm, derivative_id, first_registration_date, vehicle.mileage, valuation_features
                )
            if valuations_data is not None:

                valuations_retail_valuation = valuations_data.get('valuations', {}).get('retail', {}).get('amountGBP',
                                                                                                          '')

                # # Extract competitor link
                # competitor_link = _unwrap_vehicles_result(vehicles_data).get('links', {}).get('competitors', {}).get('href', 'N/A')
                #
                # # Check if competitor link is available
                # if competitor_link != 'N/A':
                #     # Append advertiserId to the competitor link
                #     if 'advertiserId=' not in competitor_link:
                #         if '?' in competitor_link:
                #             competitor_link += '&'
                #         else:
                #             competitor_link += '?'
                #         competitor_link += f'advertiserId={self.advertiser_id}'
                #
                #     # Append advertiserType=Trade to the competitor link
                #     if 'advertiserType=' not in competitor_link:
                #         competitor_link += '&advertiserType=Trade'
                #
                #     # Append !insuranceWriteoffCategory with a blank value
                #     if '!insuranceWriteoffCategory=' not in competitor_link:
                #         competitor_link += '&!insuranceWriteoffCategory='
                #
                #     # Send GET request to the Stock API using Competitor Link and the session object
                #     if competitor_link != 'N/A':
                #         total_results = self.competitor_request(session, headers, competitor_link)

            if not vehicles_data:
                logger.debug(f'Vehicles data unavailable for VRM {vehicle.vrm}', extra={'extra_vrm': vehicle.vrm})
            if not valuations_data:
                logger.debug(f'Valuations data unavailable for VRM {vehicle.vrm}', extra={'extra_vrm': vehicle.vrm})

            return {
                'description': description,
                'vrm': vehicle.vrm,
                'mileage': vehicle.mileage,
                'valuations_retail_valuation': valuations_retail_valuation,
                'rating': rating,
                'original_days_to_sell': original_days_to_sell,
                'market_condition': market_condition,
                'factory_fitted_feature_names': ', '.join(factory_fitted_feature_names),
                'at_first_registration_date': first_registration_date,
                'derivative_id': derivative_id,
                'vin': vehicle_info.get('vin', ''),
            }

        except Exception as e:
            with self._counter_lock:
                self._vehicles_errors += 1
                self._error_log.append({
                    'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    'api': 'AT Vehicles',
                    'vrm': vehicle.vrm,
                    'mileage': vehicle.mileage,
                    'cap_id': None,
                    'status_code': None,
                    'error_type': 'TIMEOUT' if 'timeout' in type(e).__name__.lower() else 'EXCEPTION',
                    'detail': str(e)[:200],
                })
            return {
                'description': 'N/A',
                'vrm': vehicle.vrm,
                'mileage': vehicle.mileage,
                'valuations_retail_valuation': 'N/A',
                'rating': 'N/A',
                'original_days_to_sell': 'N/A',
                'market_condition': 'N/A',
                'factory_fitted_feature_names': 'N/A',
                'at_first_registration_date': 'N/A',
                'derivative_id': '',
                'vin': '',
            }

    def process_file(self, input_df: pd.DataFrame, column_mapper: BaseColumnsMapper, max_workers: int = 40, rate_limit: int = 60) -> pd.DataFrame:
        session = requests.Session()
        processed_rows = []

        self.limiter = RateLimiter(rate_limit=rate_limit, time_window=1)

        with self._counter_lock:
            self._vehicles_requests = 0
            self._valuations_requests = 0
            self._vehicles_errors = 0
            self._valuations_errors = 0
            self._rate_limit_hits = 0
            self._abort = False
            self._error_log = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for index, row in input_df.iterrows():
                processed_rows.append(executor.submit(self.process_row, (row.to_dict()), session, column_mapper))
            processed_rows = [future.result() for future in processed_rows]

        processed_rows = [row if row is not None else {} for row in processed_rows]
        processed_df = pd.DataFrame(processed_rows)
        processed_df.columns = [f'at_{col}' for col in processed_df.columns]
        return processed_df, self._vehicles_requests, self._valuations_requests, self._vehicles_errors, self._valuations_errors, list(self._error_log)

        # # Merge new DataFrame with the original DataFrame
        # processed_df = pd.concat([df, processed_df], axis=1)
        #
        # Save the processed DataFrame to the output file
        # if output_file_path.endswith('.csv'):
        #     processed_df.to_csv(output_file_path, index=False)
        # elif output_file_path.endswith('.xlsx'):
        #     processed_df.to_excel(output_file_path, index=False)
        # else:
        #     raise ValueError("Unsupported file format. Please use a .csv or .xlsx file.")
        #
        # return output_file_path
