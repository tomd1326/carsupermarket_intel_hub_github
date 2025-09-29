# source_code/router/backend/list_processor/enhanced_list_processor.py

import logging
import pandas as pd
from typing import Optional

from list_processor.columns_mapper import get_columns_mapper
from list_processor.autotrader_client import AutoTraderClient
from list_processor.cap_client import CAPClient
from list_processor.snowflake_enrichment import enrich_with_stock_sales

log = logging.getLogger(__name__)


def process_vehicle_list(
    df: pd.DataFrame,
    supplier_name: str,
    rate_limit: int = 40,
    enable_snowflake: bool = True,
    snowflake_env: str = "prod"
) -> pd.DataFrame:
    """
    Process a vehicle list with smart CAPID handling.
    
    Args:
        df: Input DataFrame
        supplier_name: Supplier name ("Enterprise", "Motability", "1link", "Universal")
        rate_limit: API requests per second
        enable_snowflake: Whether to enrich with Snowflake data
        snowflake_env: Snowflake environment
        
    Returns:
        Enhanced DataFrame
    """
    log.info(f"Processing vehicle list: supplier={supplier_name}, rows={len(df)}")
    
    # Step 1: Map columns
    column_mapper = get_columns_mapper(supplier_name)
    mapped_rows = []
    
    for idx, row in df.iterrows():
        vehicle_row = column_mapper.map_row(row.to_dict())
        if vehicle_row is not None:
            mapped_rows.append(vehicle_row.dict())
    
    if not mapped_rows:
        return pd.DataFrame()
        
    mapped_df = pd.DataFrame(mapped_rows)
    
    # Step 2: Enrich with CAP IDs for rows that need them
    missing_capid = mapped_df['capid'].isna()
    if missing_capid.any():
        log.info(f"Performing VRM lookup for {missing_capid.sum()} rows")
        cap_client = CAPClient(rps=rate_limit)
        mapped_df = cap_client.enrich_dataframe_with_capid(
            mapped_df,
            vrm_col="vrm",
            registration_date_col="registration_date",
            mileage_col="mileage"
        )
        # Merge VRM-sourced CAPIDs
        if 'capid_vrm' in mapped_df.columns:
            mapped_df['capid'] = mapped_df['capid'].fillna(mapped_df['capid_vrm'])
    
    # Step 3: AutoTrader enrichment (if needed)
    autotrader_client = AutoTraderClient()
    at_df = autotrader_client.process_file(mapped_df, column_mapper)
    result_df = pd.concat([mapped_df, at_df], axis=1)
    
    # Step 4: Snowflake enrichment with auto-detection
    if enable_snowflake:
        result_df = enrich_with_stock_sales(
            result_df,
            capid_col=None,  # Auto-detect
            environment=snowflake_env
        )
    
    log.info(f"Processing complete: {len(result_df)} rows, {len(result_df.columns)} columns")
    return result_df