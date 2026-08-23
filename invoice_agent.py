import imaplib
import email
from email.header import decode_header
import os
import requests
import time
import re
import json
from io import BytesIO
from datetime import datetime, timedelta
import pdfplumber


# ============================================================
# CONFIGURATION
# ============================================================

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

IMAP_SERVER = "imap.gmail.com"

# Invoice emails have REPORT in the subject
KEYWORD = "REPORT"

# Gmail check interval
CHECK_INTERVAL = 30

# Number of latest Gmail messages inspected each cycle
RECENT_EMAIL_LIMIT = 50

# Persistent duplicate-protection file
STATE_FILE = "invoice_agent_state.json"


# ============================================================
# TIME
# ============================================================

def now_ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


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

def safe_decode(value):

    if not value:
        return ""

    parts = decode_header(value)

    output = []

    for part, encoding in parts:

        if isinstance(part, bytes):

            output.append(
                part.decode(
                    encoding or "utf-8",
                    errors="replace"
                )
            )

        else:

            output.append(str(part))

    return "".join(output)


def clean(value):

    if not value:
        return ""

    return re.sub(
        r"\s+",
        " ",
        value
    ).strip()


# ============================================================
# STATE
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):

        return {
            "initialized": False,
            "processed_invoices": [],
            "processed_uids": []
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            state = json.load(f)

        return state

    except Exception as e:

        print(
            f"State file could not be read: {e}"
        )

        return {
            "initialized": False,
            "processed_invoices": [],
            "processed_uids": []
        }


def save_state(state):

    # Keep the state reasonably small
    state["processed_invoices"] = list(
        dict.fromkeys(
            state.get(
                "processed_invoices",
                []
            )
        )
    )[-2000:]

    state["processed_uids"] = list(
        dict.fromkeys(
            state.get(
                "processed_uids",
                []
            )
        )
    )[-1000:]

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
# PDF EXTRACTION
# ============================================================

def extract_invoice_from_pdf(pdf_bytes):

    text = ""

    with pdfplumber.open(
        BytesIO(pdf_bytes)
    ) as pdf:

        for page in pdf.pages:

            page_text = page.extract_text()

            if page_text:

                text += page_text + "\n"


    # --------------------------------------------------------
    # INVOICE NUMBER
    # --------------------------------------------------------

    invoice_no = ""

    match = re.search(
        r"Invoice\s+NO\s*:\s*\d{4}-\d{2}/(\d+)",
        text,
        re.IGNORECASE
    )

    if match:

        invoice_no = match.group(1)


    # Fallback invoice number pattern
    if not invoice_no:

        match = re.search(
            r"Invoice\s+(?:No|Number)\s*[:\-]?\s*(\d+)",
            text,
            re.IGNORECASE
        )

        if match:

            invoice_no = match.group(1)


    # --------------------------------------------------------
    # INVOICE DATE
    # --------------------------------------------------------

    invoice_date = ""

    match = re.search(
        r"Date\s+of\s+Invoice\s*:\s*"
        r"(\d{1,2}/\d{1,2}/\d{4})",
        text,
        re.IGNORECASE
    )

    if match:

        invoice_date = match.group(1)


    # --------------------------------------------------------
    # PARTY NAME
    # --------------------------------------------------------

    party_name = ""

    match = re.search(
        r"Bill\s+To\s+Party\s+Ship\s+To\s+Party\s*"
        r"Name\s*:\s*(.*?)\s+Name\s*:",
        text,
        re.IGNORECASE | re.DOTALL
    )

    if match:

        party_name = clean(
            match.group(1)
        )


    # Fallback party pattern
    if not party_name:

        match = re.search(
            r"Party\s+Name\s*:\s*(.+)",
            text,
            re.IGNORECASE
        )

        if match:

            party_name = clean(
                match.group(1)
            )


    # --------------------------------------------------------
    # VEHICLE
    # --------------------------------------------------------

    vehicle = ""

    match = re.search(
        r"Vehicle\s+Number\s*:\s*([A-Z0-9\-]+)",
        text,
        re.IGNORECASE
    )

    if match:

        vehicle = match.group(1)


    # --------------------------------------------------------
    # RST
    # --------------------------------------------------------

    rst = ""

    match = re.search(
        r"\bRST\s+(\d+)\b",
        text,
        re.IGNORECASE
    )

    if match:

        rst = match.group(1)


    # --------------------------------------------------------
    # PRODUCT / BAGS / QUANTITY / RATE / PRODUCT AMOUNT
    # --------------------------------------------------------

    product = ""
    bags = ""
    quantity = ""
    rate = ""
    product_amount = ""

    match = re.search(
        r"\n1\s+"
        r"(.+?)\s+"
        r"(\d{4,8})\s+"
        r"(\d+)\s+"
        r"(\d+(?:\.\d+)?)\s+"
        r"([0-9,]+(?:\.\d+)?)\s+"
        r"([0-9,]+(?:\.\d+)?)"
        r"\s*(?:\n|$)",
        text,
        re.IGNORECASE
    )

    if match:

        product = clean(
            match.group(1)
        )

        bags = match.group(3)

        quantity = match.group(4)

        rate = match.group(5).replace(
            ",",
            ""
        )

        product_amount = match.group(6).replace(
            ",",
            ""
        )


    # --------------------------------------------------------
    # TAX / CHARGES
    #
    # This is the ONLY new extraction section.
    # --------------------------------------------------------

    charges = []

    charge_patterns = [

        (
            r"(IGST\s*@?\s*\d+(?:\.\d+)?%)"
            r"\s*:?\s*"
            r"([0-9,]+(?:\.\d+)?)"
        ),

        (
            r"(CGST\s*@?\s*\d+(?:\.\d+)?%)"
            r"\s*:?\s*"
            r"([0-9,]+(?:\.\d+)?)"
        ),

        (
            r"(SGST\s*@?\s*\d+(?:\.\d+)?%)"
            r"\s*:?\s*"
            r"([0-9,]+(?:\.\d+)?)"
        ),

        (
            r"(TCS\s*@?\s*\d+(?:\.\d+)?%)"
            r"\s*:?\s*"
            r"([0-9,]+(?:\.\d+)?)"
        ),

        (
            r"(WEIGHMENT)"
            r"\s*:?\s*"
            r"([0-9,]+(?:\.\d+)?)"
        ),

        (
            r"(FREIGHT)"
            r"\s*:?\s*"
            r"([0-9,]+(?:\.\d+)?)"
        ),

        (
            r"(ROUND\s*OFF)"
            r"\s*:?\s*"
            r"([0-9,]+(?:\.\d+)?)"
        )

    ]


    for pattern in charge_patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE
        )

        for label, value in matches:

            try:

                numeric_value = float(
                    value.replace(
                        ",",
                        ""
                    )
                )

            except Exception:

                continue


            charges.append({

                "label": clean(label),

                "amount": numeric_value

            })


    # --------------------------------------------------------
    # FINAL INVOICE AMOUNT
    #
    # Priority:
    #
    # 1. Total / Final amount explicitly printed
    # 2. Net / Nett amount
    #
    # We DO NOT calculate this from quantity × rate.
    # --------------------------------------------------------

    final_amount = ""


    # Common "Total Amount" format
    match = re.search(
        r"(?:Total\s+Amount|Final\s+Amount)"
        r"\s*:?\s*"
        r"(?:₹\s*)?"
        r"([0-9,]+(?:\.\d+)?)",
        text,
        re.IGNORECASE
    )

    if match:

        final_amount = match.group(1).replace(
            ",",
            ""
        )


    # "Net Amount" / "Nett Amount" fallback
    if not final_amount:

        match = re.search(
            r"Nett?\s+Amount"
            r"\s*:?\s*"
            r"(?:₹\s*)?"
            r"([0-9,]+(?:\.\d+)?)",
            text,
            re.IGNORECASE
        )

        if match:

            final_amount = match.group(1).replace(
                ",",
                ""
            )


    # --------------------------------------------------------
    # IMPORTANT FALLBACK
    #
    # Some invoice PDFs place the final number on a separate
    # line after "Total Amount".
    # --------------------------------------------------------

    if not final_amount:

        match = re.search(
            r"(?:Total\s+Amount|Final\s+Amount|"
            r"Nett?\s+Amount)"
            r"\s*[:\-]?\s*\n\s*"
            r"(?:₹\s*)?"
            r"([0-9,]+(?:\.\d+)?)",
            text,
            re.IGNORECASE
        )

        if match:

            final_amount = match.group(1).replace(
                ",",
                ""
            )


    # --------------------------------------------------------
    # IF NO FINAL AMOUNT LABEL EXISTS
    #
    # Keep the original product amount rather than guessing.
    # --------------------------------------------------------

    if not final_amount:

        final_amount = product_amount


    # --------------------------------------------------------
    # PLACE
    # --------------------------------------------------------

    place = ""

    match = re.search(
        r"Bill\s+To\s+Party.*?"
        r"City\s*:\s*([^:\n]+?)"
        r"\s+State\s*:",
        text,
        re.IGNORECASE | re.DOTALL
    )

    if match:

        place = clean(
            match.group(1)
        )


    return {

        "Invoice No": invoice_no,
        "Invoice Date": invoice_date,
        "Party": party_name,
        "Vehicle": vehicle,
        "RST": rst,
        "Product": product,
        "Bags": bags,
        "Quantity": quantity,
        "Rate": rate,

        # Original product amount
        "Product Amount": product_amount,

        # Taxes / additional charges
        "Charges": charges,

        # FINAL invoice amount including taxes/charges
        "Amount": final_amount,

        "Place": place

    }


