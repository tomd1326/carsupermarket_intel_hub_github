# list_processor/snowflake_enrichment.py
# REAL SNOWFLAKE IMPLEMENTATION using existing SnowflakeClient

import logging
import pandas as pd
from typing import Dict, List, Optional, Tuple
import re
from utils.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)

def enrich_with_stock_sales(df: pd.DataFrame, capid_col: str = None, environment: str = None, fill_missing_zero: bool = True) -> pd.DataFrame:
    """
    Enrich DataFrame with Snowflake data (Stock, 30-day Sales, 7-day Sales).
    
    Args:
        df: DataFrame to enrich
        capid_col: Specific CAPID column to use, or None for auto-detection
        environment: Environment ('dev' or 'prod') 
        fill_missing_zero: Whether to fill missing values with 0
    
    Returns:
        DataFrame with added columns: sf_stock, sf_sales, sf_sales_7d
    """
    logger.info(f"🔍 DEBUG: Starting Snowflake enrichment with {len(df)} rows")
    logger.info(f"🔍 DEBUG: Environment: {environment}")
    logger.info(f"🔍 DEBUG: DataFrame columns: {list(df.columns)}")
    logger.info(f"🔍 DEBUG: Requested CAPID column: {capid_col}")
    
    # Auto-detect CAPID column if not specified
    if capid_col is None:
        capid_col = detect_best_capid_column(df)
        
    if capid_col is None:
        logger.warning("❌ No suitable CAPID column found")
        return add_empty_snowflake_columns(df, fill_missing_zero)
    
    logger.info(f"✅ Using CAPID column: '{capid_col}'")
    return enrich_with_capid_column(df, capid_col, environment, fill_missing_zero)


def detect_best_capid_column(df: pd.DataFrame) -> Optional[str]:
    """
    Auto-detect the best CAPID column from various possible sources.
    
    Priority order:
    1. Source file CAPIDs: "CAP ID", "capid", "cap_id", etc.
    2. CAP-processed CAPIDs: "cap_capid" 
    3. Any column with "capid" in the name
    """
    logger.info("🔍 DEBUG: Auto-detecting CAPID column...")
    
    # Priority 1: Common source file CAPID column names
    source_capid_patterns = [
        "CAP ID",           # Motability, 1Link exact match
        "capid",            # Generic lowercase
        "cap_id",           # Generic with underscore
        "CAPID",            # Generic uppercase
        "Cap ID",           # Mixed case
    ]
    
    for pattern in source_capid_patterns:
        if pattern in df.columns:
            valid_count = count_valid_capids(df, pattern)
            logger.info(f"🔍 DEBUG: Found source CAPID column '{pattern}' with {valid_count} valid CAPIDs")
            if valid_count > 0:
                return pattern
    
    # Priority 2: CAP-processed columns (from cap_client.py output)
    cap_processed_patterns = ["cap_capid"]
    
    for pattern in cap_processed_patterns:
        if pattern in df.columns:
            valid_count = count_valid_capids(df, pattern)
            logger.info(f"🔍 DEBUG: Found CAP-processed column '{pattern}' with {valid_count} valid CAPIDs")
            if valid_count > 0:
                return pattern
    
    # Priority 3: Any column containing "capid" (case-insensitive)
    capid_candidates = [col for col in df.columns if 'capid' in col.lower()]
    logger.info(f"🔍 DEBUG: CAPID candidate columns found: {capid_candidates}")
    
    for col in capid_candidates:
        valid_count = count_valid_capids(df, col)
        logger.info(f"🔍 DEBUG: Column '{col}' has {valid_count} valid CAPIDs")
        if valid_count > 0:
            return col
    
    # Priority 4: Regex pattern matching (from UniversalColumnsMapper)
    for col in df.columns:
        if re.match(r"cap[\s_-]?(id|code)", col, re.IGNORECASE):
            valid_count = count_valid_capids(df, col)
            logger.info(f"🔍 DEBUG: Regex-matched column '{col}' has {valid_count} valid CAPIDs")
            if valid_count > 0:
                return col
    
    logger.warning("❌ No valid CAPID column found in DataFrame")
    return None


