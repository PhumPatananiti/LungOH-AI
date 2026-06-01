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

    model_name = "gemini-1.5-flash-8b"
    try:
        response = gemini_client.models.generate_content(
            model=model_name,
            contents=prompt
        )
        return response.text
    except Exception as e:
        # If it fails, try to list what is actually available for this key
        try:
            available = [m.name for m in gemini_client.models.list()]
            diag = f"\nโมเดลที่ใช้งานได้: {', '.join(available)}"
        except:
            diag = ""
        raise Exception(f"{str(e)}{diag}")


def update_google_doc(formatted_text):
    """Overwrite the Google Doc with the newly formatted trip plan."""
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

    full_text = f"แผนการเดินทาง (อัปเดตล่าสุดผ่าน LINE)\n{'='*40}\n\n{formatted_text}"
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
        TextSendMessage(text="✈️ กำลังจัดรูปแบบแผนการเดินทาง กรุณารอสักครู่...")
    )

    try:
        formatted = format_trip_with_gemini(trip_text)
        update_google_doc(formatted)

        doc_link = f"https://docs.google.com/document/d/{DOC_ID}/edit"

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
                text=f"✅ แผนการเดินทางของคุณพร้อมแล้ว!\n\n📄 ดูได้ที่นี่:\n{doc_link}\n\n(ส่ง !trip มาใหม่ได้ตลอดเวลาเพื่ออัปเดต)"
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
            TextSendMessage(text=f"❌ เกิดข้อผิดพลาดบางอย่าง กรุณาลองใหม่อีกครั้ง\n\nรายละเอียด: {str(e)}")
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
