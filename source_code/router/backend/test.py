# Full path: C:\Users\tom.drayson\OneDrive - Motor Depot\Data Engineering\Buying Tool test files\test.py

#!/usr/bin/env python3
"""
API Speed Test Script - Optimized for Immediate Full Speed
=========================================================
Pre-warms all connections and caches to eliminate cold start delays.
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
from typing import Dict, Any, Optional
from collections import defaultdict

import pandas as pd
import requests
import xml.etree.ElementTree as ET
import boto3
from botocore.config import Config
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# API Rate Configuration - PUSHING LIMITS
AT_VEHICLES_RATE = 100      # AutoTrader Vehicles API (above advised)
AT_VALUATIONS_RATE = 100    # AutoTrader Valuations API (above advised)
CAP_RATE = 300              # CAP API (increased too)

# File paths
INPUT_FILE = r"C:\Users\tom.drayson\OneDrive - Motor Depot\Data Engineering\Buying Tool test files\Stock 29th Sept.xlsx"
OUTPUT_FILE = r"C:\Users\tom.drayson\OneDrive - Motor Depot\Data Engineering\Buying Tool test files\Stock_29th_Sept_API_RESULTS.xlsx"
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
    """Create an HTTP session with connection pooling and retry strategies"""
    session = requests.Session()
    
    # Configure retry strategy
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    
    # Configure HTTP adapter with connection pooling
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=100,
        pool_maxsize=100,
        pool_block=False
    )
    
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    # Keep connections alive
    session.headers.update({'Connection': 'keep-alive'})
    
    return session

# Secrets Manager Client with caching
_cached_secrets = {}

def _infer_region(secret_name: str, default_region: str) -> str:
    if secret_name.startswith("prod/"):
        return "eu-west-1"
    if secret_name.startswith("dev/"):
        return "us-east-2"
    return default_region

def get_secret(secret_name: str, region_name: Optional[str] = None) -> dict:
    # Use cached secrets to avoid repeated AWS calls
    if secret_name in _cached_secrets:
        logger.debug(f"Using cached secret '{secret_name}'")
        return _cached_secrets[secret_name]
        
    env_region = os.getenv("AWS_REGION", "eu-west-1")
    chosen_region = region_name or _infer_region(secret_name, env_region)

    logger.debug(f"Getting secret '{secret_name}' from region '{chosen_region}'")

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
    
    # Cache for future use
    _cached_secrets[secret_name] = secret_data
    logger.debug(f"Cached secret '{secret_name}'")
    
    return secret_data

# Silent Rate Limiter - NO LOGGING
class SilentRateLimiter:
    """Rate limiter with no internal logging"""
    def __init__(self, rate_limit: int, name: str = ""):
        self.rate_limit = rate_limit
        self.name = name
        self.calls = []
        self.lock = threading.Lock()

    def __enter__(self):
        if shutdown_requested:
            return
            
        with self.lock:
            now = time.time()
            # Remove calls older than 1 second
            self.calls = [call_time for call_time in self.calls if now - call_time < 1.0]
            
            # Wait if we're at the rate limit (NO LOGGING)
            if len(self.calls) >= self.rate_limit:
                sleep_time = 1.0 - (now - self.calls[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    # Clean up again after sleeping
                    now = time.time()
                    self.calls = [call_time for call_time in self.calls if now - call_time < 1.0]
            
            # Record this call
            self.calls.append(now)

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

# Pre-warmed API Tester Class
class APITester:
    def __init__(self):
        # AutoTrader setup
        self.at_host = "https://api.autotrader.co.uk"
        self.at_auth_url = f"{self.at_host}/authenticate"
        self.at_vehicles_url = f"{self.at_host}/vehicles"
        self.at_valuations_url = f"{self.at_host}/valuations"
        
        # CAP setup
        self.cap_vrm_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"
        
        # Silent rate limiters - NO INTERNAL LOGGING
        self.at_vehicles_limiter = SilentRateLimiter(AT_VEHICLES_RATE, "AT_Vehicles")
        self.at_valuations_limiter = SilentRateLimiter(AT_VALUATIONS_RATE, "AT_Valuations")
        self.cap_limiter = SilentRateLimiter(CAP_RATE, "CAP")
        
        # Real API error counters
        self.real_api_errors = defaultdict(int)
        
        # Pre-warmed HTTP sessions pool
        self.session_pool = []
        self.session_lock = threading.Lock()
        
        # Auth token with pre-warming
        self.at_token = None
        self.token_expires = None
        self.token_lock = threading.Lock()
        
        # Initialize everything
        self._load_credentials()
        self._prewarm_system()
        
        logger.info(f"API Tester initialized and pre-warmed - AT_Vehicles: {AT_VEHICLES_RATE}/sec, AT_Valuations: {AT_VALUATIONS_RATE}/sec, CAP: {CAP_RATE}/sec")

    def _load_credentials(self):
        try:
            env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
            logger.info(f"Loading API credentials for {env} environment...")
            
            secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')
            
            self.at_key = secrets['AUTOTRADER_KEY']
            self.at_secret = secrets['AUTOTRADER_SECRET']
            self.at_advertiser_id = secrets['ADVERTISER_ID']
            
            self.cap_subscriber_id = secrets['CAP_SUBSCRIBER_ID']
            self.cap_password = secrets['CAP_PASSWORD']
            
            logger.info("API credentials loaded and cached")
            
        except Exception as e:
            logger.error(f"Failed to load API credentials: {e}")
            raise Exception("Missing API credentials")

    def _prewarm_system(self):
        """Pre-warm all connections and authenticate"""
        logger.info("Pre-warming system for maximum performance...")
        
        # Create initial session pool
        logger.info("Creating optimized HTTP session pool...")
        for _ in range(20):  # Pool of 20 sessions
            session = create_optimized_session()
            self.session_pool.append(session)
        
        # Pre-authenticate with AutoTrader
        logger.info("Pre-authenticating with AutoTrader...")
        try:
            session = self.get_session()
            self._authenticate_autotrader(session)
            self.return_session(session)
            logger.info("AutoTrader pre-authentication successful")
        except Exception as e:
            logger.warning(f"AutoTrader pre-authentication failed: {e}")
        
        # Pre-warm connections to all APIs
        logger.info("Pre-warming API connections...")
        self._prewarm_connections()
        
        logger.info("System pre-warming complete - ready for full speed!")

    def _prewarm_connections(self):
        """Make test calls to warm up connections"""
        def warm_api(url, data=None):
            try:
                session = self.get_session()
                if data:
                    session.post(url, data=data, timeout=5)
                else:
                    session.get(url, timeout=5)
                self.return_session(session)
            except:
                pass  # Expected to fail, just warming connections
        
        # Warm up AutoTrader endpoints
        threading.Thread(target=warm_api, args=(self.at_vehicles_url,)).start()
        threading.Thread(target=warm_api, args=(self.at_valuations_url,)).start()
        
        # Warm up CAP endpoint
        threading.Thread(target=warm_api, args=(self.cap_vrm_url, {'test': 'data'})).start()
        
        time.sleep(0.5)  # Give threads time to establish connections

    def get_session(self):
        """Get an optimized session from the pool"""
        with self.session_lock:
            if self.session_pool:
                return self.session_pool.pop()
            else:
                # Create new session if pool is empty
                return create_optimized_session()

    def return_session(self, session):
        """Return a session to the pool"""
        with self.session_lock:
            if len(self.session_pool) < 50:  # Don't let pool grow too large
                self.session_pool.append(session)
            else:
                session.close()

    def _authenticate_autotrader(self, session):
        """Pre-authenticate with AutoTrader"""
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
        """Get AutoTrader token (with pre-warming, this should be instant)"""
        with self.token_lock:
            if self.token_expires and datetime.datetime.now() < self.token_expires:
                return self.at_token
        
        # Need to re-authenticate
        with self.at_vehicles_limiter:
            return self._authenticate_autotrader(session)

    def _log_real_api_error(self, api_name: str, vrm: str, status_code: int, response_text: str = ""):
        """Log ONLY real API server errors"""
        error_key = f"{api_name}_{status_code}"
        self.real_api_errors[error_key] += 1
        api_error_stats[error_key] += 1
        
        # Only log actual server errors
        if status_code == 429:
            logger.error(f"API RATE LIMIT: {api_name} returned 429 (Too Many Requests) for VRM {vrm}")
            logger.error(f"Response: {response_text[:300]}")
        elif status_code in [503, 502, 504, 500]:
            logger.error(f"API SERVER ERROR: {api_name} returned {status_code} for VRM {vrm}")
            logger.error(f"Response: {response_text[:300]}")
        elif status_code == 401:
            logger.error(f"API AUTH ERROR: {api_name} returned 401 (Unauthorized) for VRM {vrm}")
        elif status_code == 403:
            logger.error(f"API FORBIDDEN: {api_name} returned 403 (Forbidden) for VRM {vrm}")
        elif status_code >= 400:
            logger.warning(f"API CLIENT ERROR: {api_name} returned {status_code} for VRM {vrm}")

    def call_autotrader_vehicles(self, session, vrm: str, mileage: int) -> Dict[str, Any]:
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
                # Only log if it's a real API error (not just "not found")
                if response.status_code not in [404]:  # 404 is normal for unknown vehicles
                    self._log_real_api_error("AutoTrader_Vehicles", vrm, response.status_code, response.text)
                return {
                    'at_success': False,
                    'at_error': f'Status {response.status_code}',
                    'at_response_code': response.status_code
                }
                
        except Exception as e:
            logger.error(f"AutoTrader Vehicles exception for VRM {vrm}: {str(e)}")
            return {
                'at_success': False,
                'at_error': str(e),
                'at_response_code': 0
            }

    def call_autotrader_valuations(self, session, vrm: str, derivative_id: str, first_reg_date: str, mileage: int, features: list) -> Dict[str, Any]:
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
                # Only log real API errors, not normal business logic errors
                if response.status_code not in [400, 404]:
                    self._log_real_api_error("AutoTrader_Valuations", vrm, response.status_code, response.text)
                return {
                    'at_valuation': f'Error {response.status_code}',
                    'at_valuation_success': False
                }
                
        except Exception as e:
            logger.error(f"AutoTrader Valuations exception for VRM {vrm}: {str(e)}")
            return {
                'at_valuation': f'Error: {str(e)}',
                'at_valuation_success': False
            }

    def call_cap_vrm(self, session, vrm: str, mileage: int) -> Dict[str, Any]:
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
                # Only log real server errors for CAP
                if response.status_code >= 500:
                    self._log_real_api_error("CAP_VRM", vrm, response.status_code, response.text)
                return {
                    'cap_success': False,
                    'cap_error': f'Status {response.status_code}',
                    'cap_response_code': response.status_code
                }
                
        except Exception as e:
            logger.error(f"CAP VRM exception for VRM {vrm}: {str(e)}")
            return {
                'cap_success': False,
                'cap_error': str(e),
                'cap_response_code': 0
            }

    def _get_xml_text(self, root, xpath, namespaces):
        element = root.find(xpath, namespaces)
        return element.text if element is not None else ''

    def process_vehicle(self, row_data: Dict) -> Dict[str, Any]:
        if shutdown_requested:
            return None
            
        vrm = str(row_data.get('REGNO', '')).strip()
        mileage = int(row_data.get('MILES', 0) or 0)
        
        if not vrm or mileage <= 0:
            return {'vrm': vrm, 'mileage': mileage, 'error': 'Missing VRM or mileage'}
        
        session = self.get_session()
        result = {'vrm': vrm, 'mileage': mileage}
        
        try:
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
            
            # Call CAP VRM API
            cap_data = self.call_cap_vrm(session, vrm, mileage)
            result.update(cap_data)
            
        except Exception as e:
            result['error'] = str(e)
        finally:
            self.return_session(session)
        
        return result

def main():
    logger.info(f"Starting OPTIMIZED API Speed Test - No Cold Start Delays!")
    logger.info(f"AT_Vehicles: {AT_VEHICLES_RATE}/sec, AT_Valuations: {AT_VALUATIONS_RATE}/sec, CAP: {CAP_RATE}/sec")
    logger.info(f"Will only show REAL API server errors (429, 503, etc.)")
    logger.info(f"Input: {INPUT_FILE}")
    logger.info(f"Output: {OUTPUT_FILE}")
    logger.info("Press Ctrl+C to stop")
    
    if not os.path.exists(INPUT_FILE):
        logger.error(f"Input file not found: {INPUT_FILE}")
        return
    
    # Pre-load Excel file
    logger.info("Pre-loading Excel file...")
    df = pd.read_excel(INPUT_FILE)
    logger.info(f"Loaded {len(df)} vehicles")
    
    # Initialize and pre-warm API tester
    try:
        logger.info("Initializing and pre-warming API systems...")
        tester = APITester()
        logger.info("Pre-warming complete - ready for full speed processing!")
    except Exception as e:
        logger.error(f"Failed to initialize: {e}")
        return
    
    start_time = time.time()
    results = []
    
    logger.info("Starting processing at full speed...")
    
    with ThreadPoolExecutor(max_workers=80) as executor:
        futures = []
        for idx, row in df.iterrows():
            if shutdown_requested:
                break
            future = executor.submit(tester.process_vehicle, row.to_dict())
            futures.append(future)
        
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
        
        # Only show REAL API errors
        if api_error_stats:
            logger.error("REAL API ERROR SUMMARY:")
            for error_type, count in api_error_stats.items():
                logger.error(f"  {error_type}: {count} occurrences")
        else:
            logger.info("No API server errors detected!")
        
        logger.info("Saving results...")
        results_df = pd.DataFrame(results)
        results_df.to_excel(OUTPUT_FILE, index=False)
        logger.info(f"Results saved to: {OUTPUT_FILE}")
    
    logger.info("Test complete!")

if __name__ == "__main__":
    main()