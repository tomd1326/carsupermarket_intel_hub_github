# Full path: C:\Users\tom.drayson\OneDrive - Motor Depot\Data Engineering\Buying Tool test files\test_fixed.py

#!/usr/bin/env python3
"""
API Speed Test Script - Performance Fixed
==========================================
Preserves input order and columns WITHOUT sacrificing speed.
"""

import os
import time
import datetime
import threading
import logging
import json
import signal
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional, List
from collections import defaultdict

import pandas as pd
import requests
import xml.etree.ElementTree as ET
import boto3
from botocore.config import Config
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# API Rate Configuration
AT_VEHICLES_RATE = 100      # AutoTrader Vehicles API
AT_VALUATIONS_RATE = 100    # AutoTrader Valuations API  
CAP_RATE = 300              # CAP API

# File paths
INPUT_FILE = r"C:\Users\tom.drayson\OneDrive - Motor Depot\Data Engineering\Buying Tool test files\Stock 29th Sept.xlsx"
OUTPUT_FILE = r"C:\Users\tom.drayson\OneDrive - Motor Depot\Data Engineering\Buying Tool test files\Stock_29th_Sept_FIXED_RESULTS.xlsx"
ERROR_LOG_FILE = r"C:\Users\tom.drayson\OneDrive - Motor Depot\Data Engineering\Buying Tool test files\api_errors.log"

# Global shutdown flag and error tracking
shutdown_requested = False
api_error_stats = defaultdict(int)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Additional file handler for errors
error_handler = logging.FileHandler(ERROR_LOG_FILE)
error_handler.setLevel(logging.ERROR)
error_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
error_handler.setFormatter(error_formatter)
logger.addHandler(error_handler)

# Signal handler for graceful shutdown
def signal_handler(sig, frame):
    global shutdown_requested
    logger.info("Shutdown requested by user. Finishing current tasks...")
    shutdown_requested = True

signal.signal(signal.SIGINT, signal_handler)

# Optimized HTTP Session Factory
def create_optimized_session():
    """Create an optimized HTTP session with connection pooling"""
    session = requests.Session()
    
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "PUT", "DELETE", "OPTIONS", "TRACE", "POST"]
    )
    
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=100,
        pool_maxsize=100,
        pool_block=False
    )
    
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    session.headers.update({
        'Connection': 'keep-alive',
        'User-Agent': 'CarSupermarket-API-Test/1.0'
    })
    
    return session

# Secrets Manager with caching
_cached_secrets = {}
_secret_cache_lock = threading.Lock()

def _infer_region(secret_name: str, default_region: str) -> str:
    if secret_name.startswith("prod/"):
        return "eu-west-1"
    if secret_name.startswith("dev/"):
        return "us-east-2"
    return default_region

def get_secret(secret_name: str, region_name: Optional[str] = None) -> dict:
    with _secret_cache_lock:
        if secret_name in _cached_secrets:
            return _cached_secrets[secret_name]
        
    env_region = os.getenv("AWS_REGION", "eu-west-1")
    chosen_region = region_name or _infer_region(secret_name, env_region)

    sm = boto3.client(
        "secretsmanager",
        region_name=chosen_region,
        config=Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 2},
        ),
    )

    resp = sm.get_secret_value(SecretId=secret_name)
    secret_data = json.loads(resp["SecretString"])
    
    with _secret_cache_lock:
        _cached_secrets[secret_name] = secret_data
    
    return secret_data

