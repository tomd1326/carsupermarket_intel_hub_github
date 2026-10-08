-- One-off: move Google Ads Campaigns targets from campaign-name keys to
-- campaign-ID keys. Run once, when the app change that keys targets by ID goes
-- live. Before that, the page reads name keys; after it, ID keys.
--
--   ga4:sessions:<name>                 -> ga4:sessions:<id>
--   ga4:events:<event_name>:<name>      -> ga4:events:<event_name>:<id>
--
-- Safe to re-run: once moved, no key ends with a name any more.

BEGIN;

WITH name_to_id (campaign_name, campaign_id) AS (
    VALUES
        ('.Vehicle Ads > CSM Audience', '20827229961'),
        ('.Vehicle Ads > Newcastle', '23128466804'),
        ('{TEST} Dealer > Regional > 2 > Sheffield, Barnsley & Worksop', '18957506351'),
        ('{TEST} Dealer > Regional > 3 > Grimsby, Scunthorpe, Hessle & Hull', '18957018866'),
        ('{TEST} Dealer > Regional > 4 > Lincoln', '18957506354'),
        ('Converters > Regional > 1 > Birmingham & Coventry', '24152845179')
        -- '.Vehicle Ads > CSM Audience Experiment': add a line here once its
        -- ID is known, or its targets stay under the old name key.
)
UPDATE chart_targets c
SET chart_key = LEFT(c.chart_key, LENGTH(c.chart_key) - LENGTH(m.campaign_name)) || m.campaign_id,
    updated_at = NOW()
FROM name_to_id m
WHERE (c.chart_key = 'ga4:sessions:' || m.campaign_name
       OR (c.chart_key LIKE 'ga4:events:%'
           AND RIGHT(c.chart_key, LENGTH(m.campaign_name) + 1) = ':' || m.campaign_name));

-- Check: nothing should be left under a name key.
SELECT DISTINCT chart_key
FROM chart_targets
WHERE chart_key LIKE 'ga4:%'
ORDER BY chart_key;

COMMIT;