# ============================================================
# TELEGRAM INVOICE ALERT
# ============================================================

def process_invoice(info):

    # --------------------------------------------------------
    # FINAL AMOUNT
    # --------------------------------------------------------

    if info["Amount"]:

        try:

            amount_display = (
                f"₹{float(info['Amount']):,.2f}"
            )

        except Exception:

            amount_display = (
                f"₹{info['Amount']}"
            )

    else:

        amount_display = "₹-"


    # --------------------------------------------------------
    # PRODUCT AMOUNT
    # --------------------------------------------------------

    product_amount_display = ""

    if info.get("Product Amount"):

        try:

            product_amount_display = (
                f"💰 PRODUCT AMOUNT : "
                f"₹{float(info['Product Amount']):,.2f}\n"
            )

        except Exception:

            product_amount_display = (
                f"💰 PRODUCT AMOUNT : "
                f"₹{info['Product Amount']}\n"
            )


    # --------------------------------------------------------
    # CHARGES / TAXES
    # --------------------------------------------------------

    charges_text = ""

    for charge in info.get(
        "Charges",
        []
    ):

        try:

            charges_text += (
                f"🧾 {charge['label']} : "
                f"₹{charge['amount']:,.2f}\n"
            )

        except Exception:

            pass


    # --------------------------------------------------------
    # TELEGRAM MESSAGE
    # --------------------------------------------------------

    message = (
        "🧾  INVOICE ALERT  🧾\n\n"

        f"📄 INVOICE NO : {info['Invoice No']}\n"
        f"📅 DATE : {info['Invoice Date']}\n\n"

        f"👤 {info['Party']}\n"
        f"🚛 VEHICLE : {info['Vehicle']}\n"
        f"🧾 RST : {info['RST']}\n\n"

        f"🌾 PRODUCT : {info['Product']}\n"
        f"📦 BAGS : {info['Bags']}\n"
        f"⚖ QUANTITY : {info['Quantity']} Qntl\n\n"

        f"💰 RATE : ₹{info['Rate']}\n"

        f"{product_amount_display}"

        f"{charges_text}"

        "────────────────────\n"

        f"💵 FINAL AMOUNT : {amount_display}\n\n"

        f"📍 PLACE : {info['Place']}\n\n"

        "▣ INVOICE RECEIVED"
    )


    send_telegram(
        message
    )


