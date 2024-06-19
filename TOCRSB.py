from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import storage
import json

from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import re
import os

os.environ[
    "GOOGLE_APPLICATION_CREDENTIALS"] = r"telegram-ocr-connection-7eb1d30dee09.json"


def TOCR():
    # If modifying these scopes, delete the file token.json.
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    root_bucket_name = 'andre_ocr_bot-bucket'

    def download_from_gcs(bucket_name,
                          object_name):  # must have created service account, and set google_application_credentials as env var
        """Downloads data from the specified object in the GCS bucket."""
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        return blob.download_as_string(), blob.download_as_text()

    def upload_to_gcs(bucket_name, object_name, data):
        """Uploads data to the specified object in the GCS bucket."""
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_string(data)

    _, config_texts = download_from_gcs(root_bucket_name, 'config.txt')
    texts = config_texts.split("\n")
    for text in texts:
        if 'telegram_bot_token' in text:
            TOKEN = text.split('=')[-1].strip()
        if 'google_gemini_api_key' in text:
            google_api_key = text.split('=')[-1].strip()

    # Importing your OCR function
    from pathlib import Path
    import google.generativeai as genai

    def do_ocr(image_content_or_path):
        genai.configure(api_key=google_api_key)
        generation_config = {
            "temperature": 0.4,
            "top_p": 1,
            "top_k": 32
            }

        safety_settings = [
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            },
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_MEDIUM_AND_ABOVE"
            },
        ]

        model = genai.GenerativeModel(model_name="gemini-1.5-flash",
                                      generation_config=generation_config,
                                      safety_settings=safety_settings)

        if isinstance(image_content_or_path, (str, Path)):
            # If it's a file path, read the bytes
            image_bytes = Path(image_content_or_path).read_bytes()
        elif isinstance(image_content_or_path, bytes):
            # If it's already bytes, use it directly
            image_bytes = image_content_or_path
        else:
            raise ValueError("Invalid input type. Expected file path or bytes.")

        image_parts = [
            {
                "mime_type": "image/jpeg",
                "data": image_bytes
            },
        ]

        prompt_parts = [
            "Analyze the image, extract all the text and print as it is. Make sure the output only contains the image text and nothing else:, then add a divider line like ############## to the output (in a new line) after which you specifically identify date, time, country, match league, home team, away team, staked amount, potential winning, bet option staked, odds of bet option staked, bet status from the image\n",
            image_parts[0],
        ]

        response = model.generate_content(prompt_parts)
        response.resolve()
        return response.text

    def do_gsheet_authentication(user_id):
        """
        To authenticate end users and access user data in the bot.
        """
        creds = None
        # The file token.json stores the user's access and refresh tokens, and is
        # created automatically when the authorization flow completes for the first time.

        # specify the file path
        gcs_file_path = f"bot_user_tokens/{user_id}/token.json"

        try:
            # Try to download tokens from GCS
            gcs_tokens, _ = download_from_gcs(root_bucket_name, gcs_file_path)
            toks_dict = json.loads(gcs_tokens)
            creds = Credentials.from_authorized_user_info(toks_dict)
        except Exception as e:
            pass
        finally:
            # If there are no (valid) credentials available, let the user log in.
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    gcs_credentials, _ = download_from_gcs(root_bucket_name, "credentials.json")
                    creds_dict = json.loads(gcs_credentials)
                    flow = InstalledAppFlow.from_client_config(creds_dict, SCOPES)
                    # creds = flow.run_local_server(port=0)
                    creds = flow.run_local_server(open_browser = True)
                # Save/Upload the new credentials to GCS for the next run
                upload_to_gcs(root_bucket_name, gcs_file_path, creds.to_json())

        return creds

    def do_values_extraction(text_from_ocr):
        # Define regular expressions for each piece of information
        regex_patterns = {
            "Date": r"Date: (.+)",
            "Time": r"Time: (.+)",
            "Country": r"Country: (.+)",
            "Match League": r"Match League: (.+)",
            "Home Team": r"Home Team: (.+)",
            "Away Team": r"Away Team: (.+)",
            "Staked Amount": r"Staked Amount: (.+)",
            "Potential Winning": r"Potential Winning: (.+)",
            "Bet Option Staked": r"Bet Option Staked: (.+)",
            "The Odds of Bet Option Staked": r"Odds of Bet Option Staked: (.+)",
            "Bet Status": r"Bet Status: (.+)"
        }

        # Create a dictionary to store extracted key-value pairs
        extracted_values = {}

        # Iterate over matches and store key-value pairs in the dictionary
        for key, pattern in regex_patterns.items():
            info_pattern = re.compile(pattern, re.IGNORECASE)
            # Extract information using regular expressions
            info = re.search(info_pattern, text_from_ocr).group(1) if re.search(info_pattern, text_from_ocr) else "Not Provided"
            extracted_values[key] = info

        return extracted_values

    def do_gsheet_update(user_id, text_to_write):
        """
        Creates the Sheet the user has access to and writes text_to_write into it.
        Load pre-authorized user credentials from the environment..
        """
        creds = do_gsheet_authentication(user_id)
        title = f"Track_record_{user_id}"

        try:
            service = build("sheets", "v4", credentials=creds)
            service_drive = build("drive", "v3", credentials=creds)

            # Check if the spreadsheet exists, if not, create a new one
            spreadsheets = service_drive.files().list().execute()
            existing_spreadsheet = next((s for s in spreadsheets.get("files", []) if s["name"] == title), None)

            if existing_spreadsheet:
                spreadsheet_id = existing_spreadsheet["id"]
            else:
                # Create the spreadsheet if it doesn't exist
                spreadsheet = {"properties": {"title": title}}
                spreadsheet = service.spreadsheets().create(body=spreadsheet, fields="spreadsheetId").execute()
                spreadsheet_id = spreadsheet.get("spreadsheetId")

            # Extract values from the text
            extracted_values = do_values_extraction(text_to_write)

            # Convert extracted_values into a list of lists where each list represents a row
            values_list = [list(extracted_values.values())]
            header_values = [['Date', 'Time', 'Country', 'League', 'Home', 'Away', 'Staked Amount',
                              'Potential Winning', 'Bet Option Staked', 'Odds of Bet Option Staked',
                              'Bet Status']]  # Column names in same other as in extracted values
            # Get the next available row in the sheet
            sheet_values = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id,
                                                               range="Sheet1").execute().get("values", [])
            next_row_index = len(sheet_values) + 1

            # Update the header row with column names if it's the first entry in the sheet
            if next_row_index == 1:
                header_range = "Sheet1!A1:K1"
                service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=header_range,
                    valueInputOption="RAW",
                    body={"values": header_values}
                ).execute()

                # Append the values to the next available row
                sheet_range = f"Sheet1!A{next_row_index + 1}:K{next_row_index + 1}"  # Append to the next available row starting from column A
                value_range_body = {"values": values_list}
                service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=sheet_range,
                    valueInputOption="RAW",
                    body=value_range_body
                ).execute()
            else:
                # Append the values to the next available row
                sheet_range = f"Sheet1!A{next_row_index}:K{next_row_index}"  # Append to the next available row starting from column A
                value_range_body = {"values": values_list}
                service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id,
                    range=sheet_range,
                    valueInputOption="RAW",
                    body=value_range_body
                ).execute()

            spreadsheet_id_link = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
            return spreadsheet_id_link

        except HttpError as error:
            print(f"An error occurred: {error}")
            return error

    def image_ocr(update: Update, context: CallbackContext) -> None:
        # Check if the message contains a photo or a document
        if update.message.photo or update.message.document:
            # Determine the file_id and file_path based on the type of message
            if update.message.photo:
                file_id = update.message.photo[-1].file_id
            elif update.message.document:
                file_id = update.message.document.file_id

            # Get the file object using the file_id
            file_obj = context.bot.get_file(file_id)

            # Download the file locally
            file_path = file_obj.download()

            # Perform OCR using do_ocr function
            try:
                all_text = do_ocr(file_path)
                text = all_text.split("##############\n")[0]
                info_text = all_text.split("##############\n")[1]
                sheet_link = do_gsheet_update(update.message.from_user.id, info_text)
                update.message.reply_text("OCR Result:\n" + all_text + "\n\nFind your record at: " + sheet_link)
                # text = do_ocr(file_path)
                # update.message.reply_text("OCR Result:\n" + text)
            except Exception as e:
                update.message.reply_text("Error during OCR: {}".format(str(e)))
            finally:
                # Clean up: Remove the downloaded file after processing
                Path(file_path).unlink()
        else:
            update.message.reply_text("Please send an image.")

    def start(update: Update, context: CallbackContext) -> None:
        """
        authenticate the sheet API and 
        welcome user
        """
        user_id = update.message.from_user.id
        _ = do_gsheet_authentication(user_id)
        update.message.reply_text("Welcome! Send me an image, and I'll perform OCR on it.")

    def main() -> None:
        updater = Updater(TOKEN)  # , update_queue=my_queue)
        dp = updater.dispatcher

        dp.add_handler(CommandHandler("start", start))
        # Use both Filters.photo and Filters.document to handle both types of messages
        dp.add_handler(MessageHandler(Filters.photo | Filters.document, image_ocr))

        updater.start_polling()
        updater.idle()

    if __name__ == '__main__':
        main()


# input("+=================================================================+\n"
#       "|                     >> Telegram OCR BOT <<                      |\n"
#       "+=================================================================+\n"
#       "|                          CONTACTS                               |\n"
#       "|           Fiverr  : https://fiverr.com/delelinus                |\n"
#       "|           E-Mail  : thedelelinus@gmail.com                      |\n"
#       "+=================================================================+\n"
#       "[[+]] Press any key to Start >>")

print("\n[+] Bot is running...")
TOCR()
