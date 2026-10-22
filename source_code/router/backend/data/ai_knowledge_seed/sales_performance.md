Table PROD_CARSUPERMARKET_DB.PRODUCT_SALES_PERFORMANCE.SALES_PERFORMANCE (dbt model sales_performance__p).
One row per sale (invoice), with margin, finance, products, standard costs and part exchange. Covers acknowledged, paid and cancelled invoices. Key: invoice_guid.
Descriptions below come from the dbt YAML (_sales_performance__models.yml).

Status
- invoice_status: STATUS_ACKNOWLEDGED, STATUS_PAID, STATUS_CANCELLED.
- deal_status: human-readable status - Acknowledged, Delivered, Cancelled, Invoice Credited.
- invoice_credited: whether the invoice has been credited. invoice_type: e.g. VEHICLE_SALES_INVOICE.

Dates
- order_acknowledged_at / _date / _week / _week_number / _month (first day of month): when the order was acknowledged.
- invoice_posted_at / _date / _week / _week_number / _month: when the invoice was posted.
- delivery_lead_time: days between order acknowledged and invoice posted.
- date_stock_in, date_stock_out, date_first_registered, date_first_advertised, date_last_advertised (start of the run on sale that led to the order), cancellation_date, customer_collection_date.

Days in stock / advertised
- acknowledged_days_in_stock (+ _band), invoiced_days_in_stock (+ _band), acknowledged_days_advertised (+ _band; only days actually on the website).

Vehicle
- vrm, vehicle_id, stock_id, cap_id, cap_database (car / LCV), make, model, make_model, derivative, cap_manufacturer, cap_range, cap_model, age_band, mileage, mileage_band, colour, fuel_type, fuel_type_category (Petrol, Diesel, Electric, Hybrid, Other).
- supplier_name, supplier_channel (sourcing channel), interest_free (advertised with interest-free finance), vat_scheme, vat_qualifying.

Sites and people
- deal_site_clean (deal site, prep centres remapped to collection centres), current_vehicle_site, vehicle_site_at_order, transfer_type, sales_advisor (+ _site, _site_category, _department, _brand), assigned_to, assignee_site, invoiced_by.

Prices (all inc VAT unless _ex_vat)
- purchase_price_inc_vat / _ex_vat (adjusted for consignment).
- sale_price_inc_vat / _ex_vat, original_price_inc_vat (before discounting), discount_inc_vat / _ex_vat, discount_type, discount_note.
- sale_price_after_discount; sale_price_after_discount_inc_admin_fee (what the customer paid, admin fee as charged).
- sale_price_before_discount_inc_admin_fee (list price plus fee as charged).
- advertised_price_inc_vat_inc_admin_fee (price before discount plus the standard admin fee - the advertised price).
- price_band (price band at point of sale), last_price_change_at / _amount.
- sales_type: finance product type (Cash, Rate, 0%). sales_type_bucket: '0%', 'Unknown' or 'Rate/Cash'.

Margin
- chassis_margin_before_subsidy, finance_subsidy, chassis_margin_after_subsidy, write_down, vehicle_costs (reconditioning), net_siv (net stock in value), metal_margin (chassis margin after subsidy, write down and vehicle costs), margin_vat_cost.
- gpu_m (metal margin after GPU M standard costs), gpu_s (product and finance margin after GPU S standard costs), gpu_ms (GPU M + GPU S), gpu_b (bonus allocation), gpu_msb, gpu_m_costs, gpu_s_costs, grand_total_margin, roi_percentage (GPU MS as % of net SIV).
- Standard cost allocations: late_costs, std_prep_centre_rent, std_transport_salaries, std_external_transport_costs, std_delivery_sale, std_damage_claims_rebates_other, std_credited_deals_carried_over, std_subcontracted_valet_costs, std_write_downs, std_finance_debit_backs, std_product_debit_backs, std_service_dept_efficiency, std_rfl_v5_costs.

Valuations and Autotrader
- cap_live_clean_ / cap_live_retail_ + latest / purchase / advertised; sale_price_percent_cap_live_retail.
- autotrader_latest_retail_valuation (at time of order), autotrader_latest_retail_rating (0-100, + _band), autotrader_price_position (price paid inc admin fee / Autotrader valuation, + _band), autotrader_advert_status, autotrader_unpublished.

Finance
- finance_company, finance_type (HP, PCP), finance_term, finance_apr, finance_deposit, finance_balance, finance_monthly_payment, finance_residual_value, finance_commission_rate, finance_commission, finance_subsidy_calculated_amount / _rate.

Products
- For each product: product_cost_<p>, product_sell_<p>, product_discount_<p>, product_margin_<p>. Products: admin_fee, service_mot_plan, warranty, ceramic_vehicle_protection, anti_bacterial_protection, gap_rti_insurance, premia_gap_rti_insurance, delivery, smart_plus_insurance, services_mots_carried_over, ev_charger, mats, showroom_transfer, home_delivery_charge.
- Totals: total_product_cost, total_product_sell, total_product_discount, total_product_margin.

Enquiry, customer, part exchange
- enquiry_id, enquiry_created_at, enquiry_source, enquiry_type, enquiry_category.
- Customer personal data (names, email, phone, postcode): customer_* columns. Never put these in an answer.
- Part exchange: px_vrm, px_cap_id, px_make, px_model, px_mileage, px_valuation_amount, px_system_valuation_amount, px_cap_clean, px_valuation_vs_cap_clean, px_valuation_status.
- cancellation_reason, customer_collection_type (Home Delivery, Click And Collect).

Links: order_url, invoice_url, customer_url, vehicle_url (Apex).
