Table PROD_CARSUPERMARKET_DB.PRODUCT_SLT_KPIS.DAILY_SUMMARY (dbt model daily_summary__p). The KPI page's main table.
One row per day (column date, plus month 'YYYY-MM' and week 'YYYY-Www'), last 25 months. There is no dbt YAML for this table; this description was written from the model's SQL - check it.

Which days are complete
- Stock columns come from the early-morning stock snapshot, so today's stock is already complete.
- Everything else (orders, enquiries, calls, purchases) runs to yesterday. Today shows NULL for those, not 0.

Stock (cars on sale that day; courtesy cars excluded)
- stock_on_sale (advertised on the website), stock_on_sale_target, stock_on_sale_stores (at the 13 retail sites), stock_on_sale_rate (not interest-free), stock_on_sale_0pct (interest-free), stock_on_sale_rate_ev, total_stock.
- stock_45_plus_days, stock_60_plus_days, avg_days_in_stock.
- GPU M of stock on sale: avg_gpu_m_stock_on_sale, and by supplier channel avg_gpu_m_b2b2c, avg_gpu_m_consignment, avg_gpu_m_c2b2c_px, avg_gpu_m_c2b2c_other. This is the GPU M of cars on sale, not of the day's orders - there is no order GPU M in this table (use SALES_PERFORMANCE for that).
- avg_at_price_position_unsold_stock_rate / _0pct: average Autotrader price position of advertised cars not ordered that day.

Autotrader
- stock_advertised_on_at (PUBLISHED on Autotrader), stock_capped_on_at (over the package's advert limit, not shown), stock_eligible_for_at (advertised, not interest-free, not at GL LIVE / SF LIVE), stock_selectively_not_published_on_at (eligible but chosen not to publish, with at least two images).
- at_advert_views_total / _natural / _ppc, at_search_appearances_total / _natural / _ppc.

Orders (by order acknowledged date)
- orders, orders_target, orders_rate_total, orders_cash_total, orders_rate_0_15k / _15_25k / _25k_plus, orders_cash_0_15k / _15_25k / _25k_plus, orders_8_9 / orders_11_9 / orders_12_9 (by APR).
- orders_price_changed_last_7_days, orders_published_last_7_days.
- avg_at_price_position_orders_rate / _0pct (cancelled orders left out).
- orders_not_published_on_at and its averages (avg_dis_, avg_days_advertised_, avg_at_price_position_, avg_at_retail_rating_ ... _orders_not_published_on_at).
- purchases (cars stocked in that day), published (cars first advertised that day).

Website and enquiries
- website_sessions, website_sessions_target, website_total_users.
- valuations, valuations_part_exchange, website_enquire_now, website_reservations, website_move_closer, website_battery_soh, website_renewal, website_phone, walk_on, website_enquire_reserve_csmf, web_enquiries_if4c, whatsapp_leads.
- Portals: portal_motors, portal_cargurus, portal_carwow, portal_heycar, portal_cazoo.
- unique_customers, unique_customers_no_valuations, unique_customers_no_val_or_csmf.
- customers_with_permissions_cs / _csmf / _if4c.
- at_portal_enquiries (deduped) and at_portal_orders_from_enquiries (orders attributed back to the enquiry date). Conversion = orders / enquiries; the asymmetry is deliberate.

Calls and chat
- total_incoming_calls, unique_incoming_calls (number not seen in the previous 20 days).
- mediahawk_calls_ppc / _direct / _organic / _google_plus / _autotrader / _portal / _social / _other / _csmf, mediahawk_calls_autotrader_enquiries, mediahawk_calls_autotrader_orders.
- visitor_chat_total_chats, visitor_chat_lead_chats, visitor_chat_offline_messages, visitor_chat_if4c_total_chats, visitor_chat_if4c_lead_chats.
