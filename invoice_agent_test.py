import imaplib
import email
from email.header import decode_header
import os
import re
from io import BytesIO

import pdfplumber


# ============================================================
# CONFIGURATION
# ============================================================

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

IMAP_SERVER = "imap.gmail.com"

KEYWORD = "REPORT"


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

            output.append(
                str(part)
            )

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
        r"Vehicle\s+Number\s*:\s*([A-Z0-9]+)",
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
# CHECK GMAIL
# ============================================================

def check_mail():

    print()
    print("=" * 70)
    print("CHECKING GMAIL FOR INVOICE REPORT")
    print("=" * 70)


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

        print(
            "Could not search mailbox."
        )

        mail.logout()

        return


    mail_ids = messages[0].split()


    print(
        f"Unread emails found: {len(mail_ids)}"
    )


    for mail_id in mail_ids:

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


        sender = safe_decode(
            msg.get("From")
        )


        print()
        print(
            f"Email: {subject}"
        )


        # ----------------------------------------------------
        # SUBJECT CHECK
        # ----------------------------------------------------

        if KEYWORD not in subject.upper():

            print(
                "Not an invoice REPORT email."
            )

            # IMPORTANT:
            # Do NOT mark it Seen.
            continue


        print(
            "REPORT subject detected."
        )


        pdf_found = False


        # ----------------------------------------------------
        # ATTACHMENT CHECK
        # ----------------------------------------------------

        for part in msg.walk():

            filename = part.get_filename()

            content_type = part.get_content_type()


            if (

                (
                    filename
                    and
                    filename.lower().endswith(".pdf")
                )

                or

                content_type == "application/pdf"

            ):

                pdf_found = True


                print()
                print(
                    f"PDF FOUND: {filename}"
                )


                pdf_data = part.get_payload(
                    decode=True
                )


                if not pdf_data:

                    print(
                        "PDF attachment has no data."
                    )

                    continue


                # ------------------------------------------------
                # EXTRACT
                # ------------------------------------------------

                try:

                    info = extract_invoice_from_pdf(
                        pdf_data
                    )

                except Exception as e:

                    print(
                        f"PDF extraction error: {e}"
                    )

                    continue


                # ------------------------------------------------
                # DISPLAY
                # ------------------------------------------------

                print()
                print(
                    "=" * 70
                )

                print(
                    "INVOICE DETECTED"
                )

                print(
                    "=" * 70
                )

                print(
                    f"Invoice No : {info['Invoice No']}"
                )

                print(
                    f"Date       : {info['Invoice Date']}"
                )

                print(
                    f"Party      : {info['Party']}"
                )

                print(
                    f"Vehicle    : {info['Vehicle']}"
                )

                print(
                    f"RST        : {info['RST']}"
                )

                print(
                    f"Product    : {info['Product']}"
                )

                print(
                    f"Bags       : {info['Bags']}"
                )

                print(
                    f"Quantity   : {info['Quantity']} Qntl"
                )

                print(
                    f"Rate       : ₹{info['Rate']}"
                )

                print(
                    f"Amount     : ₹{info['Amount']}"
                )

                print(
                    f"Place      : {info['Place']}"
                )

                print(
                    "=" * 70
                )


        if not pdf_found:

            print(
                "REPORT email found, but NO PDF attachment."
            )


    mail.logout()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    if not EMAIL_USER:

        print(
            "ERROR: EMAIL_USER is not set."
        )

        raise SystemExit(1)


    if not EMAIL_PASS:

        print(
            "ERROR: EMAIL_PASS is not set."
        )

        raise SystemExit(1)


    check_mail()
