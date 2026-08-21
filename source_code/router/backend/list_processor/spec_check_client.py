"""
HPI Trade Webservices Supplementary Enquiry — Spec Check (factory options).

A separate SOAP service from the CAP valuation calls in cap_client.py: different
host (wss.hpi.co.uk, not soap.cap.co.uk), different credentials (CustomerCode +
Initials + Password, not subscriber id + password) and different billing. Kept in
its own module for that reason rather than bolted onto CAPClient.

EVERY CALL IS A BILLABLE HPI CHECK. The request carries a primary product (HPI64,
the basic ID check) purely as the carrier for the AVX02 supplementary, and both
are charged. Nothing in this module should ever be called from a bulk path — the
Assess Vehicle page asks for one vehicle at a time, on a button, after the user
confirms. Results are stored so a VRM is only ever paid for once.

Only the live endpoint works: the credentials on account 0435063 are production
only, and pat-wss rejects them with fault 002. Verified 2026-08-21 against
DA66VGN, which returned base 36280, options 4575, rating 4 and 8 optional extras.
"""
import logging
import os
import threading
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests

from utils.singleton import Singleton
from utils.secrets_manager_client import get_secret

logger = logging.getLogger(__name__)

NS = "http://webservices.hpi.co.uk/SupplementaryEnquiryV1"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# Test (pat-wss) is listed in the documentation but our credentials are not
# registered on it. Left here so it is one line to switch if cap hpi ever issue
# test logins for this account.
LIVE_URL = "https://wss.hpi.co.uk/TradeSoap/services/SupplementaryEnquiryV1/"
TEST_URL = "https://pat-wss.hpi.co.uk/TradeSoap/services/SupplementaryEnquiryV1/"
SOAP_ACTION = "http://webservices.hpi.co.uk/SupplementaryEnquiryV1/parameterizedEnquiry"

# HPI64 is the basic ID check — the cheapest product that can carry a
# supplementary. Spec Check is not one of the products that requires the full
# HPI63 check, so there is no reason to pay for it.
PRIMARY_PRODUCT = "HPI64"
# AVX02 is the standard Spec Check. AVX03 is the franchised-dealer variant that
# skips your own makes and needs specific account setup by cap hpi.
SUPPLEMENTARY_PRODUCT = "AVX02"

# Terminal answers, not failures — retrying them just bills again.
STATUS_OK = 1
STATUS_UNAVAILABLE = 103
STATUS_BLOCKED_MAKE = 120

_REQUEST = """<soapenv:Envelope xmlns:soapenv="{soap_ns}">
  <soapenv:Header/>
  <soapenv:Body>
    <ParameterizedEnquiryRequest xmlns="{ns}">
      <Authentication>
        <SubscriberDetails>
          <CustomerCode>{customer}</CustomerCode>
          <Initials>{initials}</Initials>
          <Password>{password}</Password>
        </SubscriberDetails>
      </Authentication>
      <ParameterizedRequest>
        <Asset>
          <Vrm>{vrm}</Vrm>
          <Vin></Vin>
          <Reference>{reference}</Reference>
        </Asset>
        <ParameterList>
          <Parameter>
            <Name>SHOW_STANDARD_SPEC_CHECK_OPTIONS</Name>
            <Value>{standard}</Value>
          </Parameter>
        </ParameterList>
        <PrimaryProduct>
          <Code>{primary}</Code>
        </PrimaryProduct>
        <SupplementaryProduct>
          <Code>{supplementary}</Code>
        </SupplementaryProduct>
      </ParameterizedRequest>
    </ParameterizedEnquiryRequest>
  </soapenv:Body>
</soapenv:Envelope>"""


def _q(tag):
    return "{%s}%s" % (NS, tag)


