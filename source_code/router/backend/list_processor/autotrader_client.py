import datetime
import io
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
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
        self.limiter = RateLimiter(rate_limit=29, time_window=1)

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
    def send_vehicles_post_request(self, session, headers, vrm, mileage):
        vehicles_params = {
            'advertiserId': self.advertiser_id,
            'registration': vrm,
            'valuations': 'true',
            'vehicleMetrics': 'true',
            'odometerReadingMiles': mileage,
            'includeFirstRegistrationDate': 'true',
            'competitors': 'false',
            'features': 'true'
        }

        with self.limiter:
            vehicles_response = session.get(self.vehicles_api_url, params=vehicles_params, headers=headers)
            if vehicles_response.status_code == 200:
                return vehicles_response.json()
            else:
                # Log the error instead of printing
                logger.error(f"Error in Vehicles API call for VRM {vrm}: Status code {vehicles_response.status_code}", extra={'extra_vrm': vrm})
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
            s = time.time()
            valuations_response = session.post(valuations_url, json=valuations_payload, headers=headers)
            logger.info(f"Valuations API call for VRM {vrm} took {round(time.time() - s, 4)} seconds")

            if valuations_response.status_code == 200:
                return valuations_response.json()
            else:
                logger.error(
                    f"Error in Valuations API call for VRM {vrm}, Derivative ID {derivative_id}: Status code {valuations_response.status_code}",
                    extra={'extra_vrm': vrm})
                return None

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
            logger.info(f'{api_name} API call for VRM {vrm} succeeded with status code: {status_code}',
                         extra={'extra_vrm': vrm})

    def process_row(self, row: dict, session, column_mapper: BaseColumnsMapper) -> dict:
        start_time = time.time()
        # Initialize VRM and Mileage with default values
        # vrm = ''
        # mileage = ''

        vehicle = column_mapper.map_row(row)

        # Initialize features list

        # Iterate over row keys to find VRM and Mileage regardless of case and exact match
        # for key in row.keys():
        #     lower_key = key.lower()
        #     if 'vrm' in lower_key or 'reg' in lower_key and not vrm:
        #         vrm = row[key]
        #     if 'mile' in lower_key and not mileage:
        #         mileage = row[key]

        logger.debug(f'Processing row with VRM {vehicle.vrm} and Mileage {vehicle.mileage}')

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
            factory_fitted_features = []
            factory_fitted_feature_names = []
            features_data = []

            vehicles_data = None  # Initialize vehicles_data to None

            access_token = self.get_access_token(session)

            headers = {'Authorization': f'Bearer {access_token}'}

            s = time.time()
            vehicles_data = self.send_vehicles_post_request(session, headers, vehicle.vrm, vehicle.mileage)
            logger.info(f"Vehicles API call for VRM {vehicle.vrm} took {round(time.time() - s, 4)} seconds")

            # Check if vehicles_data is None and handle it without logging an error
            if vehicles_data is not None:
                vehicle_info = vehicles_data.get('vehicle', {})
                vehicle_metrics = vehicles_data.get('vehicleMetrics', {}).get('retail', {})
                make = vehicle_info.get('make', '')
                model = vehicle_info.get('model', '')
                derivative = vehicle_info.get('derivative', '')
                derivative_id = vehicle_info.get('derivativeId', '')
                first_registration_date = vehicle_info.get('firstRegistrationDate', '')
                description = self.concatenate_description(make, model, derivative)
                rating = vehicle_metrics.get('rating', {}).get('value', 'N/A')
                original_days_to_sell = vehicle_metrics.get('daysToSell', {}).get('value', 'N/A')
                market_condition = vehicle_metrics.get('marketCondition', {}).get('value', 'N/A')

                features_data = vehicles_data.get('features', [])
                for feature in features_data:
                    if feature.get('factoryFitted') is True:
                        feature_name = feature.get('name')
                        if feature_name:
                            factory_fitted_feature_names.append(feature_name.strip())
                            factory_fitted_features.append({"name": feature_name.strip()})

            # Valuations API call
            valuations_data = self.send_valuations_post_request(
                session, headers, vehicle.vrm, derivative_id, first_registration_date, vehicle.mileage, factory_fitted_features
            )
            # Check if valuations_data is None and handle it without logging an error
            if valuations_data is not None:

                valuations_retail_valuation = valuations_data.get('valuations', {}).get('retail', {}).get('amountGBP',
                                                                                                          '')

                # # Extract competitor link
                # competitor_link = vehicles_data.get('links', {}).get('competitors', {}).get('href', 'N/A')
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

            if vehicles_data:
                self.log_api_response('Vehicles', vehicle.vrm, 200, vehicles_data)
            else:
                logger.error(f'Error processing row with VRM {vehicle.vrm}: vehicles_data is None or Vehicles API call failed',
                              extra={'extra_vrm': vehicle.vrm})

            if valuations_data:
                self.log_api_response('Valuations', vehicle.vrm, 200, valuations_data)
            else:
                logger.error(f'Error processing row with VRM {vehicle.vrm}: valuations_data is None or Valuations API call failed',
                              extra={'extra_vrm': vehicle.vrm})

            processing_time = time.time() - start_time

            return {
                'description': description,  # Vehicle description
                'vrm': vehicle.vrm,  # Vehicle registration mark
                'mileage': vehicle.mileage,  # Vehicle mileage
                'valuations_retail_valuation': valuations_retail_valuation,  # Retail valuation from the Valuations API
                'rating': rating,  # Rating from the Vehicles API
                'original_days_to_sell': original_days_to_sell,  # Days to sell from the Vehicles API
                'market_condition': market_condition,  # Market condition from the Vehicles API
                # 'total_results': total_results,  # Total results from competitor analysis
                'factory_fitted_feature_names': ', '.join(factory_fitted_feature_names),  # Comma-separated list of factory-fitted features
                'processing_time': processing_time,
            }

        except Exception as e:
            return {
                'description': 'N/A',
                'vrm': vehicle.vrm,
                'mileage': vehicle.mileage,
                'valuations_retail_valuation': 'N/A',
                'rating': 'N/A',
                'original_days_to_sell': 'N/A',
                'market_condition': 'N/A',
                # 'total_results': 'N/A',
                'factory_fitted_feature_names': 'N/A',
                'processing_time': 'N/A',
            }

    def process_file(self, input_df: pd.DataFrame, column_mapper: BaseColumnsMapper) -> pd.DataFrame:
        # Initialize session object
        session = requests.Session()

        # Initialize processed rows
        processed_rows = []

        # Initialize thread pool
        with ThreadPoolExecutor(max_workers=1) as executor:
            # Process each row in the DataFrame
            for index, row in input_df.iterrows():
                processed_rows.append(executor.submit(self.process_row, (row.to_dict()), session, column_mapper))

            # Wait for all threads to finish
            processed_rows = [future.result() for future in processed_rows]

        # transform processed rows into a DataFrame
        processed_df = pd.DataFrame(processed_rows)

        # Add prefix at_ to all columns
        processed_df.columns = [f'at_{col}' for col in processed_df.columns]

        return processed_df

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
