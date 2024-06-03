from concurrent.futures import ThreadPoolExecutor

import logging
import xml.etree.ElementTree as ET
from utils.rate_limiter import RateLimiter
from utils.singleton import Singleton
from utils.cache import cache
from utils.secrets_manager_client import get_secret
from list_processor.structs import VehicleRow
from list_processor.columns_mapper import BaseColumnsMapper
from typing import Optional, Tuple
import datetime
import requests
import os
import re
import pandas as pd
import pydantic


logger = logging.getLogger(__name__)


class CAPOutput(pydantic.BaseModel):
    capid: Optional[int] = None
    capcode: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    derivative: Optional[str] = None
    registration_date: Optional[str] = None
    rounded_mileage: Optional[int] = None
    live_clean: Optional[int] = None
    live_retail: Optional[int] = None
    monthly_clean: Optional[int] = None
    monthly_retail: Optional[int] = None


class CAPClient(metaclass=Singleton):
    def __init__(self):
        # Basic urls
        self.live_valuation_url = "https://soap.cap.co.uk/usedvalueslive/capusedvalueslive.asmx/GetUsedLive_IdRegDate"
        self.monthly_valuation_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/CAPIDValuation"
        self.vrm_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"

        # Auth data
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')

        self.subscriber_id = secrets['CAP_SUBSCRIBER_ID']
        self.cap_password = secrets['CAP_PASSWORD']
        self.database = "CAR"

        # Rate limiter
        self.limiter = RateLimiter(rate_limit=29, time_window=1)

    @classmethod
    def is_excel_date_format(cls, date_str):
        try:
            int(date_str)
            return True
        except ValueError:
            return False

    @classmethod
    def convert_excel_date(cls, serial):
        excel_epoch = datetime.datetime(1899, 12, 30)  # Excel's epoch starts on January 1, 1900, but there's an off-by-two error
        converted_date = excel_epoch + datetime.timedelta(days=serial)
        return converted_date.strftime('%d/%m/%Y')

    @classmethod
    def round_to_nearest_thousand(cls, mileage):
        if mileage < 1000:
            return 1000
        return round(mileage / 1000) * 1000

    @classmethod
    def round_to_nearest_25(cls, value):
        return round(value / 25) * 25

    def fetch_and_parse_data(self, url, payload):
        with self.limiter:
            logger.debug(f"Fetching data from {url}")
            response = requests.post(
                url,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                data=payload,
                timeout=15
            )
        response.raise_for_status()

        # Parse the XML content
        root = ET.fromstring(response.text)
        namespace_live = {'ns': 'https://soap.cap.co.uk/usedvalueslive'}
        namespace_vrm = {'ns': 'https://soap.cap.co.uk/vrm'}

        if url == self.live_valuation_url:
            valuations = root.findall('.//ns:Valuations/ns:Valuation', namespace_live)
            valuation_list = []
            for valuation in valuations:
                mileage = int(valuation.find('ns:Mileage', namespace_live).text) * 1000
                retail = int(valuation.find('ns:Retail', namespace_live).text)
                clean = int(valuation.find('ns:Clean', namespace_live).text)
                valuation_list.append((mileage, retail, clean))
            return valuation_list

        elif url == self.monthly_valuation_url:
            # Ensure the response is successful
            if root.find('.//ns:Success', namespace_vrm).text == 'true':
                capid_lookup = root.find('.//ns:CAPIDLookup', namespace_vrm)
                valuation = root.find('.//ns:Valuation', namespace_vrm)
                if capid_lookup is not None and valuation is not None and valuation.find('ns:Success',
                                                                                         namespace_vrm).text == 'true':
                    return {
                        'Retail': valuation.find('ns:Retail', namespace_vrm).text,
                        'Clean': valuation.find('ns:Clean', namespace_vrm).text,
                        'CAPMan': capid_lookup.find('ns:CAPMan', namespace_vrm).text,
                        'CAPMod': capid_lookup.find('ns:CAPMod', namespace_vrm).text,
                        'CAPDer': capid_lookup.find('ns:CAPDer', namespace_vrm).text,
                        'CAPcode': capid_lookup.find('ns:CAPcode', namespace_vrm).text
                    }
            return {}  # Return empty dictionary if conditions aren't met


    def get_valuations(self, mileage, valuations):
        # Ensure the valuations are sorted by mileage
        valuations.sort()
        # If mileage is below the first band, use the lowest values
        if mileage < valuations[0][0]:
            return valuations[0][1], valuations[0][2]  # Return retail and clean from the first band

        # If mileage is within the mileage bands
        for i in range(len(valuations) - 1):
            if valuations[i][0] <= mileage < valuations[i + 1][0]:
                lower_mileage, lower_retail, lower_clean = valuations[i]
                upper_mileage, upper_retail, upper_clean = valuations[i + 1]
                # Linear interpolation
                retail_value = lower_retail + (mileage - lower_mileage) * (upper_retail - lower_retail) / (
                            upper_mileage - lower_mileage)
                clean_value = lower_clean + (mileage - lower_mileage) * (upper_clean - lower_clean) / (
                            upper_mileage - lower_mileage)
                return retail_value, clean_value

        # Mileage exceeds the highest known band, perform extrapolation
        lower_mileage, lower_retail, lower_clean = valuations[-2]
        upper_mileage, upper_retail, upper_clean = valuations[-1]
        # Assuming linear continuation of the last known trend
        mileage_diff = upper_mileage - lower_mileage
        retail_diff = upper_retail - lower_retail
        clean_diff = upper_clean - lower_clean
        extrapolated_retail = upper_retail + ((mileage - upper_mileage) / mileage_diff) * retail_diff
        extrapolated_clean = upper_clean + ((mileage - upper_mileage) / mileage_diff) * clean_diff
        return extrapolated_retail, extrapolated_clean

    def live_valuation(self, vehicle: VehicleRow) -> Tuple[Optional[int], Optional[int]]:
        # Fetch current valuation from LIVE_VALUATION_URL
        rounded_mileage = self.round_to_nearest_thousand(vehicle.mileage)

        live_payload = {
            'subscriberId': self.subscriber_id,
            'password': self.cap_password,
            'database': self.database,
            'capid': vehicle.capid,
            'valuationDate': datetime.datetime.now().strftime('%Y-%m-%d'),
            'regDate': vehicle.registration_date.strftime('%Y-%m-%d'),
        }

        current_valuations = self.fetch_and_parse_data(self.live_valuation_url, live_payload)
        if not current_valuations:
            return None, None

        # Calculate interpolated/extrapolated values for current valuation
        current_retail, current_clean = self.get_valuations(rounded_mileage, current_valuations)
        current_retail_25 = self.round_to_nearest_25(current_retail)
        current_clean_25 = self.round_to_nearest_25(current_clean)

        return current_retail_25, current_clean_25

    def capid_monthly_valuation(self, vehicle: VehicleRow) -> dict:
        # Fetch old valuation from MONTHLY_VALUATION_URL
        rounded_mileage = self.round_to_nearest_thousand(vehicle.mileage)

        old_payload = {
            'SubscriberID': self.subscriber_id,
            'Password': self.cap_password,
            'Database': self.database,
            'CAPID': vehicle.capid,
            'RegisteredDate': vehicle.registration_date.strftime('%Y-%m-%d'),
            'Mileage': rounded_mileage,
            'StandardEquipmentRequired': False
        }

        monthly_valuation_data = self.fetch_and_parse_data(self.monthly_valuation_url, old_payload)
        return monthly_valuation_data

    def extract_vrm_values(self, response):
        root = ET.fromstring(response)
        namespaces = {'ns': 'https://soap.cap.co.uk/vrm'}

        vehicle_found = root.find('.//ns:VRMLookup/ns:VehicleFound', namespaces)
        if vehicle_found is not None and vehicle_found.text == 'false':
            return None, None, None, None, None, None, None, None, None  # Ensure this line also has 10 elements

        database = root.find('.//ns:VRMLookup/ns:Database', namespaces)
        capid = root.find('.//ns:VRMLookup/ns:CAPID', namespaces)
        clean = root.find('.//ns:Valuation/ns:Clean', namespaces)
        retail = root.find('.//ns:Valuation/ns:Retail', namespaces)
        capman = root.find('.//ns:VRMLookup/ns:CAPMan', namespaces)
        capmod = root.find('.//ns:VRMLookup/ns:CAPMod', namespaces)
        capder = root.find('.//ns:VRMLookup/ns:CAPDer', namespaces)
        capcode = root.find('.//ns:VRMLookup/ns:CAPcode', namespaces)

        database_text = database.text if database is not None else 'Not Found'
        capid_text = capid.text if capid is not None else 'Not Found'
        clean_text = clean.text if clean is not None else 'Not Found'
        retail_text = retail.text if retail is not None else 'Not Found'
        capman_text = capman.text if capman is not None else 'Not Found'
        capmod_text = capmod.text if capmod is not None else 'Not Found'
        capder_text = capder.text if capder is not None else 'Not Found'
        capcode_text = capcode.text if capcode is not None else 'Not Found'

        registered_date = root.find('.//ns:VRMLookup/ns:RegisteredDate', namespaces)
        if registered_date is not None and registered_date.text:
            # Parse the existing date format
            registered_date_obj = datetime.datetime.strptime(registered_date.text, '%Y-%m-%dT%H:%M:%S')
            # Format it to the desired format
            registered_date_text = registered_date_obj.strftime('%d/%m/%Y')
        else:
            registered_date_text = 'Not Found'

        # Existing return statement with the addition of registered_date_text
        return database_text, capid_text, capman_text, capmod_text, capder_text, capcode_text, clean_text, retail_text, registered_date_text

    def vrm_valuation(self, vehicle: VehicleRow) -> dict:
        vrm_payload = {
            'SubscriberID': self.subscriber_id,
            'Password': self.cap_password,
            'VRM': vehicle.vrm,
            'Mileage': vehicle.mileage,
            'StandardEquipmentRequired': 'false'
        }

        with self.limiter:
            logger.debug(f"Fetching VRM valuation for {vehicle.vrm}")
            response = requests.post(
                self.vrm_url,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                data=vrm_payload,
                timeout=15
            )

        database, capid, capman, capmod, capder, capcode, clean, retail, registered_date = self.extract_vrm_values(response.text)

        return {
            'database': database,
            'capid': capid,
            'capman': capman,
            'capmod': capmod,
            'capder': capder,
            'capcode': capcode,
            'clean': clean,
            'retail': retail,
            'registered_date': registered_date
        }

    @classmethod
    def convert_date_format(cls, date_str):
        try:
            return datetime.datetime.strptime(date_str, '%d/%m/%Y')
        except ValueError:
            return None

    def process_row(self, row: pd.Series, column_mapper: BaseColumnsMapper) -> Optional[dict]:

        vehicle = column_mapper.map_row(row)

        if vehicle.capid:

            if not vehicle.registration_date:
                return CAPOutput().dict()

            live_cap_retail, live_cap_clean = self.live_valuation(vehicle)

            # Do capid_monthly_valuation
            monthly_valuation_data = self.capid_monthly_valuation(vehicle)

            output = CAPOutput(
                capid=vehicle.capid,
                capcode=monthly_valuation_data.get('CAPcode'),
                manufacturer=monthly_valuation_data.get('CAPMan'),
                model=monthly_valuation_data.get('CAPMod'),
                derivative=monthly_valuation_data.get('CAPDer'),
                registration_date=vehicle.registration_date.strftime('%d/%m/%Y') if vehicle.registration_date else None,
                rounded_mileage=vehicle.mileage,
                live_clean=live_cap_clean,
                live_retail=live_cap_retail,
                monthly_clean=monthly_valuation_data.get('Clean'),
                monthly_retail=monthly_valuation_data.get('Retail'),
            )

        else:
            vrm_valuation_data = self.vrm_valuation(vehicle)

            output = CAPOutput(
                capcode=vrm_valuation_data['capcode'],
                manufacturer=vrm_valuation_data['capman'],
                model=vrm_valuation_data['capmod'],
                derivative=vrm_valuation_data['capder'],
                monthly_clean=vrm_valuation_data['clean'],
                monthly_retail=vrm_valuation_data['retail'],
                capid=vrm_valuation_data['capid'],
                rounded_mileage=vehicle.mileage
            )
            vehicle.capid = vrm_valuation_data['capid']

            if vrm_valuation_data['database'] == 'CAR':
                formatted_registered_date = self.convert_date_format(vrm_valuation_data['registered_date'])
                vehicle.registration_date = formatted_registered_date

                # Live
                live_cap_retail, live_cap_clean = self.live_valuation(vehicle)

                if live_cap_clean is not None:
                    output.live_clean = self.round_to_nearest_25(live_cap_clean)
                if live_cap_retail is not None:
                    output.live_retail = self.round_to_nearest_25(live_cap_retail)

        return output.dict()

    def process_file(self, input_df: pd.DataFrame, column_mapper: BaseColumnsMapper) -> pd.DataFrame:
        processed_rows = []

        with ThreadPoolExecutor(max_workers=15) as executor:
            for index, row in input_df.iterrows():
                processed_rows.append(executor.submit(self.process_row, row, column_mapper))

            # Wait for all threads to finish
            processed_rows = [future.result() for future in processed_rows]

        # transform processed rows into a DataFrame
        processed_df = pd.DataFrame(processed_rows)

        # Add prefix cap_ to all columns
        processed_df.columns = [f'cap_{col}' for col in processed_df.columns]

        return processed_df


if __name__ == '__main__':
    from utils.file_reader import read_dataframe_file

    logging.basicConfig(level="DEBUG", format="%(asctime)s - %(name)s - %(threadName)s - %(levelname)s - %(message)s")
    # redirect output to stdout
    logger.addHandler(logging.StreamHandler())

    # Disable useless logging
    logging.getLogger('botocore').setLevel(logging.CRITICAL)
    logging.getLogger('boto3').setLevel(logging.CRITICAL)
    logging.getLogger('s3transfer').setLevel(logging.CRITICAL)
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)
    logging.getLogger('asyncio').setLevel(logging.CRITICAL)
    logging.getLogger('multipart').setLevel(logging.CRITICAL)

    cap_client = CAPClient()
    input_file = '/home/ivan/Downloads/MFL_2024_05_10_1649.xlsx'
    # input_file = '/home/ivan/Downloads/Stocklist_10_05_2024_10_54.csv'
    output_file = '/home/ivan/Downloads/Motability Stocklist exported 17-04-2024_1400_processed.csv'
    input_df = read_dataframe_file(input_file)
    # input_df = input_df[
    output_df = cap_client.process_file(input_df)
    print(1)