def _text(parent, tag):
    if parent is None:
        return None
    el = parent.find(_q(tag))
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def _to_float(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


class SpecCheckError(Exception):
    """The request could not be completed. Distinct from a 103/120 answer,
    which is a valid response saying there is no data for this vehicle."""


class SpecCheckClient(metaclass=Singleton):
    def __init__(self):
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')
        self.customer_code = secrets.get('HPI_CUSTOMER_CODE')
        self.initials = secrets.get('HPI_INITIALS')
        self.password = secrets.get('HPI_PASSWORD')
        # The account's credentials only exist on live, so both environments
        # point there. Nothing calls this without an explicit user action, so a
        # dev run cannot bill by accident.
        self.url = LIVE_URL
        self._local = threading.local()

    @property
    def configured(self) -> bool:
        return bool(self.customer_code and self.initials and self.password)

    @property
    def _session(self):
        if not hasattr(self._local, 'session'):
            self._local.session = requests.Session()
        return self._local.session

    def spec_check(self, vrm: str, show_standard: bool = True,
                   reference: str = '') -> dict:
        """
        Run a Spec Check for one VRM. Billable — see the module docstring.

        Returns a dict with the headline figures, the options list, and a status
        saying whether data came back. Raises SpecCheckError on a SOAP fault or a
        network failure, so the caller can tell "no data for this car" (a normal
        result worth storing) from "the call did not work" (do not store).
        """
        if not self.configured:
            raise SpecCheckError(
                "HPI credentials are not set — add HPI_CUSTOMER_CODE, HPI_INITIALS "
                "and HPI_PASSWORD to the api_access secret."
            )

        clean_vrm = (vrm or '').replace(' ', '').upper()
        if not clean_vrm or len(clean_vrm) > 7:
            raise SpecCheckError(f"'{vrm}' is not a valid VRM for HPI (max 7 characters, no spaces).")

        body = _REQUEST.format(
            soap_ns=SOAP_NS, ns=NS,
            customer=escape(self.customer_code),
            initials=escape(self.initials),
            password=escape(self.password),
            vrm=escape(clean_vrm),
            reference=escape(reference or ''),
            standard='true' if show_standard else 'false',
            primary=PRIMARY_PRODUCT,
            supplementary=SUPPLEMENTARY_PRODUCT,
        )

        logger.info("[HPI] Spec Check request for %s (billable)", clean_vrm)
        try:
            response = self._session.post(
                self.url, data=body.encode('utf-8'), timeout=60,
                headers={'Content-Type': 'text/xml; charset=utf-8',
                         'SOAPAction': SOAP_ACTION},
            )
        except requests.exceptions.RequestException as e:
            # Deliberately no retry. A timeout may well have billed already, and
            # a second attempt would bill again for the same vehicle.
            raise SpecCheckError(f"Could not reach the HPI service: {e}") from e

        return self._parse(response.content, clean_vrm)

    def _parse(self, content: bytes, vrm: str) -> dict:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            raise SpecCheckError(f"HPI returned a response that could not be read: {e}") from e

        for fault in root.iter("{%s}Fault" % SOAP_NS):
            errors = []
            for err in fault.iter(_q("Error")):
                code = _text(err, "Code")
                desc = _text(err, "Description")
                errors.append(f"{code} {desc}".strip())
            if not errors:
                fs = fault.find("faultstring")
                errors = [fs.text if fs is not None else "unknown SOAP fault"]
            logger.warning("[HPI] Spec Check fault for %s: %s", vrm, '; '.join(errors))
            raise SpecCheckError('; '.join(errors))

        olr = next(root.iter(_q("OptionListResponse")), None)
        if olr is None:
            raise SpecCheckError(
                "HPI accepted the request but returned no Spec Check section — "
                "the account may not be licensed for AVX02."
            )

        status_code = _text(olr, "StatusCode")
        try:
            status_code = int(status_code)
        except (TypeError, ValueError):
            status_code = None

        options = []
        for node in olr.iter(_q("ModelOptionDetail")):
            short = _text(node, "ShortDescription")
            if not short:
                continue
            options.append({
                'short_description': short,
                'long_description': _text(node, "LongDescription"),
                'option_kind': _text(node, "OptionKind") or 'Standard',
                # Standard items come back as nil or 0; only Optional items
                # carry a real price.
                'price': _to_float(_text(node, "Price")),
            })

        result = {
            'vrm': vrm,
            'status_code': status_code,
            'status_message': _text(olr, "StatusMessage"),
            'base_price': _to_float(_text(olr, "BasePrice")),
            'options_price': _to_float(_text(olr, "OptionsPrice")),
            'total_price': _to_float(_text(olr, "TotalPrice")),
            'options_rating': _to_float(_text(olr, "OptionsRating")),
            'options': options,
            'available': status_code == STATUS_OK,
            'blocked_for_make': status_code == STATUS_BLOCKED_MAKE,
        }
        logger.info(
            "[HPI] Spec Check %s: status=%s options=%d base=%s options_price=%s rating=%s",
            vrm, status_code, len(options), result['base_price'],
            result['options_price'], result['options_rating'],
        )
        return result


def optional_options(result: dict) -> list:
    """Just the paid-for extras — what the buying team actually cares about."""
    return [o for o in (result.get('options') or []) if o.get('option_kind') == 'Optional']


def standard_options(result: dict) -> list:
    return [o for o in (result.get('options') or []) if o.get('option_kind') != 'Optional']
