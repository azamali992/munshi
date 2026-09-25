# Munshi — user guide

*اردو رہنما نیچے ہے / Urdu guide below.*

## Getting in

Open the app (web address, home-screen icon, or the Android app), enter your mobile number
and PIN. The owner creates PINs for staff under **More → Staff**. Five wrong PINs lock the
account for 15 minutes. Change your PIN in **Settings**.

**Create your business** (sign-in screen): business name, city, your name, mobile, PIN.
Tick *sample data* to explore with Sultan Traders' numbers, or leave it off and start
empty: add products and customers under **More**, or download the Excel template from
**Setup → Import**, fill it in, upload it. Setting up many products or clients is quicker on
a computer: see [Office console](#office-console-on-a-computer).

## Who sees what

| | Owner | Clerk | Salesman | Driver |
|---|---|---|---|---|
| Home | Today | Desk | Book order | Stops |
| Chat with the munshis | ✓ | ✓ | ✓ (order, khata, stock, promises) | ✓ (stops) |
| Approve actions | all | routine | — | — |
| Khata, payments, expenses | ✓ | ✓ | khata only | — |
| Purchases, supplier payments | ✓ / ✓ | receive only | — | — |
| Reports, audit | ✓ | ✓ | — | — |
| Staff, setup, settings, backup | ✓ | — | — | — |

## The day

**Orders.** Type or say it: *"Chaudhry Farms ko 20 urea aur 5 dap bhej do"*. The Order
Munshi drafts it; a card appears; the clerk (or owner) taps **Approve**. Or tap **New
order** on the Desk and fill the form. Confirm when the customer says yes, then **Allocate**
at a godown. If the customer is over their credit limit the order stays a draft and the
owner is told.

**Dispatch.** Desk → **Suggest today's plan**: allocated orders grouped by route with the
smallest van that fits. Tick and create. Open the plan and **Approve loading** — stock
leaves the godown and every customer on the plan gets a four-digit delivery code by
WhatsApp (or from the Messages screen).

**Delivery (driver).** Stops in route order with address and phone. Tap a stop: delivered
quantities, returns, cash collected, the customer's code. **Close stop**. No signal? It
saves on the phone and syncs by itself.

**Cash.** When the driver returns, open the plan → **Record driver's cash hand-in**. If it
is short, Munshi names the stop to look at first, and the owner gets a notification.
Payments at the counter, by bank, JazzCash or Easypaisa: **Record payment** (a receipt
is generated and queued to the customer). Expenses: **Add expense**. **Reports → Cashbook**
shows the day.

**Stock in.** *"Received 100 urea from Fauji at 3600 bill FF-2291"* or **Receive stock**:
the goods go into the godown, the bill goes on the supplier's account, the cost price
updates. Only the owner pays a supplier.

**Collections.** **Khata** lists who owes what, oldest first, with promises. **Draft for
everyone overdue** writes templated reminders (gentle / firm / final by age); each is sent
only when you tap **Approve & send**. Log a promise from the customer's page; broken
promises appear on Today.

**Documents.** From a customer's page tap any entry: invoice or receipt as PDF, print view,
WhatsApp share, and a link the customer can open without an account. **Statement** gives
the full account.

**Reports.** Sales (by product, customer, day), profit (revenue, cost of goods, expenses,
net), collections (invoiced vs collected, by method), cashbook, stock value, slow stock,
top customers, and each product's movement history.

**Settings.** Language (English / اردو), change PIN, export everything to Excel, download a
backup, install on this phone, sign out. Owners also get **Staff** (add, edit, reset PIN,
sign out of every phone) and **Setup** (business details, digest time, godowns, routes with
stop order, vehicles with capacity, Excel import).

## Office console (on a computer)

For entering a lot of data, open **`<your Munshi address>/office`** on a computer or tablet (the owner
and clerk also find it in the phone app under **More → Office console**). Sign in with the same phone
number and PIN. It is the same business, the same numbers and the same rules -- just laid out in big
tables:

- **Products & prices** -- search and sort every product. The owner clicks a list price to change it
  (Enter saves), or ticks several products and changes them together (set a price, ± %, ± Rs): a
  preview shows old → new before anything is saved. Each product's side panel shows its **price
  history** (who changed what, when), stock per godown and recent movements. The clerk can look but
  not change prices.
- **Inventory** -- stock per godown with totals (and its value, for the owner). **Receive purchase**,
  **Transfer**, and (owner) **Adjust stock**. **Physical count**: pick the godown, type what you
  counted (leave a row blank to skip it; Enter moves down), **Preview differences**, and the owner
  **posts** them -- each difference becomes an adjustment named "stock count <date>". If stock moved
  while you were counting (a van loaded), nothing is posted and you preview again. A count below
  what is reserved for allocated orders is refused. **Movements** shows every in and out of a product
  with the balance after each.
- **Clients** -- balances, days overdue, credit used; add or edit a client. Raising or removing a
  credit limit needs the owner. Open a client for their khata with running balance, open orders and
  the **Statement**.
- **Suppliers** (what we owe, their khata), **Godowns** (owner adds), **Import / export** (owner).

## Things to know

- Nothing changes stock or money until a person approves it. The Approvals tab shows what's waiting; the bell shows notifications.
- Reminders, codes, invoices and receipts are templates. No one — and no AI — writes free text to your customers.
- Every action is in **Audit** with who asked and who approved.
- The 8pm digest (time in Setup) goes to the owner's WhatsApp and the bell.

---

# منشی — اردو رہنما

## داخل ہونا

ایپ کھولیں، موبائل نمبر اور پن درج کریں۔ مالک عملے کے پن **مزید ← عملہ** میں بناتا ہے۔
پانچ غلط پن پر اکاؤنٹ 15 منٹ کے لیے بند ہو جاتا ہے۔ اپنا پن **ترتیبات** میں بدلیں۔

**اپنا کاروبار بنائیں**: کاروبار کا نام، شہر، اپنا نام، موبائل، پن۔ نمونہ ڈیٹا کے ساتھ
آزمائیں یا خالی شروع کریں اور **سیٹ اپ ← درآمد** سے ایکسل ٹیمپلیٹ بھر کر اپ لوڈ کریں۔

## دن کا کام

**آرڈر**: لکھیں یا بولیں — *"چوہدری فارمز کو 20 یوریا اور 5 ڈی اے پی بھیج دو"*۔ آرڈر منشی
ڈرافٹ بناتا ہے، کارڈ آتا ہے، منشی یا مالک **منظور** دباتا ہے۔ گاہک کی ہاں پر تصدیق کریں،
پھر گودام میں **مختص** کریں۔ کریڈٹ حد سے زیادہ ہو تو آرڈر ڈرافٹ رہتا ہے اور مالک کو اطلاع ملتی ہے۔

**ڈسپیچ**: ڈیسک ← **آج کا پلان تجویز کریں**۔ پلان کھول کر **لوڈنگ منظور** کریں — اسٹاک
گودام سے نکلتا ہے اور ہر گاہک کو چار ہندسوں کا ڈیلیوری کوڈ جاتا ہے۔

**ڈیلیوری (ڈرائیور)**: اسٹاپ ترتیب سے۔ اسٹاپ پر: پہنچائی گئی تعداد، واپسی، نقد، گاہک کا کوڈ۔
**اسٹاپ بند کریں**۔ نیٹ نہ ہو تو فون میں محفوظ ہو کر خود بھیج دیا جاتا ہے۔

**نقد**: ڈرائیور واپس آئے تو پلان کھول کر **ڈرائیور کی نقدی درج کریں**۔ کم ہو تو منشی بتاتا ہے
پہلے کون سا اسٹاپ دیکھیں۔ کاؤنٹر، بینک، جاز کیش، ایزی پیسہ کی ادائیگی: **ادائیگی درج کریں**۔
اخراجات: **خرچ درج کریں**۔ **رپورٹس ← روکڑ** میں دن کا حساب۔

**اسٹاک آنا**: *"فوجی سے 100 یوریا 3600 پر آیا بل FF-2291"* یا **اسٹاک وصول کریں**۔ سپلائر کو
ادائیگی صرف مالک کرتا ہے۔

**وصولی**: **کھاتہ** میں کس پر کتنا باقی، پرانا پہلے۔ **سب زائد المیعاد کے لیے** طے شدہ
یاد دہانیاں بناتا ہے؛ **منظور اور بھیجیں** پر ہی جاتی ہیں۔ وعدہ گاہک کے صفحے سے درج کریں۔

**دستاویزات**: گاہک کے صفحے پر کسی اندراج پر: بل یا رسید PDF، پرنٹ، واٹس ایپ شیئر، اور
ایسا لنک جو گاہک بغیر اکاؤنٹ کھول سکے۔

**ترتیبات**: زبان، پن، ایکسل ایکسپورٹ، بیک اپ، فون پر انسٹال، سائن آؤٹ۔ مالک کے لیے **عملہ**
اور **سیٹ اپ** (گودام، روٹ، گاڑیاں، درآمد)۔

## یاد رکھیں

- کسی کی منظوری کے بغیر اسٹاک یا پیسہ نہیں ہلتا۔
- گاہک کو کبھی آزاد متن نہیں جاتا — صرف طے شدہ پیغامات۔
- ہر کام **آڈٹ لاگ** میں ہے: کس نے کہا، کس نے منظور کیا۔
