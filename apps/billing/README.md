# Plans and manual payments

Default plans are Ultra (PKR 2,425/calendar month, all non-AI features) and Ultra AI (PKR 4,599/calendar month, including AI). Run `python manage.py migrate` when deploying. Existing accounts also need an approved subscription; the old user unlock flag alone does not grant paid access.

In Jazzmin `/admin/`, use **Billing → Plans** to change prices, names, AI entitlement and availability. Disabling a plan hides it from new checkouts without revoking existing subscriptions. AI entitlement changes apply to current subscribers too.

Add your receiving bank under **Billing → Payment banks** with bank name, account title, account number, optional IBAN and instructions. Only active accounts appear at checkout. No real bank details are seeded.

Owners sign in and open `/billing` (also available to locked/expired accounts). Public prices are at `/pricing`. Creating a checkout saves the quoted PKR amount and receiving bank details. The owner transfers money, enters the transaction reference and uploads a PNG/JPEG/WebP receipt (up to 5 MB and 20 megapixels). Proof is validated, re-encoded and stored privately in the database; it has no public media URL. Include this data in database backups.

Under **Billing → Payments**, filter Pending, open the payment and inspect its screenshot and reference against your bank statement. Use the **Review payment** link in the list to open a pending payment, then click **Approve payment & activate access** at the top of its detail page. The bulk **Verify payment and activate / renew subscription** action is also available in the list. A screenshot alone never grants access. To reject from the detail page, enter a review note explaining why and click **Reject payment**; the note is saved in the same step. For the bulk rejection action, save the note first. The customer can see the note and start a new checkout. Approved/rejected records are retained with reviewer and timestamps. Repeated approval cannot add access twice.

Approval starts one calendar month. Same-plan renewal before expiry adds a month to the expiry; month-end dates clamp to the last day of the next month. Switching plans replaces the remaining period with a new month, with no automatic prorating; checkout explains this. Subscriptions expire automatically through request-time checks. **Billing → Subscriptions** can suspend/resume access. An individually suspended user remains suspended after payment approval and must be restored separately in Users. Workspace member roles continue to apply.

All AI API requests and running AI research checkpoints require an active AI-enabled subscription. Billing APIs remain available for renewal, but only owners can submit payments and view workspace receipts. Platform superusers can review every receipt. Payment approval is transactional and recorded in audit events.