# ============================================================
# INITIALIZATION
# ============================================================

def initialize_agent(mail):

    state = load_state()

    if state.get("initialized"):

        return state


    print()
    print("=" * 70)
    print("INITIALIZING INVOICE AGENT")
    print("=" * 70)
    print(
        "Existing REPORT emails will NOT generate Telegram alerts."
    )
    print(
        "Only new REPORT emails received after initialization "
        "will be processed."
    )
    print("=" * 70)


    # --------------------------------------------------------
    # Record all currently existing Gmail UIDs.
    # This creates our starting point.
    # --------------------------------------------------------

    status, data = mail.uid(
        "search",
        None,
        "ALL"
    )


    if status == "OK" and data and data[0]:

        current_uids = data[0].split()

        for uid in current_uids:

            state.setdefault(
                "processed_uids",
                []
            ).append(
                uid.decode()
            )


    state["processed_uids"] = list(
        dict.fromkeys(
            state.get(
                "processed_uids",
                []
            )
        )
    )[-1000:]


    state["initialized"] = True

    save_state(
        state
    )


    print(
        "Invoice agent initialized successfully."
    )

    print(
        "Waiting for NEW REPORT emails..."
    )

    return state


# ============================================================
# CHECK GMAIL
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
        "inbox"
    )


    state = initialize_agent(
        mail
    )


    processed_uids = set(
        state.get(
            "processed_uids",
            []
        )
    )


    processed_invoices = set(
        str(x).strip()
        for x in state.get(
            "processed_invoices",
            []
        )
    )


    # --------------------------------------------------------
    # GET ALL EMAIL UIDs
    # --------------------------------------------------------

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


    all_uids = (
        data[0].split()
        if data and data[0]
        else []
    )


    # --------------------------------------------------------
    # ONLY RECENT EMAILS
    # --------------------------------------------------------

    recent_uids = all_uids[
        -RECENT_EMAIL_LIMIT:
    ]


    telegram_count = 0


    for uid in recent_uids:

        uid_text = uid.decode()


        # ----------------------------------------------------
        # OLD / ALREADY CHECKED UID
        # ----------------------------------------------------

        if uid_text in processed_uids:

            continue


        # ----------------------------------------------------
        # FETCH EMAIL
        # ----------------------------------------------------

        status, msg_data = mail.uid(
            "fetch",
            uid,
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


        # ----------------------------------------------------
        # ONLY REPORT EMAILS
        # ----------------------------------------------------

        if KEYWORD not in subject.upper():

            processed_uids.add(
                uid_text
            )

            continue


        print()
        print("=" * 70)
        print("NEW REPORT EMAIL DETECTED")
        print("=" * 70)
        print(
            f"Subject : {subject}"
        )


        pdf_found = False

        processed_successfully = False


        # ----------------------------------------------------
        # FIND PDF
        # ----------------------------------------------------

        for part in msg.walk():

            filename = part.get_filename()

            content_type = part.get_content_type()


            is_pdf = (

                (
                    filename
                    and
                    filename.lower().endswith(".pdf")
                )

                or

                content_type == "application/pdf"

            )


            if not is_pdf:

                continue


            pdf_found = True


            print(
                f"PDF found: {filename or 'Report.pdf'}"
            )


            pdf_data = part.get_payload(
                decode=True
            )


            if not pdf_data:

                print(
                    "PDF is empty."
                )

                continue


            try:

                info = extract_invoice_from_pdf(
                    pdf_data
                )


                invoice_no = (
                    str(info["Invoice No"]).strip()
                )


                print(
                    f"Invoice No : {invoice_no}"
                )

                print(
                    f"Party      : {info['Party']}"
                )

                print(
                    f"Product Amt: {info['Product Amount']}"
                )

                print(
                    f"Charges    : {info['Charges']}"
                )

                print(
                    f"Final Amt  : {info['Amount']}"
                )


                # ------------------------------------------------
                # INVOICE NUMBER VALIDATION
                # ------------------------------------------------

                if not invoice_no:

                    print(
                        "WARNING: Invoice number could not "
                        "be extracted."
                    )

                    print(
                        "Email will NOT be sent to Telegram "
                        "because duplicate protection requires "
                        "an invoice number."
                    )

                    break


                # ------------------------------------------------
                # DUPLICATE CHECK
                # ------------------------------------------------

                if invoice_no in processed_invoices:

                    print(
                        f"Invoice {invoice_no} already processed."
                    )

                    print(
                        "Telegram alert SKIPPED."
                    )

                    # This Gmail UID is now also considered
                    # handled.
                    processed_successfully = True

                    break


                # ------------------------------------------------
                # SEND TELEGRAM
                # ------------------------------------------------

                process_invoice(
                    info
                )


                print(
                    "Telegram alert sent successfully."
                )


                # ------------------------------------------------
                # REMEMBER INVOICE NUMBER
                # ------------------------------------------------

                processed_invoices.add(
                    invoice_no
                )


                processed_successfully = True

                telegram_count += 1


                print(
                    f"Invoice {invoice_no} saved as processed."
                )


            except Exception as e:

                print(
                    f"Invoice processing error: {e}"
                )


            # Only process the first PDF
            break


        # ----------------------------------------------------
        # REMEMBER GMAIL UID
        # ----------------------------------------------------

        if pdf_found and processed_successfully:

            processed_uids.add(
                uid_text
            )


        elif not pdf_found:

            print(
                "REPORT email has no PDF."
            )

            # Don't repeatedly inspect a bad REPORT email.
            processed_uids.add(
                uid_text
            )


        # If PDF exists but invoice extraction failed,
        # UID is deliberately NOT marked processed.
        # It can be retried next cycle.


    # --------------------------------------------------------
    # SAVE STATE
    # --------------------------------------------------------

    state["processed_uids"] = list(
        processed_uids
    )[-1000:]


    state["processed_invoices"] = list(
        processed_invoices
    )[-2000:]


    save_state(
        state
    )


    mail.logout()


    if telegram_count:

        print()
        print(
            f"{telegram_count} NEW invoice alert(s) sent."
        )


# ============================================================
# MAIN LOOP
# ============================================================

if __name__ == "__main__":

    print()
    print("=" * 70)
    print("SVT INVOICE TELEGRAM AGENT")
    print("=" * 70)
    print("Watching Gmail for NEW REPORT invoice emails")
    print(f"Check interval : {CHECK_INTERVAL} seconds")
    print("Duplicate key  : INVOICE NUMBER")
    print("Mode           : UPDATE / NEW MAIL ONLY")
    print("Weighment agent: UNTOUCHED")
    print("=" * 70)


    while True:

        try:

            check_mail()

        except Exception as e:

            print()
            print(
                f"Email check error: {e}"
            )


        time.sleep(
            CHECK_INTERVAL
        )
