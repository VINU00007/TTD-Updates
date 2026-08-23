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

KEYWORD = "REPORT"

CHECK_INTERVAL = 30

# How many recent emails to inspect each cycle
RECENT_EMAIL_LIMIT = 50

# File used to remember processed Gmail UIDs
STATE_FILE = "processed_invoices.json"


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
# SAFE TEXT DECODING
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
# STATE MANAGEMENT
# ============================================================

def load_state():

    if not os.path.exists(STATE_FILE):

        return {
            "initialized": False,
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

    except Exception:

        return {
            "initialized": False,
            "processed_uids": []
        }


def save_state(state):

    # Keep only the latest 1000 UIDs
    state["processed_uids"] = (
        state.get("processed_uids", [])[-1000:]
    )

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
    # PRODUCT / BAGS / QUANTITY / RATE / AMOUNT
    # --------------------------------------------------------

    product = ""
    bags = ""
    quantity = ""
    rate = ""
    amount = ""

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

        amount = match.group(6).replace(
            ",",
            ""
        )


    # --------------------------------------------------------
    # AMOUNT FALLBACK
    # --------------------------------------------------------

    if not amount:

        match = re.search(
            r"Total\s+Amount\s*:\s*"
            r"([0-9,]+(?:\.\d+)?)",
            text,
            re.IGNORECASE
        )

        if match:

            amount = match.group(1).replace(
                ",",
                ""
            )


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
        "Amount": amount,
        "Place": place

    }


# ============================================================
# TELEGRAM INVOICE ALERT
# ============================================================

def process_invoice(info):

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
        f"💵 INVOICE AMOUNT : {amount_display}\n\n"

        f"📍 PLACE : {info['Place']}\n\n"

        "▣ INVOICE RECEIVED"
    )

    send_telegram(message)


# ============================================================
# INITIALIZE AGENT
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
        "Existing REPORT emails will NOT be sent to Telegram."
    )
    print(
        "Only new REPORT emails received after startup "
        "will generate alerts."
    )
    print("=" * 70)


    # Get the current highest Gmail UID.
    # Everything currently in the mailbox becomes historical.
    status, data = mail.uid(
        "search",
        None,
        "ALL"
    )


    if status == "OK" and data and data[0]:

        current_uids = data[0].split()

        for uid in current_uids:

            uid_text = uid.decode()

            state.setdefault(
                "processed_uids",
                []
            ).append(
                uid_text
            )


    state["processed_uids"] = list(
        dict.fromkeys(
            state.get("processed_uids", [])
        )
    )[-1000:]


    state["initialized"] = True

    save_state(state)


    print(
        "Invoice agent initialized successfully."
    )

    print(
        "Waiting for NEW REPORT emails..."
    )

    return state


# ============================================================
# CHECK GMAIL FOR NEW REPORT EMAILS
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


    # --------------------------------------------------------
    # SEARCH ALL MESSAGE UIDs
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


    all_uids = data[0].split() if data and data[0] else []


    # Only inspect the most recent messages.
    recent_uids = all_uids[
        -RECENT_EMAIL_LIMIT:
    ]


    new_found = 0


    for uid in recent_uids:

        uid_text = uid.decode()


        # ----------------------------------------------------
        # ALREADY PROCESSED?
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
        # ONLY REPORT SUBJECTS
        # ----------------------------------------------------

        if KEYWORD not in subject.upper():

            # Mark this UID as checked.
            # This prevents repeatedly checking unrelated
            # old emails.
            processed_uids.add(
                uid_text
            )

            continue


        new_found += 1


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
                f"PDF found: {filename or 'attachment.pdf'}"
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


                print(
                    f"Invoice No : {info['Invoice No']}"
                )

                print(
                    f"Party      : {info['Party']}"
                )

                print(
                    f"Amount     : {info['Amount']}"
                )


                # ------------------------------------------------
                # TELEGRAM
                # ------------------------------------------------

                process_invoice(
                    info
                )


                print(
                    "Telegram alert sent successfully."
                )


                processed_successfully = True


            except Exception as e:

                print(
                    f"Invoice processing error: {e}"
                )


            # Only process first PDF
            break


        # ----------------------------------------------------
        # REMEMBER EMAIL
        # ----------------------------------------------------

        if pdf_found and processed_successfully:

            processed_uids.add(
                uid_text
            )

            print(
                "Invoice UID saved as processed."
            )

        elif not pdf_found:

            print(
                "REPORT email has no PDF."
            )

            # Remember it so we don't repeatedly process
            # the same bad email.
            processed_uids.add(
                uid_text
            )


    # --------------------------------------------------------
    # SAVE STATE
    # --------------------------------------------------------

    state["processed_uids"] = list(
        processed_uids
    )[-1000:]


    save_state(
        state
    )


    mail.logout()


    if new_found:

        print(
            f"{new_found} new REPORT email(s) processed."
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
    print("Mode           : UPDATE / NEW MAIL ONLY")
    print("Weighment agent: UNTOUCHED")
    print("=" * 70)


    while True:

        try:

            check_mail()

        except Exception as e:

            print(
                f"Email check error: {e}"
            )


        time.sleep(
            CHECK_INTERVAL
        )
