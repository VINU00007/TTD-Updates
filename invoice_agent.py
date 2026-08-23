import imaplib
import email
from email.header import decode_header
import os
import requests
import time
import json
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

KEYWORD = "REPORT"

CHECK_INTERVAL = 30

RECENT_EMAIL_LIMIT = 50

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


def money(value):

    if not value:
        return ""

    return value.replace(
        ",",
        ""
    ).strip()


def money_display(value):

    if not value:
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
            "processed_invoices": [],
            "processed_uids": []
        }

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

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
# PDF WORD HELPERS
# ============================================================

def get_words(pdf_bytes):

    words = []

    with pdfplumber.open(
        BytesIO(pdf_bytes)
    ) as pdf:

        for page_number, page in enumerate(pdf.pages):

            page_words = page.extract_words(
                x_tolerance=2,
                y_tolerance=3,
                keep_blank_chars=False
            )

            for word in page_words:

                word["page"] = page_number

                words.append(word)

    return words


def normalize_word(word):

    return re.sub(
        r"[^A-Za-z0-9@%:./-]",
        "",
        word or ""
    )


def amount_pattern(value):

    return re.fullmatch(
        r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?"
        r"|\d+(?:\.\d{1,2})?",
        value or ""
    )


def get_numeric_amounts(words):

    results = []

    for word in words:

        text = normalize_word(
            word["text"]
        )

        if amount_pattern(text):

            try:

                number = float(
                    text.replace(",", "")
                )

                # Ignore tiny integers that are clearly
                # quantities, HSN codes, etc.
                if number >= 100:

                    results.append({
                        "value": text.replace(",", ""),
                        "number": number,
                        "x0": word["x0"],
                        "x1": word["x1"],
                        "top": word["top"],
                        "bottom": word["bottom"],
                        "page": word["page"]
                    })

            except Exception:
                pass

    return results


# ============================================================
# FIND AMOUNT BESIDE A LABEL
# ============================================================

def find_amount_near_label(
    words,
    label_patterns,
    prefer_right=True
):

    candidates = []

    for i, word in enumerate(words):

        text = clean(
            word["text"]
        ).lower()

        # Look at the current word and nearby words
        combined = text

        if i + 1 < len(words):

            combined2 = clean(
                words[i + 1]["text"]
            ).lower()

            combined_pair = (
                combined + " " + combined2
            )

        else:

            combined_pair = combined


        matched = False
        label_end_index = i


        for pattern in label_patterns:

            if re.search(
                pattern,
                combined,
                re.IGNORECASE
            ):

                matched = True
                label_end_index = i
                break


            if re.search(
                pattern,
                combined_pair,
                re.IGNORECASE
            ):

                matched = True
                label_end_index = i + 1
                break


        if not matched:

            continue


        label_word = words[label_end_index]

        label_top = label_word["top"]

        label_x1 = label_word["x1"]


        # Search words on approximately the same line
        same_line = []

        for candidate in words:

            if candidate["page"] != word["page"]:
                continue

            if abs(
                candidate["top"] - label_top
            ) > 6:

                continue

            if prefer_right:

                if candidate["x0"] < label_x1 - 3:
                    continue

            text_candidate = normalize_word(
                candidate["text"]
            )

            if amount_pattern(
                text_candidate
            ):

                try:

                    number = float(
                        text_candidate.replace(",", "")
                    )

                    if number >= 100:

                        same_line.append({
                            "value": text_candidate.replace(",", ""),
                            "number": number,
                            "x0": candidate["x0"],
                            "top": candidate["top"],
                            "page": candidate["page"]
                        })

                except Exception:
                    pass


        if same_line:

            # nearest amount to the label
            same_line.sort(
                key=lambda x: x["x0"]
            )

            candidates.append(
                same_line[0]
            )


    if not candidates:

        return ""


    # Bottom-most matching label is generally the final
    # financial total in these invoices.
    candidates.sort(
        key=lambda x: (
            x["page"],
            x["top"]
        )
    )

    return candidates[-1]["value"]


# ============================================================
# FIND TAX / CHARGE LINES
# ============================================================

def extract_charge_lines(words):

    charges = []

    labels = [
        r"^igst$",
        r"^cgst$",
        r"^sgst$",
        r"^tcs$",
        r"^cess$",
        r"^weighment$"
    ]


    for i, word in enumerate(words):

        text = clean(
            word["text"]
        ).lower()

        matched_label = None

        for pattern in labels:

            if re.fullmatch(
                pattern,
                text,
                re.IGNORECASE
            ):

                matched_label = text.upper()
                break


        if not matched_label:

            continue


        label_top = word["top"]
        label_x1 = word["x1"]


        # Check same line for percentage
        percentage = ""

        for candidate in words:

            if candidate["page"] != word["page"]:
                continue

            if abs(
                candidate["top"] - label_top
            ) > 6:
                continue

            candidate_text = clean(
                candidate["text"]
            )

            if "%" in candidate_text:

                percentage = candidate_text
                break


        # Find monetary amount on same line
        possible = []

        for candidate in words:

            if candidate["page"] != word["page"]:
                continue

            if abs(
                candidate["top"] - label_top
            ) > 6:
                continue

            if candidate["x0"] < label_x1 - 3:
                continue

            candidate_text = normalize_word(
                candidate["text"]
            )

            if not amount_pattern(
                candidate_text
            ):
                continue

            try:

                number = float(
                    candidate_text.replace(",", "")
                )

                if number >= 100:

                    possible.append({
                        "value": candidate_text.replace(",", ""),
                        "x0": candidate["x0"]
                    })

            except Exception:
                pass


        if possible:

            possible.sort(
                key=lambda x: x["x0"]
            )

            amount = possible[0]["value"]

        else:

            amount = ""


        # Don't add zero-value tax rows
        if amount:

            try:

                if float(amount) != 0:

                    display_label = matched_label

                    if percentage:

                        display_label += (
                            f" {percentage}"
                        )

                    charges.append({
                        "label": display_label,
                        "amount": amount
                    })

            except Exception:
                pass


    return charges


