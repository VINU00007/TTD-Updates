import imaplib
import email
from email.header import decode_header
import os
import re
import json
import time
import requests
import pdfplumber
from io import BytesIO
from datetime import datetime, timedelta


# ============================================================
# CONFIGURATION
# ============================================================

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

IMAP_SERVER = "imap.gmail.com"

SUBJECT_KEYWORD = "REPORT"

CHECK_INTERVAL = 30

STATE_FILE = "invoice_agent_state.json"


# ============================================================
# TIME
# ============================================================

def now_ist():

    return datetime.utcnow() + timedelta(
        hours=5,
        minutes=30
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    response = requests.post(
        url,
        data=payload,
        timeout=20
    )

    response.raise_for_status()


# ============================================================
# TEXT HELPERS
# ============================================================

def clean(text):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


def safe_decode(value):

    if not value:
        return ""

    result = []

    for part, encoding in decode_header(value):

        if isinstance(part, bytes):

            result.append(
                part.decode(
                    encoding or "utf-8",
                    errors="replace"
                )
            )

        else:

            result.append(str(part))

    return "".join(result)


def format_money(value):

    if value in ("", None):
        return ""

    try:

        return f"₹{float(value):,.2f}"

    except Exception:

        return f"₹{value}"


# ============================================================
# STATE
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):

        return {
            "initialized": False,
            "processed_uids": [],
            "processed_invoices": []
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return {
            "initialized": False,
            "processed_uids": [],
            "processed_invoices": []
        }


def save_state(state):

    state["processed_uids"] = list(
        dict.fromkeys(
            state.get(
                "processed_uids",
                []
            )
        )
    )[-2000:]

    state["processed_invoices"] = list(
        dict.fromkeys(
            state.get(
                "processed_invoices",
                []
            )
        )
    )[-2000:]

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            indent=2
        )


# ============================================================
# PDF TEXT
# ============================================================

def extract_pdf_text(pdf_bytes):

    pages = []

    with pdfplumber.open(
        BytesIO(pdf_bytes)
    ) as pdf:

        for page in pdf.pages:

            text = page.extract_text()

            if text:

                pages.append(text)

    return "\n".join(pages)


# ============================================================
# INDIAN NUMBER WORDS
# ============================================================

ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19
}


TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90
}


SCALES = {
    "thousand": 1_000,
    "thousands": 1_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "crore": 10_000_000,
    "crores": 10_000_000
}


def words_to_number(text):

    words = re.findall(
        r"[A-Za-z]+",
        text.lower()
    )

    total = 0
    current = 0

    for word in words:

        if word in ONES:

            current += ONES[word]

        elif word in TENS:

            current += TENS[word]

        elif word == "hundred":

            current *= 100

        elif word in SCALES:

            total += current * SCALES[word]
            current = 0

    return total + current


# ============================================================
# FIND AMOUNT WRITTEN IN WORDS
# ============================================================

def amount_from_words(text):

    pattern = (
        r"([A-Za-z\s-]+?)"
        r"\s+Rupees?\s+only"
    )

    matches = re.findall(
        pattern,
        text,
        re.IGNORECASE
    )

    if not matches:

        return None

    # Usually the final one is the invoice amount
    phrase = matches[-1]

    number = words_to_number(
        phrase
    )

    if number <= 0:

        return None

    return float(number)


# ============================================================
# FIND MONEY VALUES
# ============================================================

def extract_money_values(text):

    pattern = (
        r"(?<![\d.])"
        r"(\d{1,3}(?:,\d{3})+(?:\.\d{2})?"
        r"|\d+\.\d{2})"
        r"(?![\d.])"
    )

    values = []

    for match in re.finditer(
        pattern,
        text
    ):

        raw = match.group(1)

        try:

            number = float(
                raw.replace(",", "")
            )

            values.append(
                number
            )

        except Exception:

            pass

    return values


# ============================================================
# FIND FINAL AMOUNT
# ============================================================

