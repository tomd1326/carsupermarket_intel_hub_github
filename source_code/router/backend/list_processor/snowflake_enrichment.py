# FILE PATH: source_code/router/backend/list_processor/snowflake_enrichment.py
# LAMBDA-OPTIMIZED: HARDCODED PROD + minimal logging + maximum speed

import logging
import time
from typing import Optional, List, Dict, Any, Tuple
import pandas as pd
import numpy as np
from utils.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)

def enrich_with_stock_sales(
    df: pd.DataFrame,
    capid_col: Optional[str] = None,
    environment: str = "prod",  # Parameter ignored - always PROD
    fill_missing_zero: bool = True
) -> pd.DataFrame:
    """
    LAMBDA-OPTIMIZED: Always uses PROD Snowflake with minimal logging
    Adds 7 columns: 5 CAPID-specific + 2 market-wide totals
    """
    start_time = time.time()
    
    # HARDCODED: Always PROD
    environment = "prod"
    
    if df.empty:
        return _add_empty_snowflake_columns(df)
    
    # Auto-detect CAPID column if not provided
    if capid_col is None:
        capid_col = _detect_capid_column(df)
        if capid_col is None:
            return _add_empty_snowflake_columns(df)
    
    # Get unique CAPIDs for bulk query
    unique_capids = _get_unique_capids(df, capid_col)
    if not unique_capids:
        return _add_empty_snowflake_columns(df)
    
    # Bulk query Snowflake for CAPID-specific data + market totals
    snowflake_data, market_totals = _bulk_query_snowflake(unique_capids, "prod")
    
    # Vectorized merge
    result_df = _vectorized_merge(df, snowflake_data, market_totals, capid_col, fill_missing_zero)
    
    # Minimal performance log
    total_time = time.time() - start_time
    logger.info(f"Snowflake enrichment: {len(df)} rows in {total_time:.2f}s")
    
    return result_df


def _detect_capid_column(df: pd.DataFrame) -> Optional[str]:
    """Auto-detect CAPID column"""
    capid_candidates = []
    
    for col in df.columns:
        col_lower = col.lower()
        if any(x in col_lower for x in ['capid', 'cap_id']):
            capid_candidates.append((col, df[col].count()))
    
    if capid_candidates:
        best_col = max(capid_candidates, key=lambda x: x[1])
        return best_col[0]
    
    return None


def _get_unique_capids(df: pd.DataFrame, capid_col: str) -> List[int]:
    """Extract unique valid CAPIDs"""
    capids = pd.to_numeric(df[capid_col], errors='coerce')
    capids = capids.dropna()
    capids = capids[capids > 0]
    unique_capids = capids.unique().astype(int).tolist()
    return unique_capids


def _bulk_query_snowflake(capids: List[int], environment: str) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Any]]:
    """
    SPEED-OPTIMIZED: Bulk query Snowflake for CAPID data + market totals
    Returns: (capid_data_dict, market_totals_dict)
    """
    if not capids:
        return {}, {}
    
    try:
        with SnowflakeClient("prod") as client:
            capids_str = ",".join(map(str, capids))
            
            # Query 1: CAPID-specific data (already aggregated in Snowflake)
            capid_sql = f"""
                SELECT 
                    CAP_ID,
                    STOCK,
                    SALES,
                    SALES_7_DAYS,
                    AVG_DAYS_IN_STOCK_ACKNOWLEDGED,
                    SALES_PERCENTAGE
                FROM PROD_CARSUPERMARKET_DB.product_vehicle_journey.stock_coverage
                WHERE CAP_ID IN ({capids_str})
            """
            
            capid_rows, capid_cols = client.execute(capid_sql)
            
            # Convert to lookup dictionary
            capid_data = {}
            for row in capid_rows:
                cap_id = int(row[0])
                capid_data[cap_id] = {
                    'stock': row[1] if row[1] is not None else 0,
                    'sales': row[2] if row[2] is not None else 0,
                    'sales_7_days': row[3] if row[3] is not None else 0,
                    'avg_days_in_stock_acknowledged': row[4] if row[4] is not None else 0,
                    'sales_percentage': row[5] if row[5] is not None else 0.0
                }
            
            # Query 2: Market-wide totals (single row, same for all)
            market_sql = """
                SELECT 
                    TOTAL_SALES,
                    TOTAL_STOCK
                FROM PROD_CARSUPERMARKET_DB.product_vehicle_journey.stock_coverage
                LIMIT 1
            """
            
            market_rows, market_cols = client.execute(market_sql)
            
            market_totals = {}
            if market_rows:
                market_totals = {
                    'total_sales': market_rows[0][0] if market_rows[0][0] is not None else 0,
                    'total_stock': market_rows[0][1] if market_rows[0][1] is not None else 0
                }
            
            return capid_data, market_totals
            
    except Exception as e:
        logger.error(f"Snowflake query failed: {str(e)}")
        return {}, {}


def _vectorized_merge(
    df: pd.DataFrame, 
    snowflake_data: Dict[int, Dict], 
    market_totals: Dict[str, Any],
    capid_col: str, 
    fill_missing_zero: bool
) -> pd.DataFrame:
    """SPEED-OPTIMIZED: High-performance vectorized merge"""
    if not snowflake_data:
        return _add_empty_snowflake_columns(df)
    
    # Create lookup DataFrame for CAPID-specific data
    lookup_data = []
    for capid, data in snowflake_data.items():
        lookup_data.append({
            capid_col: capid,
            'capid_stock': data['stock'],
            'capid_sales': data['sales'],
            'capid_sales_7_days': data['sales_7_days'],
            'capid_avg_days_in_stock_acknowledged': data['avg_days_in_stock_acknowledged'],
            'capid_sales_percentage': data['sales_percentage']
        })
    
    lookup_df = pd.DataFrame(lookup_data)
    
    # Ensure CAPID column is numeric
    df_copy = df.copy()
    df_copy[capid_col] = pd.to_numeric(df_copy[capid_col], errors='coerce')
    
    # Vectorized left join for CAPID-specific columns
    result_df = df_copy.merge(lookup_df, on=capid_col, how='left')
    
    # Fill missing values for CAPID-specific columns
    default_value = 0 if fill_missing_zero else np.nan
    result_df['capid_stock'] = result_df['capid_stock'].fillna(default_value)
    result_df['capid_sales'] = result_df['capid_sales'].fillna(default_value)
    result_df['capid_sales_7_days'] = result_df['capid_sales_7_days'].fillna(default_value)
    result_df['capid_avg_days_in_stock_acknowledged'] = result_df['capid_avg_days_in_stock_acknowledged'].fillna(default_value)
    result_df['capid_sales_percentage'] = result_df['capid_sales_percentage'].fillna(default_value)
    
    # Add market-wide totals (same value for all rows)
    result_df['total_sales'] = market_totals.get('total_sales', 0)
    result_df['total_stock'] = market_totals.get('total_stock', 0)
    
    return result_df


def _add_empty_snowflake_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add empty Snowflake columns when no data available"""
    result_df = df.copy()
    result_df['capid_stock'] = 0
    result_df['capid_sales'] = 0
    result_df['capid_sales_7_days'] = 0
    result_df['capid_avg_days_in_stock_acknowledged'] = 0
    result_df['capid_sales_percentage'] = 0.0
    result_df['total_sales'] = 0
    result_df['total_stock'] = 0
    return result_df