# Rate Limiter
class RateLimiter:
    """Rate limiter with minimal overhead"""
    def __init__(self, rate_limit: int, name: str = ""):
        self.rate_limit = rate_limit
        self.name = name
        self.calls = []
        self.lock = threading.Lock()

    def __enter__(self):
        if shutdown_requested:
            return
            
        now = time.time()
        with self.lock:
            cutoff = now - 1.0
            self.calls = [t for t in self.calls if t >= cutoff]
            
            if len(self.calls) >= self.rate_limit:
                sleep_time = 1.0 - (now - self.calls[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.time()
                    cutoff = now - 1.0
                    self.calls = [t for t in self.calls if t >= cutoff]
            
            self.calls.append(now)

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

# API Processor - FAST version
class APIProcessor:
    def __init__(self):
        logger.info("Initializing API processor...")
        
        # AutoTrader setup
        self.at_host = "https://api.autotrader.co.uk"
        self.at_auth_url = f"{self.at_host}/authenticate"
        self.at_vehicles_url = f"{self.at_host}/vehicles"
        self.at_valuations_url = f"{self.at_host}/valuations"
        
        # CAP setup
        self.cap_vrm_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"
        
        # Rate limiters
        self.at_vehicles_limiter = RateLimiter(AT_VEHICLES_RATE, "AT_Vehicles")
        self.at_valuations_limiter = RateLimiter(AT_VALUATIONS_RATE, "AT_Valuations")
        self.cap_limiter = RateLimiter(CAP_RATE, "CAP")
        
        # Error tracking
        self.real_api_errors = defaultdict(int)
        
        # Session pool
        self.session_pool = []
        self.session_lock = threading.Lock()
        
        # Authentication
        self.at_token = None
        self.token_expires = None
        self.token_lock = threading.Lock()
        
        # Initialize
        self._load_credentials()
        self._prewarm_system()
        
        logger.info(f"API processor ready - CAP {CAP_RATE}/sec, AT_Vehicles {AT_VEHICLES_RATE}/sec, AT_Valuations {AT_VALUATIONS_RATE}/sec")

    def _load_credentials(self):
        """Load API credentials with caching"""
        try:
            env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
            logger.info(f"Loading credentials for {env} environment...")
            
            secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')
            
            self.at_key = secrets['AUTOTRADER_KEY']
            self.at_secret = secrets['AUTOTRADER_SECRET']
            self.at_advertiser_id = secrets['ADVERTISER_ID']
            
            self.cap_subscriber_id = secrets['CAP_SUBSCRIBER_ID']
            self.cap_password = secrets['CAP_PASSWORD']
            
            logger.info("Credentials loaded successfully")
            
        except Exception as e:
            logger.error(f"Failed to load credentials: {e}")
            raise Exception("Missing API credentials")

    def _prewarm_system(self):
        """Pre-warm connections and authenticate"""
        logger.info("Pre-warming system...")
        
        # Create session pool
        for _ in range(80):
            session = create_optimized_session()
            self.session_pool.append(session)
        
        # Pre-authenticate
        try:
            session = self.get_session()
            self._authenticate_autotrader(session)
            self.return_session(session)
            logger.info("AutoTrader pre-authentication successful")
        except Exception as e:
            logger.warning(f"AutoTrader pre-authentication failed: {e}")
        
        logger.info("System pre-warming complete")

    def get_session(self):
        """Get session from pool"""
        with self.session_lock:
            if self.session_pool:
                return self.session_pool.pop()
            else:
                return create_optimized_session()

    def return_session(self, session):
        """Return session to pool"""
        with self.session_lock:
            if len(self.session_pool) < 100:
                self.session_pool.append(session)
            else:
                session.close()

    def _authenticate_autotrader(self, session):
        """Authenticate with AutoTrader"""
        response = session.post(self.at_auth_url, data={
            'key': self.at_key,
            'secret': self.at_secret
        }, timeout=10)
        
        if response.status_code == 200:
            auth_data = response.json()
            with self.token_lock:
                self.at_token = auth_data.get('access_token')
                self.token_expires = datetime.datetime.now() + datetime.timedelta(minutes=15)
            return self.at_token
        else:
            raise Exception(f"AutoTrader auth failed: {response.status_code}")

    def get_autotrader_token(self, session):
        """Get AutoTrader token"""
        with self.token_lock:
            if self.token_expires and datetime.datetime.now() < self.token_expires:
                return self.at_token
        
        return self._authenticate_autotrader(session)

    def _log_real_api_error(self, api_name: str, vrm: str, status_code: int, response_text: str = ""):
        """Log API server errors only"""
        error_key = f"{api_name}_{status_code}"
        self.real_api_errors[error_key] += 1
        api_error_stats[error_key] += 1
        
        if status_code == 429:
            logger.error(f"API RATE LIMIT: {api_name} returned 429 for VRM {vrm}")
        elif status_code in [503, 502, 504, 500]:
            logger.error(f"API SERVER ERROR: {api_name} returned {status_code} for VRM {vrm}")
        elif status_code == 401:
            logger.error(f"API AUTH ERROR: {api_name} returned 401 for VRM {vrm}")

    def call_cap_vrm(self, session, vrm: str, mileage: int) -> Dict[str, Any]:
        """Call CAP VRM API"""
        if shutdown_requested:
            return {'cap_success': False, 'cap_error': 'Shutdown requested'}
            
        try:
            payload = {
                'SubscriberID': self.cap_subscriber_id,
                'Password': self.cap_password,
                'VRM': vrm,
                'Mileage': mileage,
                'StandardEquipmentRequired': 'false'
            }
            
            with self.cap_limiter:
                response = session.post(
                    self.cap_vrm_url,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    data=payload,
                    timeout=15
                )
            
            if response.status_code == 200:
                root = ET.fromstring(response.text)
                ns = {'ns': 'https://soap.cap.co.uk/vrm'}
                
                return {
                    'cap_success': True,
                    'cap_capid': self._get_xml_text(root, './/ns:VRMLookup/ns:CAPID', ns),
                    'cap_capcode': self._get_xml_text(root, './/ns:VRMLookup/ns:CAPcode', ns),
                    'cap_make': self._get_xml_text(root, './/ns:VRMLookup/ns:CAPMan', ns),
                    'cap_model': self._get_xml_text(root, './/ns:VRMLookup/ns:CAPMod', ns),
                    'cap_derivative': self._get_xml_text(root, './/ns:VRMLookup/ns:CAPDer', ns),
                    'cap_clean': self._get_xml_text(root, './/ns:Valuation/ns:Clean', ns),
                    'cap_retail': self._get_xml_text(root, './/ns:Valuation/ns:Retail', ns),
                    'cap_reg_date': self._get_xml_text(root, './/ns:VRMLookup/ns:RegisteredDate', ns),
                    'cap_response_code': response.status_code
                }
            else:
                if response.status_code >= 500:
                    self._log_real_api_error("CAP_VRM", vrm, response.status_code, response.text)
                return {
                    'cap_success': False,
                    'cap_error': f'Status {response.status_code}',
                    'cap_response_code': response.status_code
                }
                
        except Exception as e:
            logger.error(f"CAP exception for {vrm}: {str(e)}")
            return {
                'cap_success': False,
                'cap_error': str(e),
                'cap_response_code': 0
            }

    def call_autotrader_vehicles(self, session, vrm: str, mileage: int) -> Dict[str, Any]:
        """Call AutoTrader Vehicles API"""
        if shutdown_requested:
            return {'at_success': False, 'at_error': 'Shutdown requested'}
            
        try:
            token = self.get_autotrader_token(session)
            headers = {'Authorization': f'Bearer {token}'}
            
            params = {
                'advertiserId': self.at_advertiser_id,
                'registration': vrm,
                'valuations': 'true',
                'vehicleMetrics': 'true',
                'odometerReadingMiles': mileage,
                'includeFirstRegistrationDate': 'true',
                'competitors': 'false',
                'features': 'true'
            }
            
            with self.at_vehicles_limiter:
                response = session.get(self.at_vehicles_url, params=params, headers=headers, timeout=15)
                
            if response.status_code == 200:
                data = response.json()
                vehicle = data.get('vehicle', {})
                metrics = data.get('vehicleMetrics', {}).get('retail', {})
                features = data.get('features', [])
                
                factory_features = [f.get('name', '') for f in features if f.get('factoryFitted')]
                
                return {
                    'at_success': True,
                    'at_make': vehicle.get('make', ''),
                    'at_model': vehicle.get('model', ''),
                    'at_derivative': vehicle.get('derivative', ''),
                    'at_derivative_id': vehicle.get('derivativeId', ''),
                    'at_first_reg_date': vehicle.get('firstRegistrationDate', ''),
                    'at_rating': metrics.get('rating', {}).get('value', ''),
                    'at_days_to_sell': metrics.get('daysToSell', {}).get('value', ''),
                    'at_market_condition': metrics.get('marketCondition', {}).get('value', ''),
                    'at_factory_features': ', '.join(factory_features),
                    'at_response_code': response.status_code
                }
            else:
                if response.status_code not in [404]:
                    self._log_real_api_error("AutoTrader_Vehicles", vrm, response.status_code, response.text)
                return {
                    'at_success': False,
                    'at_error': f'Status {response.status_code}',
                    'at_response_code': response.status_code
                }
                
        except Exception as e:
            logger.error(f"AutoTrader Vehicles exception for {vrm}: {str(e)}")
            return {
                'at_success': False,
                'at_error': str(e),
                'at_response_code': 0
            }

    def call_autotrader_valuations(self, session, vrm: str, derivative_id: str, first_reg_date: str, mileage: int, features: list) -> Dict[str, Any]:
        """Call AutoTrader Valuations API"""
        if shutdown_requested:
            return {'at_valuation': 'Shutdown requested', 'at_valuation_success': False}
            
        try:
            if not derivative_id:
                return {'at_valuation': 'No derivative ID', 'at_valuation_success': False}
                
            token = self.get_autotrader_token(session)
            headers = {'Authorization': f'Bearer {token}'}
            
            payload = {
                "vehicle": {
                    "derivativeId": derivative_id,
                    "firstRegistrationDate": first_reg_date,
                    "odometerReadingMiles": mileage,
                },
                "features": [{"name": f} for f in features if f][:10]
            }
            
            with self.at_valuations_limiter:
                response = session.post(f"{self.at_valuations_url}?advertiserId={self.at_advertiser_id}", 
                                      json=payload, headers=headers, timeout=15)
                
            if response.status_code == 200:
                data = response.json()
                valuation = data.get('valuations', {}).get('retail', {}).get('amountGBP', '')
                return {
                    'at_valuation': valuation,
                    'at_valuation_success': True
                }
            else:
                if response.status_code not in [400, 404]:
                    self._log_real_api_error("AutoTrader_Valuations", vrm, response.status_code, response.text)
                return {
                    'at_valuation': f'Error {response.status_code}',
                    'at_valuation_success': False
                }
                
        except Exception as e:
            logger.error(f"AutoTrader Valuations exception for {vrm}: {str(e)}")
            return {
                'at_valuation': f'Error: {str(e)}',
                'at_valuation_success': False
            }

    def _get_xml_text(self, root, xpath, namespaces):
        element = root.find(xpath, namespaces)
        return element.text if element is not None else ''

    def process_vehicle(self, row_index: int, row_data: Dict) -> Dict[str, Any]:
        """Process vehicle - FAST method with row tracking"""
        if shutdown_requested:
            return None
            
        vrm = str(row_data.get('REGNO', '')).strip()
        mileage = int(row_data.get('MILES', 0) or 0)
        
        if not vrm or mileage <= 0:
            result = row_data.copy()  # Include original data
            result.update({'row_index': row_index, 'error': 'Missing VRM or mileage'})
            return result
        
        session = self.get_session()
        result = row_data.copy()  # Start with original data
        result['row_index'] = row_index  # Track original position
        
        try:
            # Call CAP API
            cap_data = self.call_cap_vrm(session, vrm, mileage)
            result.update(cap_data)
            
            # Call AutoTrader Vehicles API
            at_vehicles = self.call_autotrader_vehicles(session, vrm, mileage)
            result.update(at_vehicles)
            
            # Call AutoTrader Valuations API if we got derivative ID
            if at_vehicles.get('at_success') and at_vehicles.get('at_derivative_id'):
                features = at_vehicles.get('at_factory_features', '').split(', ')
                at_valuations = self.call_autotrader_valuations(
                    session, vrm, at_vehicles['at_derivative_id'], 
                    at_vehicles['at_first_reg_date'], mileage, features
                )
                result.update(at_valuations)
            
        except Exception as e:
            result['error'] = str(e)
        finally:
            self.return_session(session)
        
        return result

def main():
    logger.info("Starting API speed test")
    logger.info(f"CAP: {CAP_RATE}/sec, AT_Vehicles: {AT_VEHICLES_RATE}/sec, AT_Valuations: {AT_VALUATIONS_RATE}/sec")
    logger.info(f"Input: {INPUT_FILE}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("Press Ctrl+C to stop")
    
    if not os.path.exists(INPUT_FILE):
        logger.error(f"Input file not found: {INPUT_FILE}")
        return
    
    # Load Excel file
    logger.info("Loading Excel file...")
    df = pd.read_excel(INPUT_FILE)
    logger.info(f"Loaded {len(df)} vehicles")
    
    # Initialize processor
    try:
        logger.info("Initializing processor...")
        processor = APIProcessor()
        logger.info("Processor ready")
    except Exception as e:
        logger.error(f"Failed to initialize: {e}")
        return
    
    start_time = time.time()
    
    logger.info("Starting processing...")
    
    # FAST processing with as_completed - CRITICAL for performance
    with ThreadPoolExecutor(max_workers=80) as executor:
        futures = []
        for idx, (_, row) in enumerate(df.iterrows()):
            if shutdown_requested:
                break
            future = executor.submit(processor.process_vehicle, idx, row.to_dict())
            futures.append(future)
        
        # Use as_completed for maximum performance
        results = []
        for i, future in enumerate(as_completed(futures)):
            if shutdown_requested:
                logger.info("Stopped by user")
                break
                
            result = future.result()
            if result:
                results.append(result)
            
            if (i + 1) % 50 == 0:
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed
                eta = (len(futures) - i - 1) / rate if rate > 0 else 0
                logger.info(f"Progress: {i+1}/{len(futures)} ({(i+1)/len(futures)*100:.1f}%) - Rate: {rate:.1f}/sec - ETA: {eta:.1f}s")
    
    total_time = time.time() - start_time
    total_vehicles = len(results)
    avg_rate = total_vehicles / total_time if total_time > 0 else 0
    
    logger.info(f"Complete! Time: {total_time:.1f}s, Vehicles: {total_vehicles}, Rate: {avg_rate:.1f}/sec")
    
    if results:
        at_successes = sum(1 for r in results if r.get('at_success'))
        cap_successes = sum(1 for r in results if r.get('cap_success'))
        
        logger.info(f"AutoTrader success: {at_successes}/{total_vehicles} ({at_successes/total_vehicles*100:.1f}%)")
        logger.info(f"CAP success: {cap_successes}/{total_vehicles} ({cap_successes/total_vehicles*100:.1f}%)")
        
        # Only show real API errors
        if api_error_stats:
            logger.error("API ERROR SUMMARY:")
            for error_type, count in api_error_stats.items():
                logger.error(f"  {error_type}: {count} occurrences")
        else:
            logger.info("No API server errors detected")
        
        # Sort by original row index to preserve order
        logger.info("Preserving original order...")
        results.sort(key=lambda x: x.get('row_index', 0))
        
        # Remove the row_index column before saving
        for result in results:
            result.pop('row_index', None)
        
        logger.info("Saving results...")
        results_df = pd.DataFrame(results)
        results_df.to_excel(OUTPUT_FILE, index=False)
        logger.info(f"Results saved to: {OUTPUT_FILE}")
    
    logger.info("Test complete")

if __name__ == "__main__":
    main()