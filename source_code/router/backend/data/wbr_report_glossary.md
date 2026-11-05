# Margin Lines
Line | Source | Definition
Date Of Deliveries | APEX | Date
Purchase Date | Autoview | Date In
DIS (Days In Sale) | Calculation | Days from Date In to Delivery Date. Nil if there is no Date In
Selling Price £/U | APEX | Sales Net Price
AV - Purchase Price | Autoview | cost1
APEX - Vehicle Discount | APEX | Vehicle Discount
Calcul - VAT Margin | Calculation | If VAT Scheme is Margin: (Sales Net Price + Vehicle Discount + cost1) / 1.20 x -0.2. Otherwise nil
Calcul - Finance Subsidy | Calculation | If Finance Co. is Blackhorse Deal Saver or Santander 0%: Finance Total from APEX x Finance Rate, from a table kept by hand (term / period). Otherwise nil
Chassis Margin £/U | Calculation | Sales Net Price + Vehicle Discount + Autoview cost1 + VAT Margin + Finance Subsidy. Consignment cars use the standard chassis margin
AV - Recon Costs £/U | Autoview | cost1. No figure is assumed for a car with none
STD - Prep Centre (Rent) £/U | Standard | Standard costs from a table (nature, period), reviewed every month from the monthly financial closing
STD - Transport Salaries £/U | Standard | As above
STD - External Transport Costs £/U | Standard | As above
STD - Delivery Sale £/U | Standard | As above
STD - Late Costs £/U | Standard | As above
STD - Damage Claim / Rebates / Other | Standard | As above
Total Costs Of Sales | Calculation | The AV and STD lines above added together
GPU-M | Calculation | Chassis Margin + Total Costs Of Sales
Volume Financing | Calculation | 1 if Finance Total is above 0. Otherwise 0
Volume Warranty | Calculation | 1 if Net - Warranty is above 0. Otherwise 0
APEX - Financing Margin | APEX | Finance Commission
APEX - Warranty Margin | APEX | Net - Warranty
APEX - GAP/RTI Insurance | APEX | Net - Premia GAP/RTI Insurance
APEX - SMART Plus | APEX | Net - SMART Insurance
APEX - Admin Fees | APEX | Net - Admin Fee
APEX - Service/MOT Plan | APEX | Net - Service/MOT Plan
APEX - Ceramic Vh Protection | APEX | Net - Ceramic Vehicle Protection
APEX - Anti-Bac | APEX | Net - Anti-Bacterial Protection
STD - Internal Costs Of Services | Standard | Used to adjust any differences found between metal margin and services, if needed
GPU S £/U | Calculation | The APEX and STD lines above added together
GPU MS £/U | Calculation | GPU-M + GPU-S

# Inventories Status
Status | Definition
DIS | The days from invoicing the car into stock
Awaiting Arrival | Cars we have bought and invoiced into stock that have not yet arrived at a prep centre or site
Trade | Cars we have sent or are sending to auction
In Prep | Cars at the prep centre or site that we are waiting to inspect, or have inspected and are awaiting parts delivery
To Price | Cars at a status that should see them For Sale, but we have not yet given a retail price
For Sale | Cars live on the website: on the forecourt at a site, or at a prep centre where the parts have arrived and are ready to be fitted
Sold - Not Delivered | Cars with orders on them that have not been collected by or delivered to the customer
Courtesy | Cars held in stock at sites for customers to use while their car is in for repair or service
Held | To be defined

# Sources Of Data
Note | Detail
Metal And Service Margin | There is a structural underestimation of the metal margin and an overestimation of the service margin
Why | As far as we understand, this is linked to the subsidy finance catch-up effect. Not everything is tracked at the chassis
How Much | The amount is found by analysing the difference between the weekly dashboard and the monthly closings
