import os
import json
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from google import genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = Flask(__name__)

# --- LINE setup ---
line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

# --- Gemini setup (new SDK - supports both AIza and AQ keys) ---
gemini_client = genai.Client(api_key=os.environ['GEMINI_API_KEY'])

# --- Google Docs setup ---
DOC_ID = os.environ['GOOGLE_DOC_ID']
try:
    creds_info = json.loads(os.environ['GOOGLE_CREDS_JSON'])
    creds = service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=['https://www.googleapis.com/auth/documents']
    )
    docs_service = build('docs', 'v1', credentials=creds)
    print("Google Docs connected successfully")
except Exception as e:
    print(f"WARNING: Google Docs setup failed: {e}")
    docs_service = None


def format_trip_with_gemini(raw_text):
    """Send raw trip text to Gemini and get a formatted version back."""
    prompt = f"""คุณคือผู้ช่วยวางแผนการเดินทางที่แสนดี
ช่วยจัดรูปแบบแผนการเดินทางต่อไปนี้ให้เป็นระเบียบ อ่านง่าย และสวยงาม

ใช้หัวข้อเหล่านี้ (เลือกใช้เฉพาะหัวข้อที่มีข้อมูล):
- 🗺️ จุดหมายปลายทาง
- 📅 วันที่เดินทาง
- 🚌 การเดินทาง
- 🏨 ที่พัก
- 📋 แผนการเดินทางรายวัน
- 💰 งบประมาณ (ถ้ามีระบุ)
- 📝 หมายเหตุ / รายละเอียดเพิ่มเติม

กฎในการจัดรูปแบบ:
- เก็บรายละเอียดเดิมไว้ทั้งหมด ห้ามตัดข้อมูลทิ้ง
- ใช้ bullet points ในแต่ละหัวข้อ
- เขียนให้กระชับแต่ชัดเจน
- ให้ผลลัพธ์เป็นภาษาไทยเสมอ

ข้อมูลทริป:
{raw_text}"""

    # ใช้โมเดลมาตรฐานฟรีในปี 2026
    model_name = "gemini-3.1-flash-lite"
    try:
        response = gemini_client.models.generate_content(
            model=model_name,
            contents=prompt
        )
        return response.text, True  # True means formatted by AI
    except Exception as e:
        # หาก AI ล้มเหลว ให้คืนค่าข้อความดิบกลับไป (Manual Mode)
        print(f"AI Error: {e}")
        return raw_text, False  # False means raw text fallback


def update_google_doc(text, is_ai_formatted):
    """Overwrite the Google Doc with either formatted or raw text."""
    if not docs_service:
        raise Exception("Google Docs ไม่ได้รับการตั้งค่าอย่างถูกต้อง")

    doc = docs_service.documents().get(documentId=DOC_ID).execute()
    content = doc.get('body', {}).get('content', [])
    end_index = content[-1].get('endIndex', 1) - 1 if content else 1

    requests = []

    if end_index > 1:
        requests.append({
            'deleteContentRange': {
                'range': {'startIndex': 1, 'endIndex': end_index}
            }
        })

    status_tag = "(จัดรูปแบบโดย AI)" if is_ai_formatted else "(บันทึกข้อมูลดิบ - AI ไม่ว่าง)"
    full_text = f"แผนการเดินทาง {status_tag}\n{'='*40}\n\n{text}"
    
    requests.append({
        'insertText': {
            'location': {'index': 1},
            'text': full_text
        }
    })

    docs_service.documents().batchUpdate(
        documentId=DOC_ID,
        body={'requests': requests}
    ).execute()


@app.route("/")
def health():
    return "Bot is running!", 200


@app.route("/webhook", methods=['POST'])
def webhook():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()

    if not text.lower().startswith("!trip"):
        return

    trip_text = text[5:].strip()

    if not trip_text:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="❓ กรุณาใส่รายละเอียดทริปหลังคำสั่ง !trip\n\nตัวอย่าง:\n!trip เชียงใหม่ 3 วัน 10-12 กรกฎาคม นั่งรถทัวร์ พักที่นิมมาน..."
            )
        )
        return

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="✈️ กำลังเตรียมแผนการเดินทางของคุณ...")
    )

    try:
        content, is_ai = format_trip_with_gemini(trip_text)
        update_google_doc(content, is_ai)

        doc_link = f"https://docs.google.com/document/d/{DOC_ID}/edit"
        
        success_msg = "✅ แผนการเดินทางอัปเดตแล้ว!"
        if not is_ai:
            success_msg += "\n(หมายเหตุ: AI ไม่ว่างชั่วคราว จึงบันทึกเป็นข้อความดิบให้ก่อนครับ)"

        source = event.source
        if hasattr(source, 'group_id'):
            target_id = source.group_id
        elif hasattr(source, 'room_id'):
            target_id = source.room_id
        else:
            target_id = source.user_id

        line_bot_api.push_message(
            target_id,
            TextSendMessage(
                text=f"{success_msg}\n\n📄 ดูได้ที่นี่:\n{doc_link}"
            )
        )

    except Exception as e:
        source = event.source
        if hasattr(source, 'group_id'):
            target_id = source.group_id
        elif hasattr(source, 'room_id'):
            target_id = source.room_id
        else:
            target_id = source.user_id

        line_bot_api.push_message(
            target_id,
            TextSendMessage(text=f"❌ เกิดข้อผิดพลาดทางเทคนิค: {str(e)}")
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
