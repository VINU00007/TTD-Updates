# TTD Update Bot

This bot checks the TTD dashboard and sends a daily summary of updates tagged "New".

Website monitored:
https://ttdevasthanams.ap.gov.in/home/dashboard

Features:
- Detects updates with "New" tag
- Sends a summarized alert
- Runs daily at 8 AM

## Setup

1. Install dependencies

pip install -r requirements.txt

2. Set environment variables

BOT_TOKEN
CHAT_ID

3. Run the script

python ttd_updates.py