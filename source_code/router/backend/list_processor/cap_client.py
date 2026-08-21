from concurrent.futures import ThreadPoolExecutor
import threading

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
import hashlib
import time
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
    note: Optional[str] = None


class CAPVRMResponse(pydantic.BaseModel):
    database: Optional[str] = None
    capid: Optional[str] = None
    capman: Optional[str] = None
    capmod: Optional[str] = None
    capder: Optional[str] = None
    capcode: Optional[str] = None
    clean: Optional[str] = None
    retail: Optional[str] = None
    registered_date: Optional[str] = None
    note: Optional[str] = None


class CAPClient(metaclass=Singleton):
    def __init__(self):
        # Basic urls
        self.live_valuation_url = "https://soap.cap.co.uk/usedvalueslive/capusedvalueslive.asmx/GetUsedLive_IdRegDate"
        self.monthly_valuation_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/CAPIDValuation"
        self.vrm_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"
        self.image_url = "https://soap.cap.co.uk/images/VehicleImage.aspx"

        # Auth data — use override creds from settings if enabled, otherwise fall back to AWS secret
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        try:
            from utils.db_utils import get_database_connection, get_settings
            with get_database_connection() as _conn:
                _settings = get_settings(_conn)
            if _settings.get('cap_override_enabled') and _settings.get('cap_override_subscriber_id') and _settings.get('cap_override_password'):
                self.subscriber_id = _settings['cap_override_subscriber_id']
                self.cap_password = _settings['cap_override_password']
                logger.warning("[CAP] Using override credentials (subscriber_id=%s)", self.subscriber_id)
            else:
                secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')
                self.subscriber_id = secrets['CAP_SUBSCRIBER_ID']
                self.cap_password = secrets['CAP_PASSWORD']
        except Exception as _e:
            logger.warning("[CAP] Could not load override settings, falling back to AWS secret: %s", _e)
            secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')
            self.subscriber_id = secrets['CAP_SUBSCRIBER_ID']
            self.cap_password = secrets['CAP_PASSWORD']

        # Vehicle Image is a separate CAP product the main account does not
        # subscribe to, so images use the time-limited test account instead.
        # Kept apart from the valuation credentials on purpose: the valuation
        # calls must never start billing the test account by accident. When the
        # main account gets the product, drop these and use the pair above.
        try:
            _img_secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')
            self.image_subscriber_id = _img_secrets.get('CAP_SUBSCRIBER_ID_TEST')
            self.image_password = _img_secrets.get('CAP_PASSWORD_TEST')
        except Exception as _e:
            logger.warning("[CAP] Could not load image credentials: %s", _e)
            self.image_subscriber_id = None
            self.image_password = None

        self.database = "CAR"

        # Rate limiter
        self.limiter = RateLimiter(rate_limit=100, time_window=1)

        self._retry_count = 0
        self._retry_lock = threading.Lock()
        self._local = threading.local()

        # Request counters (reset per process_file call)
        self._counter_lock = threading.Lock()
        self._vrm_requests = 0
        self._live_requests = 0
        self._monthly_requests = 0
        self._vrm_errors = 0
        self._live_errors = 0
        self._connection_errors = 0
        self._abort = False

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

    @property
    def _session(self):
        if not hasattr(self._local, 'session'):
            self._local.session = requests.Session()
        return self._local.session

    def _post_with_retry(self, url, payload, label='', retries=3, timeout=20):
        _RETRYABLE = (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError)
        for attempt in range(retries):
            try:
                with self.limiter:
                    return self._session.post(
                        url,
                        headers={'Content-Type': 'application/x-www-form-urlencoded'},
                        data=payload,
                        timeout=timeout,
                    )
            except _RETRYABLE as e:
                if attempt == retries - 1:
                    raise
                with self._retry_lock:
                    self._retry_count += 1
                wait = 2 ** attempt
                logger.warning(f"CAP request failed [{label}] ({type(e).__name__}, attempt {attempt + 1}/{retries}), retrying in {wait}s")
                time.sleep(wait)

    def image_hashcode(self, capid: int) -> str:
        """
        MD5 for the image API's GET form.

        DOES NOT WORK - do not use. CAP documents the hash as subscriber id +
        password + database + CAP id, but plain concatenation in that order is
        rejected with "Validate_MD5Hash Exception: Unable to verify MD5 hash
        code" (tested 2026-08-21 against an account that the POST form works
        on, so this is the recipe being wrong, not a subscription problem).
        There is presumably a separator, a different order or a casing rule
        that CAP has not documented.

        Kept only as a starting point if the GET form is ever needed. Use the
        POST form in vehicle_image() instead, which works and keeps the
        password off the page.
        """
        raw = f"{self.subscriber_id}{self.cap_password}{self.database}{capid}"
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    def vehicle_image(self, capid: int, registration_date: str = None,
                      width: int = 600, height: int = 448,
                      image_text: str = None, viewpoint: str = None):
        """
        Model image for a CAP id, as (bytes, content_type).

        With a registration date this is CAP's "Get Image for CAPId and Date" —
        it picks the model year that was current on that date, so a facelifted
        model shows the right one. Without a date CAP falls back to the last
        model year, which is wrong for older stock, so pass the date whenever
        it is known.

        `registration_date` must be dd/mm/yyyy. The VRM lookup's
        `registered_date` is already in that format and can be passed straight
        in. Width and height cap at 1024x768. `viewpoint` needs the Enhanced
        Image Product subscription — leave it unset without one.

        Returns (None, None) when CAP has no image, rather than raising: a
        missing picture should not fail the rest of a lookup.
        """
        if not self.image_subscriber_id or not self.image_password:
            logger.warning("CAP image credentials not configured - skipping image request")
            return None, None

        payload = {
            'SUBID': self.image_subscriber_id,
            'PWD':   self.image_password,
            'DB':    self.database,
            'CAPID': str(capid),
            'WIDTH': str(width),
            'HEIGHT': str(height),
        }
        if registration_date:
            payload['DATE'] = registration_date
        if image_text:
            payload['IMAGETEXT'] = image_text
        if viewpoint:
            payload['VIEWPOINT'] = viewpoint

        response = self._post_with_retry(self.image_url, payload, label=f"CAP image {capid}")
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else 'no response'
            logger.warning(f"CAP image request failed for capid={capid}: {status}")
            return None, None

        content_type = response.headers.get('Content-Type', '')
        # CAP answers an error with a 200 and a short "VI Error: ..." text body
        # rather than a non-200, so the content type is what tells us whether we
        # actually got a picture.
        if not content_type.startswith('image/'):
            body = (response.text or '').strip()[:200]
            if 'do not subscribe' in body:
                logger.warning(f"CAP Vehicle Image not on this subscription (capid={capid}): {body}")
            else:
                logger.info(f"CAP returned no image for capid={capid}: {body or content_type}")
            return None, None

        return response.content, content_type

    def fetch_and_parse_data(self, url, payload, vrm=''):
        endpoint = url.split('/')[-1]
        response = self._post_with_retry(url, payload, label=f"{vrm} {endpoint}")
        response.raise_for_status()

        # Parse the XML content
        root = ET.fromstring(response.text)
        namespace_live = {'ns': 'https://soap.cap.co.uk/usedvalueslive'}
        namespace_vrm = {'ns': 'https://soap.cap.co.uk/vrm'}

        # Check the top-level FailReason only (direct child of root) — nested ones e.g. inside
        # <StandardEquipment> are per-feature failures and should not abort the whole request.
        for ns in (namespace_vrm, namespace_live):
            fail_node = root.find('ns:FailReason', ns)
            if fail_node is not None and fail_node.text and fail_node.text.strip():
                logger.error("[CAP] %s returned FailReason for VRM %s: %s", endpoint, vrm, fail_node.text)
                raise RuntimeError(f"CAP API error ({endpoint}): {fail_node.text}")

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
            success_node = root.find('.//ns:Success', namespace_vrm)
            if success_node is None or success_node.text != 'true':
                logger.error("[CAP] %s returned no Success=true for VRM %s. Response: %s", endpoint, vrm, response.text[:500])
                raise RuntimeError(f"CAP API error ({endpoint}): unexpected response (no Success element)")
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

        with self._counter_lock:
            self._live_requests += 1
        current_valuations = self.fetch_and_parse_data(self.live_valuation_url, live_payload, vrm=vehicle.vrm)
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

        with self._counter_lock:
            self._monthly_requests += 1
        monthly_valuation_data = self.fetch_and_parse_data(self.monthly_valuation_url, old_payload, vrm=vehicle.vrm)
        return monthly_valuation_data

    def extract_vrm_values(self, response):
        root = ET.fromstring(response)
        namespaces = {'ns': 'https://soap.cap.co.uk/vrm'}

        vehicle_found = root.find('.//ns:VRMLookup/ns:VehicleFound', namespaces)
        if vehicle_found is not None and vehicle_found.text == 'false':
            fail_reason = root.find('.//ns:VRMLookup/ns:FailReason', namespaces)
            return CAPVRMResponse(note=fail_reason.text)

        database = root.find('.//ns:VRMLookup/ns:Database', namespaces)
        capid = root.find('.//ns:VRMLookup/ns:CAPID', namespaces)
        clean = root.find('.//ns:Valuation/ns:Clean', namespaces)
        retail = root.find('.//ns:Valuation/ns:Retail', namespaces)
        capman = root.find('.//ns:VRMLookup/ns:CAPMan', namespaces)
        capmod = root.find('.//ns:VRMLookup/ns:CAPMod', namespaces)
        capder = root.find('.//ns:VRMLookup/ns:CAPDer', namespaces)
        capcode = root.find('.//ns:VRMLookup/ns:CAPcode', namespaces)

        database_text = database.text if database is not None else None
        capid_text = capid.text if capid is not None else None
        clean_text = clean.text if clean is not None else None
        retail_text = retail.text if retail is not None else None
        capman_text = capman.text if capman is not None else None
        capmod_text = capmod.text if capmod is not None else None
        capder_text = capder.text if capder is not None else None
        capcode_text = capcode.text if capcode is not None else None

        registered_date = root.find('.//ns:VRMLookup/ns:RegisteredDate', namespaces)
        if registered_date is not None and registered_date.text:
            # Parse the existing date format
            registered_date_obj = datetime.datetime.strptime(registered_date.text, '%Y-%m-%dT%H:%M:%S')
            # Format it to the desired format
            registered_date_text = registered_date_obj.strftime('%d/%m/%Y')
        else:
            registered_date_text = None

        # Existing return statement with the addition of registered_date_text
        return CAPVRMResponse(
            database=database_text,
            capid=capid_text,
            capman=capman_text,
            capmod=capmod_text,
            capder=capder_text,
            capcode=capcode_text,
            clean=clean_text,
            retail=retail_text,
            registered_date=registered_date_text
        )

    def vrm_valuation(self, vehicle: VehicleRow) -> dict:
        vrm_payload = {
            'SubscriberID': self.subscriber_id,
            'Password': self.cap_password,
            'VRM': vehicle.vrm.upper(),
            'Mileage': vehicle.mileage,
            'StandardEquipmentRequired': 'false'
        }

        with self._counter_lock:
            self._vrm_requests += 1
        response = self._post_with_retry(self.vrm_url, vrm_payload, label=f"{vehicle.vrm} VRMValuation")

        cap_vrm_response = self.extract_vrm_values(response.text)
        return {
            'database': cap_vrm_response.database,
            'capid': cap_vrm_response.capid,
            'capman': cap_vrm_response.capman,
            'capmod': cap_vrm_response.capmod,
            'capder': cap_vrm_response.capder,
            'capcode': cap_vrm_response.capcode,
            'clean': cap_vrm_response.clean,
            'retail': cap_vrm_response.retail,
            'registered_date': cap_vrm_response.registered_date,
            'note': cap_vrm_response.note
        }

    @classmethod
    def convert_date_format(cls, date_str):
        try:
            return datetime.datetime.strptime(date_str, '%d/%m/%Y')
        except ValueError:
            return None

    _CAP_CONNECTION_ERROR_ABORT = 10  # abort if this many connection/timeout errors received

    def process_row(self, row: pd.Series, column_mapper: BaseColumnsMapper) -> Optional[dict]:
        if self._abort:
            return None
        try:
            result = self._process_row(row, column_mapper)
            # Log VRM not-found (note set by CAP, no exception raised)
            if result and result.get('note') and not result.get('capid'):
                vehicle = column_mapper.map_row(row)
                with self._counter_lock:
                    self._error_log.append({
                        'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                        'api': 'CAP VRM',
                        'vrm': vehicle.vrm if vehicle else str(row.name),
                        'mileage': vehicle.mileage if vehicle else None,
                        'cap_id': None,
                        'status_code': None,
                        'error_type': 'NOT_FOUND',
                        'detail': result.get('note', '')[:200],
                    })
            return result
        except RuntimeError as e:
            vehicle = column_mapper.map_row(row)
            with self._counter_lock:
                self._vrm_errors += 1
                self._error_log.append({
                    'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    'api': 'CAP',
                    'vrm': vehicle.vrm if vehicle else str(row.name),
                    'mileage': vehicle.mileage if vehicle else None,
                    'cap_id': vehicle.capid if vehicle else None,
                    'status_code': None,
                    'error_type': 'CAP_ERROR',
                    'detail': str(e)[:200],
                })
            return CAPOutput().dict()
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            vehicle = column_mapper.map_row(row)
            error_type = 'TIMEOUT' if 'timeout' in type(e).__name__.lower() else 'CONNECTION_ERROR'
            with self._counter_lock:
                self._vrm_errors += 1
                self._connection_errors += 1
                self._error_log.append({
                    'timestamp': datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                    'api': 'CAP VRM',
                    'vrm': vehicle.vrm if vehicle else str(row.name),
                    'mileage': vehicle.mileage if vehicle else None,
                    'cap_id': vehicle.capid if vehicle else None,
                    'status_code': None,
                    'error_type': error_type,
                    'detail': str(e)[:200],
                })
                if self._connection_errors >= self._CAP_CONNECTION_ERROR_ABORT:
                    self._abort = True
                    logger.error("[CAP] Aborting: %d connection/timeout errors received", self._connection_errors)
            logger.debug(f"CAP request failed after all retries for {row.get('VRM', row.name)}: {type(e).__name__}")
            return CAPOutput(note=f"CAP API error: {type(e).__name__}").dict()

    def _process_row(self, row: pd.Series, column_mapper: BaseColumnsMapper) -> Optional[dict]:

        vehicle = column_mapper.map_row(row)
        if not vehicle:
            return None

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
                rounded_mileage=vehicle.mileage,
                note=vrm_valuation_data['note']
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

    def process_file(self, input_df: pd.DataFrame, column_mapper: BaseColumnsMapper, max_workers: int = 25, rate_limit: int = 100) -> pd.DataFrame:
        processed_rows = []
        self._retry_count = 0
        self.limiter = RateLimiter(rate_limit=rate_limit, time_window=1)

        with self._counter_lock:
            self._vrm_requests = 0
            self._live_requests = 0
            self._monthly_requests = 0
            self._vrm_errors = 0
            self._live_errors = 0
            self._connection_errors = 0
            self._abort = False
            self._error_log: list = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for index, row in input_df.iterrows():
                processed_rows.append(executor.submit(self.process_row, row, column_mapper))
            processed_rows = [future.result() for future in processed_rows]

        processed_rows = [row if row is not None else {} for row in processed_rows]
        processed_df = pd.DataFrame(processed_rows)

        # Add prefix cap_ to all columns
        processed_df.columns = [f'cap_{col}' for col in processed_df.columns]

        return processed_df, self._vrm_requests, self._monthly_requests, self._live_requests, self._retry_count, self._vrm_errors, self._live_errors, list(self._error_log)


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