# ============================================================
# EXTRACT PRODUCT AMOUNT
# ============================================================

def extract_product_amount(words):

    # Look for Amount column values in the main invoice table.
    # We deliberately do NOT use this as the final invoice amount.

    amount_candidates = []

    for word in words:

        text = normalize_word(
            word["text"]
        )

        if not amount_pattern(text):
            continue

        try:

            number = float(
                text.replace(",", "")
            )

            if number < 1000:
                continue

            amount_candidates.append({
                "value": text.replace(",", ""),
                "number": number,
                "top": word["top"],
                "x0": word["x0"],
                "page": word["page"]
            })

        except Exception:
            pass


    if not amount_candidates:

        return ""


    # In the invoice table, the Amount column is usually
    # on the right side. Use the upper/middle amount candidates
    # rather than the bottom-most total.
    amount_candidates.sort(
        key=lambda x: (
            x["page"],
            x["top"]
        )
    )


    # The first substantial amount is normally the product amount.
    return amount_candidates[0]["value"]


# ============================================================
# EXTRACT INVOICE
# ============================================================

def extract_invoice_from_pdf(pdf_bytes):

    words = get_words(
        pdf_bytes
    )


    # Full text
    text = " ".join(
        clean(word["text"])
        for word in words
    )


    # --------------------------------------------------------
    # INVOICE NUMBER
    # --------------------------------------------------------

    invoice_no = ""

    match = re.search(
        r"Invoice\s*NO\s*[:.]?\s*"
        r"\d{4}-\d{2}/(\d+)",
        text,
        re.IGNORECASE
    )

    if match:

        invoice_no = match.group(1)


    if not invoice_no:

        match = re.search(
            r"Invoice\s*(?:No|Number)"
            r"\s*[:.]?\s*(\d+)",
            text,
            re.IGNORECASE
        )

        if match:

            invoice_no = match.group(1)


    # --------------------------------------------------------
    # DATE
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
    # PARTY
    # --------------------------------------------------------

    party_name = ""

    match = re.search(
        r"Name\s*:\s*(.*?)"
        r"\s+State\s*:",
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
        r"Vehicle\s+Number\s*:\s*"
        r"([A-Z0-9\-]+)",
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
    # PLACE
    # --------------------------------------------------------

    place = ""

    match = re.search(
        r"City\s*:\s*"
        r"(.+?)"
        r"\s+State\s*:",
        text,
        re.IGNORECASE
    )

    if match:

        place = clean(
            match.group(1)
        )


    # --------------------------------------------------------
    # PRODUCT
    # --------------------------------------------------------

    product = ""

    match = re.search(
        r"\b\d+\s+"
        r"([A-Za-z][A-Za-z0-9 \-]+?)\s+"
        r"\d{4,8}\s+"
        r"\d+\s+"
        r"(?:\d+(?:\.\d+)?)\s+"
        r"\d+(?:\.\d+)?\s+"
        r"\d+(?:\.\d+)?",
        text
    )

    if match:

        product = clean(
            match.group(1)
        )


    # --------------------------------------------------------
    # BAGS / QUANTITY / RATE
    # --------------------------------------------------------

    bags = ""
    quantity = ""
    rate = ""


    # Main table row
    match = re.search(
        r"\b\d+\s+"
        r".+?\s+"
        r"\d{4,8}\s+"
        r"(\d+)\s+"
        r"(?:\d+(?:\.\d+)?)?\s*"
        r"(\d+(?:\.\d+)?)\s+"
        r"(\d+(?:\.\d+)?)",
        text
    )

    if match:

        bags = match.group(1)

        quantity = match.group(2)

        rate = match.group(3)


    # --------------------------------------------------------
    # PRODUCT AMOUNT
    # --------------------------------------------------------

    product_amount = extract_product_amount(
        words
    )


    # --------------------------------------------------------
    # TAX / WEIGHMENT / OTHER CHARGES
    # --------------------------------------------------------

    charges = extract_charge_lines(
        words
    )


    # --------------------------------------------------------
    # FINAL TOTAL
    #
    # Priority:
    #
    # 1. Bottom-most Net Amount / Nett Amount
    # 2. Bottom-most Total Amount
    #
    # We use the actual amount printed beside the label.
    # We DO NOT calculate the final amount ourselves.
    # --------------------------------------------------------

    final_amount = ""


    final_amount = find_amount_near_label(
        words,
        [
            r"net\s*amount",
            r"nett\s*amount"
        ]
    )


    if not final_amount:

        final_amount = find_amount_near_label(
            words,
            [
                r"total\s*amount"
            ]
        )


    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    if not final_amount:

        # Search all monetary values and use the bottom-most
        # substantial value on the invoice.
        amounts = get_numeric_amounts(
            words
        )

        if amounts:

            amounts.sort(
                key=lambda x: (
                    x["page"],
                    x["top"]
                )
            )

            final_amount = amounts[-1]["value"]


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
        "Product Amount": product_amount,
        "Charges": charges,
        "Final Amount": final_amount,
        "Place": place

    }


