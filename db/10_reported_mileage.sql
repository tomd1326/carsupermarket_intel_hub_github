-- Keep the exact odometer reading separately from the rounded valuation-cache
-- key. CAP valuations are rounded to 1,000 miles, but check history must show
-- the mileage the supplier supplied or the user entered.
ALTER TABLE vehicle_valuations
    ADD COLUMN IF NOT EXISTS reported_mileage INTEGER;

-- Older evaluator checks can recover their exact mileage from the supplier
-- listing recorded at the same import time. Old manual checks only retained
-- the rounded cache key, so they deliberately remain on that safe fallback.
UPDATE vehicle_valuations AS valuation
SET reported_mileage = (
    SELECT source_listing.mileage
    FROM supplier_vehicle_listings AS source_listing
    WHERE source_listing.vrm = valuation.vrm
      AND source_listing.mileage IS NOT NULL
      AND source_listing.imported_at BETWEEN valuation.fetched_at - INTERVAL '10 minutes'
                                          AND valuation.fetched_at + INTERVAL '10 minutes'
    ORDER BY ABS(EXTRACT(EPOCH FROM (source_listing.imported_at - valuation.fetched_at)))
    LIMIT 1
)
WHERE valuation.reported_mileage IS NULL
  AND valuation.source = 'evaluator'
  AND EXISTS (
      SELECT 1
      FROM supplier_vehicle_listings AS source_listing
      WHERE source_listing.vrm = valuation.vrm
        AND source_listing.mileage IS NOT NULL
        AND source_listing.imported_at BETWEEN valuation.fetched_at - INTERVAL '10 minutes'
                                            AND valuation.fetched_at + INTERVAL '10 minutes'
  );