def find_final_amount(text):

    money_values = extract_money_values(
        text
    )

    if not money_values:

        return ""

    # --------------------------------------------------------
    # BEST METHOD:
    #
    # Match the amount written in words.
    #
    # Example Invoice 6:
    #
    # Sixty Five Thousand Nine Hundred Fifty Nine Rupees only
    #
    # -> 65959
    #
    # Actual PDF amount:
    #
    # 65958.95
    #
    # Difference is only rounding.
    # --------------------------------------------------------

    words_amount = amount_from_words(
        text
    )

    if words_amount is not None:

        matching = []

        for value in money_values:

            if abs(
                value - words_amount
            ) <= 1.00:

                matching.append(
                    value
                )

        if matching:

            # If several are close, choose the largest
            return max(
                matching
            )


    # --------------------------------------------------------
    # SECOND METHOD:
    #
    # Look for Total Amount.
    # --------------------------------------------------------

    total_matches = re.findall(
        r"Total\s+Amount\s*:?\s*"
        r"([\d,]+\.\d{2})",
        text,
        re.IGNORECASE
    )

    if total_matches:

        try:

            return float(
                total_matches[0].replace(
                    ",",
                    ""
                )
            )

        except Exception:

            pass


    # --------------------------------------------------------
    # THIRD METHOD:
    #
    # Net / Nett Amount
    # --------------------------------------------------------

    net_matches = re.findall(
        r"Nett?\s+Amount\s*:?\s*"
        r"([\d,]+\.\d{2})",
        text,
        re.IGNORECASE
    )

    if net_matches:

        try:

            return float(
                net_matches[-1].replace(
                    ",",
                    ""
                )
            )

        except Exception:

            pass


    # Last fallback
    return max(
        money_values
    )


# ============================================================
# PRODUCT ROWS
# ============================================================

def extract_products(text):

    products = []

    pattern = re.compile(
        r"^\s*"
        r"(\d+)\s+"
        r"(.+?)\s+"
        r"(\d{4,8})\s+"
        r"(\d+)\s+"
        r"(\d+(?:\.\d+)?)\s+"
        r"(\d+(?:\.\d+)?)\s+"
        r"([\d,]+\.\d{2})"
        r"\s*$"
    )

    for line in text.splitlines():

        line = clean(line)

        match = pattern.match(
            line
        )

        if not match:

            continue

        products.append({

            "name": clean(
                match.group(2)
            ),

            "hsn": match.group(3),

            "bags": int(
                match.group(4)
            ),

            "quantity": float(
                match.group(5)
            ),

            "rate": float(
                match.group(6)
            ),

            "amount": float(
                match.group(7).replace(
                    ",",
                    ""
                )
            )
        })

    return products


# ============================================================
# EXTRA CHARGES / TAXES
# ============================================================

CHARGE_LABELS = [
    "IGST",
    "CGST",
    "SGST",
    "TCS",
    "CESS",
    "WEIGHMENT",
    "FREIGHT",
    "DISCOUNT",
    "ROUND OFF"
]


def extract_charges(text):

    charges = []

    lines = [
        clean(x)
        for x in text.splitlines()
        if clean(x)
    ]


    for index, line in enumerate(lines):

        upper = line.upper()

        matched_label = None

        for label in CHARGE_LABELS:

            if re.search(
                rf"\b{re.escape(label)}\b",
                upper
            ):

                matched_label = label
                break


        if not matched_label:

            continue


        # Amount on same line
        amount_match = re.search(
            r"([\d,]+\.\d{2})\s*$",
            line
        )


        amount = None


        if amount_match:

            amount = float(
                amount_match.group(1)
                .replace(",", "")
            )


        # Amount on following line
        elif index + 1 < len(lines):

            next_match = re.fullmatch(
                r"([\d,]+\.\d{2})",
                lines[index + 1]
            )

            if next_match:

                amount = float(
                    next_match.group(1)
                    .replace(",", "")
                )


        # Ignore zero-value tax lines
        if amount is None:

            continue

        if amount == 0:

            continue


        # Capture percentage if present
        percent = ""

        percent_match = re.search(
            r"@?\s*(\d+(?:\.\d+)?)\s*%",
            line
        )

        if percent_match:

            percent = (
                f" @{percent_match.group(1)}%"
            )


        charges.append({

            "label": matched_label + percent,

            "amount": amount

        })


    return charges


# ============================================================
# PARTY
# ============================================================

def extract_party(text):

    match = re.search(
        r"Bill\s+To\s+Party.*?"
        r"Name\s*:\s*(.*?)"
        r"\s+State\s*:",
        text,
        re.IGNORECASE | re.DOTALL
    )

    if match:

        return clean(
            match.group(1)
        )

    return ""


# ============================================================
# INVOICE EXTRACTION
# ============================================================