def count_valid_capids(df: pd.DataFrame, col: str) -> int:
    """Count valid CAPIDs in a column (numeric, 4-6 digits, not zero)."""
    try:
        series = pd.to_numeric(df[col], errors='coerce')
        valid_mask = (
            series.notna() & 
            (series != 0) & 
            (series >= 1000) &      # At least 4 digits
            (series <= 999999)      # At most 6 digits
        )
        return valid_mask.sum()
    except Exception as e:
        logger.warning(f"Error checking column '{col}': {e}")
        return 0


def enrich_with_capid_column(df: pd.DataFrame, capid_col: str, environment: str, fill_missing_zero: bool) -> pd.DataFrame:
    """Enrich using specific CAPID column with detailed logging."""
    logger.info(f"🔍 DEBUG: Processing {len(df)} rows using CAPID column '{capid_col}'")
    
    # Clean and validate CAPIDs
    df_clean = df.copy()
    original_capids = df_clean[capid_col].copy()
    
    # Show sample original values
    original_sample = original_capids.dropna().head(5).tolist()
    logger.info(f"🔍 DEBUG: Original CAPID values (sample): {original_sample}")
    
    # Convert to numeric, coerce errors to NaN
    df_clean[capid_col] = pd.to_numeric(df_clean[capid_col], errors='coerce')
    
    # Show sample converted values
    converted_sample = df_clean[capid_col].dropna().head(5).tolist()
    logger.info(f"🔍 DEBUG: After numeric conversion (sample): {converted_sample}")
    
    # Filter valid CAPIDs
    valid_mask = (
        df_clean[capid_col].notna() & 
        (df_clean[capid_col] != 0) & 
        (df_clean[capid_col] >= 1000) &     # Reasonable CAPID range (at least 4 digits)
        (df_clean[capid_col] <= 999999)     # At most 6 digits
    )
    
    valid_capids = df_clean[valid_mask][capid_col].astype(int).tolist()
    logger.info(f"🔍 DEBUG: Valid CAPIDs after cleaning: {len(valid_capids)} out of {len(df)} rows")
    
    if len(valid_capids) == 0:
        logger.warning("❌ No valid CAPIDs found after cleaning")
        return add_empty_snowflake_columns(df, fill_missing_zero)
    
    logger.info(f"🔍 DEBUG: Valid CAPIDs to query: {valid_capids}")
    
    # Get Snowflake data
    try:
        snowflake_data = get_snowflake_data(valid_capids, environment)
        logger.info(f"🔍 DEBUG: Snowflake returned {len(snowflake_data)} records")
        
        if snowflake_data:
            # Show sample of returned data
            for capid, data in list(snowflake_data.items())[:3]:
                logger.info(f"🔍 DEBUG: CAPID {capid} -> {data}")
        
        return merge_snowflake_data(df_clean, capid_col, snowflake_data, fill_missing_zero)
        
    except Exception as e:
        logger.error(f"❌ Snowflake query failed: {str(e)}")
        logger.error(f"❌ Full error details: {repr(e)}")
        return add_empty_snowflake_columns(df, fill_missing_zero)


