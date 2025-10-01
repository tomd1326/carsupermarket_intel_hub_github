# FILE PATH: source_code/router/backend/list_processor/snowflake_enrichment.py
# LAMBDA-OPTIMIZED: HARDCODED PROD + minimal logging + maximum speed

import logging
import time
from typing import Optional, List, Dict, Any
import pandas as pd
import numpy as np
from router.backend.utils.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)

def enrich_with_stock_sales(
    df: pd.DataFrame,
    capid_col: Optional[str] = None,
    environment: str = "prod",  # Parameter ignored - always PROD
    fill_missing_zero: bool = True
) -> pd.DataFrame:
    """
    LAMBDA-OPTIMIZED: Always uses PROD Snowflake with minimal logging
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
    
    # Bulk query Snowflake
    snowflake_data = _bulk_query_snowflake(unique_capids, "prod")  # Always PROD
    
    # Vectorized merge
    result_df = _vectorized_merge(df, snowflake_data, capid_col, fill_missing_zero)
    
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


def _bulk_query_snowflake(capids: List[int], environment: str) -> Dict[int, Dict[str, Any]]:
    """SPEED-OPTIMIZED: Bulk query Snowflake"""
    if not capids:
        return {}
    
    try:
        with SnowflakeClient("prod") as client:  # Force PROD
            capids_str = ",".join(map(str, capids))
            
            # FIXED SCHEMA: product_vehicle_journey
            sql = f"""
                SELECT 
                    CAP_ID,
                    SUM(COALESCE(STOCK, 0)) as total_stock,
                    SUM(COALESCE(SALES, 0)) as total_sales_30d,
                    SUM(COALESCE(SALES_7_DAYS, 0)) as total_sales_7d,
                    COUNT(*) as record_count
                FROM PROD_CARSUPERMARKET_DB.product_vehicle_journey.stock_coverage
                WHERE CAP_ID IN ({capids_str})
                GROUP BY CAP_ID
            """
            
            rows, cols = client.execute(sql)
            
            # Convert to lookup dictionary
            result = {}
            for row in rows:
                cap_id = int(row[0])
                result[cap_id] = {
                    'stock': int(row[1]),
                    'sales_30d': int(row[2]),
                    'sales_7d': int(row[3]),
                    'record_count': int(row[4])
                }
            
            return result
            
    except Exception as e:
        logger.error(f"Snowflake query failed: {str(e)}")
        return {}


def _vectorized_merge(df: pd.DataFrame, snowflake_data: Dict[int, Dict], capid_col: str, fill_missing_zero: bool) -> pd.DataFrame:
    """SPEED-OPTIMIZED: High-performance vectorized merge"""
    if not snowflake_data:
        return _add_empty_snowflake_columns(df)
    
    # Create lookup DataFrame
    lookup_data = []
    for capid, data in snowflake_data.items():
        lookup_data.append({
            capid_col: capid,
            'sf_stock': data['stock'],
            'sf_sales_30d': data['sales_30d'],
            'sf_sales_7d': data['sales_7d']
        })
    
    lookup_df = pd.DataFrame(lookup_data)
    
    # Ensure CAPID column is numeric
    df_copy = df.copy()
    df_copy[capid_col] = pd.to_numeric(df_copy[capid_col], errors='coerce')
    
    # Vectorized left join
    result_df = df_copy.merge(lookup_df, on=capid_col, how='left')
    
    # Fill missing values
    default_value = 0 if fill_missing_zero else np.nan
    result_df['sf_stock'] = result_df['sf_stock'].fillna(default_value)
    result_df['sf_sales_30d'] = result_df['sf_sales_30d'].fillna(default_value)
    result_df['sf_sales_7d'] = result_df['sf_sales_7d'].fillna(default_value)
    
    return result_df


def _add_empty_snowflake_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add empty Snowflake columns when no data available"""
    result_df = df.copy()
    result_df['sf_stock'] = 0
    result_df['sf_sales_30d'] = 0
    result_df['sf_sales_7d'] = 0
    return result_df