def extract_invoice(pdf_bytes):

    text = extract_pdf_text(
        pdf_bytes
    )


    # --------------------------------------------------------
    # Invoice number
    # --------------------------------------------------------

    invoice_match = re.search(
        r"Invoice\s*NO\s*:?\s*"
        r"\d{4}-\d{2}/(\d+)",
        text,
        re.IGNORECASE
    )

    invoice_no = (
        invoice_match.group(1)
        if invoice_match
        else ""
    )


    # --------------------------------------------------------
    # Date
    # --------------------------------------------------------

    date_match = re.search(
        r"Date\s+of\s+Invoice\s*:\s*"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        text,
        re.IGNORECASE
    )

    invoice_date = (
        date_match.group(1)
        if date_match
        else ""
    )


    # --------------------------------------------------------
    # Vehicle
    # --------------------------------------------------------

    vehicle_match = re.search(
        r"Vehicle\s+Number\s*:\s*"
        r"([A-Z0-9-]+)",
        text,
        re.IGNORECASE
    )

    vehicle = (
        vehicle_match.group(1)
        if vehicle_match
        else ""
    )


    # --------------------------------------------------------
    # RST
    # --------------------------------------------------------

    rst_match = re.search(
        r"\bRST\s+(\d+)",
        text,
        re.IGNORECASE
    )

    rst = (
        rst_match.group(1)
        if rst_match
        else ""
    )


    # --------------------------------------------------------
    # Party
    # --------------------------------------------------------

    party = extract_party(
        text
    )


    # --------------------------------------------------------
    # Place
    # --------------------------------------------------------

    place_match = re.search(
        r"Bill\s+To\s+Party.*?"
        r"City\s*:\s*(.*?)"
        r"\s+Phone",
        text,
        re.IGNORECASE | re.DOTALL
    )

    place = ""

    if place_match:

        place = clean(
            place_match.group(1)
        )


    # --------------------------------------------------------
    # Products
    # --------------------------------------------------------

    products = extract_products(
        text
    )


    # --------------------------------------------------------
    # Charges
    # --------------------------------------------------------

    charges = extract_charges(
        text
    )


    # --------------------------------------------------------
    # Final amount
    # --------------------------------------------------------

    final_amount = find_final_amount(
        text
    )


    # --------------------------------------------------------
    # Totals from product rows
    # --------------------------------------------------------

    total_bags = sum(
        p["bags"]
        for p in products
    )

    total_quantity = sum(
        p["quantity"]
        for p in products
    )

    product_amount = sum(
        p["amount"]
        for p in products
    )


    # Product names
    product_names = ", ".join(
        dict.fromkeys(
            p["name"]
            for p in products
        )
    )


    # Rate
    if len(products) == 1:

        rate_text = format_money(
            products[0]["rate"]
        )

    elif len(products) > 1:

        rate_text = "Multiple"

    else:

        rate_text = ""


    return {

        "invoice_no": invoice_no,

        "date": invoice_date,

        "party": party,

        "vehicle": vehicle,

        "rst": rst,

        "product": product_names,

        "bags": total_bags,

        "quantity": total_quantity,

        "rate": rate_text,

        "product_amount": product_amount,

        "charges": charges,

        "final_amount": final_amount,

        "place": place

    }


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def build_message(info):

    message = (
        "🧾  INVOICE ALERT  🧾\n\n"

        f"📄 INVOICE NO : {info['invoice_no']}\n"
        f"📅 DATE : {info['date']}\n\n"

        f"👤 {info['party']}\n"
        f"🚛 VEHICLE : {info['vehicle']}\n"
        f"🧾 RST : {info['rst']}\n\n"

        f"🌾 PRODUCT : {info['product']}\n"
        f"📦 BAGS : {info['bags']}\n"
        f"⚖ QUANTITY : {info['quantity']:.2f} Qntl\n"
        f"💰 RATE : {info['rate']}\n\n"
    )


    # Product amount
    if info["product_amount"]:

        message += (
            f"💰 PRODUCT AMOUNT : "
            f"{format_money(info['product_amount'])}\n"
        )


    # Taxes / charges
    for charge in info["charges"]:

        message += (
            f"🧾 {charge['label']} : "
            f"{format_money(charge['amount'])}\n"
        )


    # Final amount
    if info["final_amount"]:

        message += (
            "\n"
            "────────────────────\n"
            f"💵 FINAL AMOUNT : "
            f"{format_money(info['final_amount'])}\n"
        )


    message += (
        "\n"
        f"📍 PLACE : {info['place']}\n\n"
        "▣ INVOICE RECEIVED"
    )


    return message


# ============================================================
# INITIALIZE
# ============================================================

def initialize(mail, state):

    if state.get("initialized"):

        return state


    print()
    print("=" * 70)
    print("INITIALIZING INVOICE TELEGRAM AGENT")
    print("=" * 70)
    print(
        "Existing REPORT emails will be ignored."
    )
    print(
        "Only new emails will generate alerts."
    )
    print("=" * 70)


    status, data = mail.uid(
        "search",
        None,
        "ALL"
    )


    if status == "OK" and data and data[0]:

        state["processed_uids"] = [
            uid.decode()
            for uid in data[0].split()
        ]


    state["initialized"] = True

    save_state(
        state
    )


    print(
        "Initialization complete."
    )

    return state