def get_snowflake_data(capids: List[int], environment: str) -> Dict[int, Dict]:
    """Query Snowflake for real CAPID data from stock_coverage table using existing SnowflakeClient."""
    logger.info(f"🔍 DEBUG: Querying Snowflake stock_coverage table for {len(capids)} CAPIDs in {environment} environment")
    
    try:
        # Use existing SnowflakeClient with proper credentials from Secrets Manager
        with SnowflakeClient(environment=environment or "prod") as sf_client:
            logger.info("✅ SnowflakeClient connected successfully")
            
            # Build SQL query - aggregate by CAP_ID since there are multiple rows per CAPID
            capids_str = ','.join(map(str, capids))
            query = f"""
            SELECT 
                CAP_ID,
                SUM(STOCK) as total_stock,
                SUM(SALES) as total_sales_30d,
                SUM(SALES_7_DAYS) as total_sales_7d,
                COUNT(*) as record_count
            FROM stock_coverage 
            WHERE CAP_ID IN ({capids_str})
            GROUP BY CAP_ID
            ORDER BY CAP_ID
            """
            
            logger.info(f"🔍 DEBUG: Executing SQL query for {len(capids)} CAPIDs")
            logger.info(f"🔍 DEBUG: Query: {query[:200]}...")  # Show first 200 chars
            
            # Execute query and get DataFrame
            result_df = sf_client.execute_df(query)
            
            logger.info(f"✅ Query executed successfully, got {len(result_df)} result rows")
            
            if len(result_df) == 0:
                logger.warning("⚠️  No results returned from stock_coverage table")
                return {}
            
            # Convert DataFrame to dictionary
            snowflake_data = {}
            for _, row in result_df.iterrows():
                cap_id = int(row['CAP_ID'])
                stock = int(row['TOTAL_STOCK']) if pd.notna(row['TOTAL_STOCK']) else 0
                sales_30d = int(row['TOTAL_SALES_30D']) if pd.notna(row['TOTAL_SALES_30D']) else 0
                sales_7d = int(row['TOTAL_SALES_7D']) if pd.notna(row['TOTAL_SALES_7D']) else 0
                record_count = int(row['RECORD_COUNT']) if pd.notna(row['RECORD_COUNT']) else 0
                
                snowflake_data[cap_id] = {
                    'stock': stock,
                    '30_day_sales': sales_30d,
                    '7_day_sales': sales_7d,
                    'record_count': record_count
                }
                
                logger.info(f"🔍 DEBUG: CAPID {cap_id}: Stock={stock}, Sales30d={sales_30d}, Sales7d={sales_7d} (from {record_count} records)")
            
            logger.info(f"✅ Successfully retrieved data for {len(snowflake_data)} CAPIDs from Snowflake")
            
            # Show which CAPIDs had no data
            missing_capids = set(capids) - set(snowflake_data.keys())
            if missing_capids:
                logger.warning(f"⚠️  No Snowflake data found for CAPIDs: {sorted(missing_capids)}")
            
            return snowflake_data
        
    except Exception as e:
        logger.error(f"❌ Snowflake query error: {str(e)}")
        logger.error(f"❌ Full error details: {repr(e)}")
        return {}


def merge_snowflake_data(df: pd.DataFrame, capid_col: str, snowflake_data: Dict[int, Dict], fill_missing_zero: bool) -> pd.DataFrame:
    """Merge Snowflake data back into DataFrame with detailed logging."""
    logger.info(f"🔍 DEBUG: Merging Snowflake data for {len(snowflake_data)} CAPIDs")
    
    result_df = df.copy()
    
    # Add Snowflake columns with correct names (sf_stock, sf_sales, sf_sales_7d)
    default_value = 0 if fill_missing_zero else None
    result_df['sf_stock'] = default_value
    result_df['sf_sales'] = default_value      # 30-day sales
    result_df['sf_sales_7d'] = default_value   # 7-day sales
    
    # Merge data row by row
    merged_count = 0
    for idx, row in result_df.iterrows():
        capid = row[capid_col]
        
        if pd.notna(capid):
            capid_int = int(capid)
            logger.info(f"🔍 DEBUG: Row {idx}: Processing CAPID {capid_int}")
            
            if capid_int in snowflake_data:
                data = snowflake_data[capid_int]
                result_df.at[idx, 'sf_stock'] = data.get('stock', default_value)
                result_df.at[idx, 'sf_sales'] = data.get('30_day_sales', default_value)
                result_df.at[idx, 'sf_sales_7d'] = data.get('7_day_sales', default_value)
                merged_count += 1
                
                logger.info(f"✅ DEBUG: Merged CAPID {capid_int}: Stock={data.get('stock')}, 30d={data.get('30_day_sales')}, 7d={data.get('7_day_sales')}")
            else:
                logger.warning(f"⚠️  CAPID {capid_int} not found in Snowflake data - using defaults")
        else:
            logger.warning(f"⚠️  Row {idx}: Invalid CAPID value: {capid}")
    
    logger.info(f"✅ Successfully merged Snowflake data for {merged_count} out of {len(result_df)} rows")
    
    # Show final column summary
    stock_values = result_df['sf_stock'].value_counts().head(5)
    logger.info(f"🔍 DEBUG: Final sf_stock value distribution: {dict(stock_values)}")
    
    return result_df


def add_empty_snowflake_columns(df: pd.DataFrame, fill_missing_zero: bool) -> pd.DataFrame:
    """Add empty Snowflake columns as fallback."""
    result_df = df.copy()
    default_value = 0 if fill_missing_zero else None
    
    result_df['sf_stock'] = default_value
    result_df['sf_sales'] = default_value      # 30-day sales  
    result_df['sf_sales_7d'] = default_value   # 7-day sales
    
    logger.info(f"📋 Added empty Snowflake columns with default value: {default_value}")
    return result_df