# ============================================================
# BUILD FINANCIAL SECTION
# ============================================================

def build_financial_section(info):

    lines = []

    product_amount = info.get(
        "Product Amount",
        ""
    )

    if product_amount:

        lines.append(
            f"💰 PRODUCT AMOUNT : "
            f"{money_display(product_amount)}"
        )


    for charge in info.get(
        "Charges",
        []
    ):

        label = charge["label"]

        amount = charge["amount"]

        if label == "WEIGHMENT":

            lines.append(
                f"⚖ WEIGHMENT : "
                f"{money_display(amount)}"
            )

        else:

            lines.append(
                f"🧾 {label} : "
                f"{money_display(amount)}"
            )


    final_amount = info.get(
        "Final Amount",
        ""
    )

    if final_amount:

        lines.append(
            "────────────────────"
        )

        lines.append(
            f"💵 FINAL AMOUNT : "
            f"{money_display(final_amount)}"
        )


    return "\n".join(
        lines
    )


# ============================================================
# TELEGRAM ALERT
# ============================================================

def process_invoice(info):

    financial_section = build_financial_section(
        info
    )


    message = (
        "🧾  INVOICE ALERT  🧾\n\n"

        f"📄 INVOICE NO : {info['Invoice No']}\n"
        f"📅 DATE : {info['Invoice Date']}\n\n"

        f"👤 {info['Party']}\n"
        f"🚛 VEHICLE : {info['Vehicle']}\n"
        f"🧾 RST : {info['RST']}\n\n"

        f"🌾 PRODUCT : {info['Product']}\n"
        f"📦 BAGS : {info['Bags']}\n"
        f"⚖ QUANTITY : {info['Quantity']} Qntl\n"
        f"💰 RATE : ₹{info['Rate']}\n\n"

        f"{financial_section}\n\n"

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


    recent_uids = all_uids[
        -RECENT_EMAIL_LIMIT:
    ]


    telegram_count = 0


    for uid in recent_uids:

        uid_text = uid.decode()


        if uid_text in processed_uids:

            continue


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
                f"PDF found: "
                f"{filename or 'Report.pdf'}"
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


                invoice_no = str(
                    info["Invoice No"]
                ).strip()


                print(
                    f"Invoice No : {invoice_no}"
                )

                print(
                    f"Party      : {info['Party']}"
                )

                print(
                    f"Product Amt: "
                    f"{info['Product Amount']}"
                )

                print(
                    f"Charges    : "
                    f"{info['Charges']}"
                )

                print(
                    f"FINAL AMT  : "
                    f"{info['Final Amount']}"
                )


                if not invoice_no:

                    print(
                        "WARNING: Invoice number could "
                        "not be extracted."
                    )

                    break


                # =================================================
                # DUPLICATE PROTECTION
                # =================================================

                if invoice_no in processed_invoices:

                    print(
                        f"Invoice {invoice_no} "
                        f"already processed."
                    )

                    print(
                        "Telegram alert SKIPPED."
                    )

                    processed_successfully = True

                    break


                # =================================================
                # SEND
                # =================================================

                process_invoice(
                    info
                )


                print(
                    "Telegram alert sent successfully."
                )


                processed_invoices.add(
                    invoice_no
                )


                processed_successfully = True

                telegram_count += 1


                print(
                    f"Invoice {invoice_no} "
                    f"saved as processed."
                )


            except Exception as e:

                print(
                    f"Invoice processing error: {e}"
                )


            break


        if pdf_found and processed_successfully:

            processed_uids.add(
                uid_text
            )

        elif not pdf_found:

            processed_uids.add(
                uid_text
            )


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
    print(
        "Watching Gmail for NEW REPORT invoice emails"
    )
    print(
        f"Check interval : {CHECK_INTERVAL} seconds"
    )
    print(
        "Duplicate key  : INVOICE NUMBER"
    )
    print(
        "Financial data : READ DIRECTLY FROM INVOICE"
    )
    print(
        "Final amount   : NET / FINAL TOTAL ON INVOICE"
    )
    print(
        "Mode           : UPDATE / NEW MAIL ONLY"
    )
    print(
        "Weighment agent: UNTOUCHED"
    )
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
