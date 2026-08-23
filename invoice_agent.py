import imaplib
import email
from email.header import decode_header
import os
import requests
import time
import re
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

CHECK_INTERVAL = 30


# ============================================================
# TIME
# ============================================================

def now_ist():
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str):

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
# HELPERS
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
        "Place": place,

    }


# ============================================================
# TELEGRAM INVOICE ALERT
# ============================================================

def process_invoice(info):

    amount = info["Amount"]

    if amount:

        try:

            amount_display = (
                f"₹{float(amount):,.2f}"
            )

        except:

            amount_display = f"₹{amount}"

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
# EMAIL CHECK
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

    status, messages = mail.search(
        None,
        "(UNSEEN)"
    )

    if status != "OK":

        mail.logout()

        return


    mail_ids = messages[0].split()

    print(
        f"[{now_ist().strftime('%d-%m-%Y %I:%M:%S %p')}] "
        f"Unread emails: {len(mail_ids)}"
    )


    for mail_id in mail_ids:

        try:

            status, msg_data = mail.fetch(
                mail_id,
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


            print(
                f"Checking: {subject}"
            )


            # ------------------------------------------------
            # ONLY REPORT EMAILS
            # ------------------------------------------------

            if KEYWORD not in subject.upper():

                # IMPORTANT:
                # Leave unrelated emails untouched.
                continue


            print(
                "REPORT invoice email detected."
            )


            pdf_found = False

            processed_successfully = False


            # ------------------------------------------------
            # FIND PDF
            # ------------------------------------------------

            for part in msg.walk():

                filename = part.get_filename()

                content_type = part.get_content_type()


                if not (

                    (
                        filename
                        and
                        filename.lower().endswith(".pdf")
                    )

                    or

                    content_type == "application/pdf"

                ):

                    continue


                pdf_found = True


                print(
                    f"Invoice PDF found: {filename}"
                )


                pdf_data = part.get_payload(
                    decode=True
                )


                if not pdf_data:

                    print(
                        "PDF attachment is empty."
                    )

                    continue


                # ------------------------------------------------
                # EXTRACT
                # ------------------------------------------------

                info = extract_invoice_from_pdf(
                    pdf_data
                )


                print(
                    f"Invoice No: {info['Invoice No']}"
                )

                print(
                    f"Party: {info['Party']}"
                )

                print(
                    f"Amount: {info['Amount']}"
                )


                # ------------------------------------------------
                # SEND TELEGRAM
                # ------------------------------------------------

                process_invoice(
                    info
                )


                print(
                    "Telegram invoice alert sent."
                )


                processed_successfully = True


                # Only process the first PDF
                break


            # ------------------------------------------------
            # MARK ONLY SUCCESSFULLY PROCESSED INVOICE EMAIL
            # ------------------------------------------------

            if pdf_found and processed_successfully:

                mail.store(
                    mail_id,
                    "+FLAGS",
                    "\\Seen"
                )

                print(
                    "Invoice email marked as Seen."
                )

            elif not pdf_found:

                print(
                    "REPORT email found but no PDF. "
                    "Email left unread."
                )


        except Exception as e:

            print(
                f"Invoice processing error: {e}"
            )

            # IMPORTANT:
            # Failed invoice remains unread so it can
            # be retried on the next cycle.


    mail.logout()


# ============================================================
# MAIN CONTINUOUS LOOP
# ============================================================

if __name__ == "__main__":

    print()
    print("=" * 70)
    print("SVT INVOICE TELEGRAM AGENT")
    print("=" * 70)
    print("Watching Gmail for REPORT invoice emails")
    print(f"Check interval : {CHECK_INTERVAL} seconds")
    print("Weighment agent : UNTOUCHED")
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
