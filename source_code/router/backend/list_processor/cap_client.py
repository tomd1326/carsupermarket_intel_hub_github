import requests
import logging
import xml.etree.ElementTree as ET
from utils.rate_limiter import RateLimiter
from utils.singleton import Singleton
from utils.cache import cache
from typing import Optional
import datetime
import pydantic


logger = logging.getLogger(__name__)


class VehicleRow(pydantic.BaseModel):
    vrm: str
    mileage: int
    registration_date: str
    capid: Optional[int] = None


class CAPClient(metaclass=Singleton):
    def __init__(self, subscriber_id: str, cap_password):
        # Basic urls
        self.live_valuation_url = "https://soap.cap.co.uk/usedvalueslive/capusedvalueslive.asmx/GetUsedLive_IdRegDate"
        self.monthly_valuation_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/CAPIDValuation"
        self.vrm_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"

        # Auth data
        self.subscriber_id = subscriber_id
        self.cap_password = cap_password
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

    def fetch_and_parse_data(self, session, url, payload, vrm_value):
        try:
            response = session.post(
                    url,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    data=payload)
            response.raise_for_status()
            content = response.text()

            # Parse the XML content
            root = ET.fromstring(content)
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

        except Exception as e:
            current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logging.error(f"An error occurred for VRM {vrm_value} at {current_time}: {str(e)}")
            return {}

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

    def construct_vehicle_row(self, input_data) -> VehicleRow:
        """
        This function verifies the presence of:
            - CAPID column. If absent, executes a call to recognize the CAPID based on VRM.
            - Registration date. If absent checks for the Year column and calculates the registration date. If Year is
              also absent, the function returns None.
        """

    def process_row(self, session, row, total_valid_rows):
        # Check if any of the required columns have missing or NaN values
        if row.isna().any():
            return None  # Skip processing for this row

        # Check if the date is in the 5-digit Excel format and convert if necessary
        if self.is_excel_date_format(row['DFR']):
            row['DFR'] = self.convert_excel_date(int(row['DFR']))

        reg_date = datetime.datetime.strptime(row['DFR'], '%d/%m/%Y').strftime('%Y-%m-%d')
        rounded_mileage = self.round_to_nearest_thousand(row[mileage_column])
        capid_value = row[capid_column]
        vrm_value = row[vrm_column]

        # Fetch current valuation from LIVE_VALUATION_URL
        live_payload = {
            'subscriberId': self.subscriber_id,
            'password': self.cap_password,
            'database': self.database,
            'capid': int(capid_value),
            'valuationDate': VALUATION_DATE,
            'regDate': reg_date,
            'mileage': rounded_mileage
        }

        current_valuations = self.fetch_and_parse_data(session, self.live_valuation_url, live_payload, vrm_value)
        if not current_valuations:
            return None  # Skip this row due to error fetching data

        # Calculate interpolated/extrapolated values for current valuation
        current_retail, current_clean = self.get_valuations(rounded_mileage, current_valuations)
        current_retail_25 = self.round_to_nearest_25(current_retail)
        current_clean_25 = self.round_to_nearest_25(current_clean)

        # Fetch old valuation from MONTHLY_VALUATION_URL
        old_payload = {
            'SubscriberID': self.subscriber_id,
            'Password': self.cap_password,
            'Database': self.database,
            'CAPID': int(row[capid_column]),
            'RegisteredDate': reg_date,
            'Mileage': rounded_mileage,
            'StandardEquipmentRequired': False  # As per your setup, might not need this field based on your API
        }

        old_valuation_data = await fetch_and_parse_data(session, MONTHLY_VALUATION_URL, old_payload, vrm_value)
        if not old_valuation_data:
            return None  # Handle empty data or errors more gracefully

        old_retail = old_valuation_data.get('Retail')
        old_clean = old_valuation_data.get('Clean')
        if not old_retail or not old_clean:
            return None  # Skip this row if necessary data is missing

        today_date = datetime.now().strftime('%d/%m/%Y')

        # Assemble all data into the final row format
        return [
            vrm_value, None, old_valuation_data['CAPMan'], old_valuation_data['CAPMod'], old_valuation_data['CAPDer'],
            row['DFR'],
            capid_value, old_valuation_data['CAPcode'], rounded_mileage, None, None, None, today_date, None, None, None,
            old_clean, None, None, old_retail, None, None, None, DATABASE, None, None,
            None, None, None, current_clean_25, None, None, current_retail_25
        ]
