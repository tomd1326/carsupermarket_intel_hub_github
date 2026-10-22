Business rules confirmed by Tom. Use these instead of guessing; if a question needs a rule that is not here, ask.

Ordered and sold
- Ordered: the customer has paid a deposit and is waiting to collect. invoice_status = 'STATUS_ACKNOWLEDGED' (deal_status 'Acknowledged').
- Sold / delivered: the car has been collected or delivered and the balance paid. invoice_status = 'STATUS_PAID' (deal_status 'Delivered').
- Both ordered and sold count as a sale unless the question says otherwise. Ask which one is meant when it matters (e.g. "sold this month" by order acknowledged date or by invoice posted date).
- Cancelled orders: invoice_status = 'STATUS_CANCELLED' (deal_status 'Cancelled'). Credited invoices: invoice_credited = TRUE (deal_status 'Invoice Credited'). Ask whether to include either when it is not clear.
- Count orders by order_acknowledged_date; count deliveries / invoices by invoice_posted_date.

Prices
- The advertised price is advertised_price_inc_vat_inc_admin_fee: price before discount plus the standard admin fee. Use it for "cars under 15k" style questions unless told otherwise.
- The price the customer actually paid is sale_price_after_discount_inc_admin_fee.
- Leave cancelled orders out of every price-based average: their selling price can be a stand-in typed on the deal (e.g. a -9998 discount on a car not yet priced). Their Autotrader valuation and retail rating are correct and can be kept.

Interest-free (0%) cars
- Interest-free cars (interest_free = TRUE, sales_type '0%', sales_type_bucket '0%') sell at about 125% of the Autotrader valuation because the price absorbs a ~14% finance subsidy charge.
- Leave them out of any price-related average (selling price, Autotrader valuation, price position). Keep them in counts and in non-price metrics (GPU, days in stock, ratings).

Benchmarks
- CAP Clean is the trade benchmark for purchase prices. The Autotrader valuation is the retail benchmark for selling prices; normal stock sells at about 98% of it.

GPU
- GPU M = metal margin after GPU M standard costs. GPU S = product and finance margin after GPU S standard costs. GPU MS = GPU M + GPU S.
