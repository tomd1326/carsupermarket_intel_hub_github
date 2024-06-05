import datetime
import pytest

from list_processor.columns_mapper import UniversalColumnsMapper
from list_processor.structs import VehicleRow


class TestColumnMapper:
    @pytest.fixture
    def column_mapper(self):
        return UniversalColumnsMapper()

    @pytest.fixture(params=[
        # Scot stock
        {
            "input": {
                "Registration": "WM70BKY",
                "EM": "Now",
                "Plate": "70",
                "RegYear": "2021",
                "RegDate": "26/01/2021",
                "Make": "MG",
                "Description": "ZS 105kW Excite EV 45kWh 5dr Auto, 1 Previous Keeper (not VAT qualifying)",
                "SalesColour": "Pale Blue (Black Cloth)",
                "Odometer": "15687",
                "Price": "POA"
            },
            "expected": VehicleRow(
                vrm="WM70BKY",
                mileage=15687,
                registration_date=datetime.date(2021, 1, 1)
            )
        },
        # Spoticar
        {
            "input": {
                'Registration No_': 'WG22CKO',
                'Plate': '22:22',
                'Carline Code': 'ASTRA 22',
                'Full Description': 'ASTRA 22 1.2 Turbo (130PS) GS 5 Dr Hatch 6 Speed',
                'Mileage': '1231',
                'Advertised Price': '£19,150',
                'Option Value': '£0',
                'Current Discount': '£325',
                'Current Deal Price': '£18,825',
                'Bid': '',
                'CAP Code': '99097',
                'Colour Desc_': 'DIAMOND BLACK',
                'Fuel Desc_': 'Petrol',
                'VAT': 'Q2000',
                'Date of Registration': '30/04/2022',
                'Receipt Channel Code': 'F2ML',
                'Options': 'BUMPER FRT & RR BLACK, CARRIER-ROOF LUG, DELETE, BLACK HOOD PACK'
            },
            "expected": VehicleRow(
                vrm="WM70BKY",
                mileage=15687
            )
        },
        # LCVR
        {
            "input": {
                'Reg No.': 'AO72DVM',
                'Plate': '22/72',
                'Model': '1.0 Mild Hybrid Dolcevita [Part Leather] 3dr',
                'Colour': 'BOSSANOVAWHITE',
                'Miles': '20141',
                'CAP Live': '£9,550',
                'AT 100%': '£10,970',
                'AT Rating': '69',
                'Comment': 'Serviced at 10,133 and 19,602'
            },
            "expected": VehicleRow(
                vrm="AO72DVM",
                mileage=20141
            )
        },
        # Enterprise
        {
            "input": {
                'REGNO': 'KK19PVE',
                'YR-P': '19-19',
                'MAKE': 'ALFA',
                'MODL': 'GLTA',
                'TRIM': 'SPORT',
                'SERS': '5H14',
                'GEARBOX': '6M',
                'FUEL': 'P',
                'BHP': '120',
                'VERSION': '1.4 TB 120 BHP SPORT',
                'OPTIONAL EQUIP': 'TRADITIONAL ALFA WHITE PAINT^',
                'EXT COLOUR': 'ALFA WHITE GLOSS',
                'MILES': '31,617',
                'PRICE': '10400'
            },
            "expected": VehicleRow(
                vrm="KK19PVE",
                mileage=31617
            )
        },
        # Motability
        {
            "input": {
                'Reg No.': 'HW21JSZ',
                'Type': 'On Sale',
                'Condition': 'mfldirect Standard',
                'Buy Now Price': '13500',
                'Make': 'SKODA',
                'Model': 'OCTAVIA',
                'Variant': 'OCTAVIA 1.0 TSI SE Technology 5dr Hatchback',
                'CAP ID': '94077',
                'CAP %': '98.90%',
                'Transmission': 'Manual',
                'Fuel': 'Petrol',
                'Mileage': '18762',
                'Colour': 'Red',
                'Year': '2021',
                'Location': 'PO31 8BF',
                'Maintained': '',
                'Refurb Centre': 'No'
            },
            "expected": VehicleRow(
                vrm="HW21JSZ",
                mileage=18762
            )
        },
        # 1link
        {
            "input": {
                'Vendor Name': 'Volkswagen Financial Services (UK) Ltd',
                'Reg No.': 'PJ18PVO',
                'Make': 'ABARTH',
                'Model': '595 HATCHBACK',
                'Derivative': '1.4 T-Jet 165 Turismo 3dr',
                'VIN': 'ZFA3120000J905513',
                'CAP ID': '75462',
                'Odometer': '43125',
                'Odometer Type': 'Miles',
                'Date Of Registration': '2018-05-31',
                'Colour': 'RED',
                'Transmission': 'Manual',
                'FuelType': 'Petrol',
                'Doors': '3',
                'V5 Status': 'Yes',
                'Body Style': 'Hatchback',
                'Condition': 'BelowAverage',
                'Auction Grade': '5',
                'Additional Equipment': '',
                'VAT Status': 'NonQualified',
                'Location': 'LE17 5QS',
                'Buy Now Price': '9350',
                'Current Bid Price': '',
                'Bidding End Date Time': '',
                'Cap Valuation': '8875',
                'Buy Now Cap Percentage': '105',
                'Current Bid Cap Percentage': '',
                'Available For Purchase': 'Yes',
                'Ending Today': 'No',
                'Shortlisted': 'No',
                'Notes': '',
                'Sales Text': 'Ex-business use vehicle which may have had multiple users',
                'at_description': 'Abarth 595 1.4 T-Jet Turismo Hatchback 3dr Petrol Manual Euro 6 (165 bhp)'
            },
            "expected": VehicleRow(
                vrm="PJ18PVO",
                mileage=43125
            )
        }
    ])
    def row_data(self, request):
        return request.param

    def test_map_row(self, column_mapper, row_data):
        row = row_data["input"]
        expected = row_data["expected"]
        vehicle_row = column_mapper.map_row(row)
        assert vehicle_row.vrm == expected.vrm
        assert vehicle_row.mileage == expected.mileage