# ============================================================
# CHECK MAIL
# ============================================================

def check_mail():

    mail = imaplib.IMAP4_SSL(
        IMAP_SERVER
    )

    mail.login(
        EMAIL_USER,
        EMAIL_PASS
    )

    mail.select(
        "INBOX"
    )


    state = load_state()

    state = initialize(
        mail,
        state
    )


    processed_uids = set(
        state.get(
            "processed_uids",
            []
        )
    )


    processed_invoices = set(
        str(x)
        for x in state.get(
            "processed_invoices",
            []
        )
    )


    status, data = mail.uid(
        "search",
        None,
        "ALL"
    )


    if status != "OK":

        print(
            "Gmail search failed."
        )

        mail.logout()

        return


    uids = data[0].split()


    new_count = 0


    for uid_bytes in uids:

        uid = uid_bytes.decode()


        if uid in processed_uids:

            continue


        status, msg_data = mail.uid(
            "fetch",
            uid_bytes,
            "(RFC822)"
        )


        if status != "OK":

            continue


        raw_email = msg_data[0][1]

        msg = email.message_from_bytes(
            raw_email
        )


        subject = safe_decode(
            msg.get("Subject")
        )


        # Only REPORT emails
        if not re.search(r"\bREPORT\b", subject, re.IGNORECASE):

            processed_uids.add(
                uid
            )

            continue


        print()
        print("=" * 70)
        print("NEW REPORT EMAIL DETECTED")
        print("=" * 70)
        print(
            f"Subject : {subject}"
        )


        invoice_processed = False


        for part in msg.walk():

            filename = part.get_filename()

            content_type = (
                part.get_content_type()
            )


            is_pdf = (

                (
                    filename
                    and
                    filename.lower().endswith(
                        ".pdf"
                    )
                )

                or

                content_type == "application/pdf"

            )


            if not is_pdf:

                continue


            pdf_bytes = part.get_payload(
                decode=True
            )


            if not pdf_bytes:

                continue


            try:

                info = extract_invoice(
                    pdf_bytes
                )


                invoice_no = (
                    info["invoice_no"]
                    .strip()
                )


                print(
                    f"PDF found: "
                    f"{filename or 'Report.pdf'}"
                )

                print(
                    f"Invoice No : "
                    f"{invoice_no}"
                )

                print(
                    f"Party      : "
                    f"{info['party']}"
                )

                print(
                    f"Product Amt: "
                    f"{format_money(info['product_amount'])}"
                )

                print(
                    f"Charges    : "
                    f"{info['charges']}"
                )

                print(
                    f"FINAL AMT  : "
                    f"{format_money(info['final_amount'])}"
                )


                if not invoice_no:

                    print(
                        "Invoice number not found."
                    )

                    break


                # ------------------------------------------------
                # DUPLICATE PROTECTION
                # ------------------------------------------------

                if invoice_no in processed_invoices:

                    print(
                        f"Invoice {invoice_no} "
                        f"already processed."
                    )

                    print(
                        "Telegram alert skipped."
                    )

                    invoice_processed = True

                    break


                # ------------------------------------------------
                # SEND TELEGRAM
                # ------------------------------------------------

                message = build_message(
                    info
                )

                send_telegram(
                    message
                )


                print(
                    "Telegram alert sent successfully."
                )


                processed_invoices.add(
                    invoice_no
                )

                new_count += 1

                invoice_processed = True

                break


            except Exception as e:

                print(
                    "Invoice processing error:",
                    e
                )

                break


        if invoice_processed:

            processed_uids.add(
                uid
            )


    state["processed_uids"] = list(
        processed_uids
    )

    state["processed_invoices"] = list(
        processed_invoices
    )

    save_state(
        state
    )


    mail.logout()


    if new_count:

        print()
        print(
            f"{new_count} new REPORT email(s) processed."
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print()
    print("=" * 70)
    print("SVT INVOICE TELEGRAM AGENT")
    print("=" * 70)
    print(
        "Watching Gmail for REPORT invoice emails"
    )
    print(
        f"Check interval : {CHECK_INTERVAL} seconds"
    )
    print(
        "Duplicate key  : Invoice Number"
    )
    print(
        "Amount method  : Invoice final amount"
    )
    print(
        "Weighment agent: UNTOUCHED"
    )
    print("=" * 70)


    while True:

        try:

            check_mail()

        except Exception as e:

            print(
                "Email check error:",
                e
            )


        time.sleep(
            CHECK_INTERVAL
        )
