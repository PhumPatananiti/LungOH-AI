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


def get_current_doc_content():
    """Read all text from the Google Doc."""
    if not docs_service:
        return ""
    try:
        doc = docs_service.documents().get(documentId=DOC_ID).execute()
        content = doc.get('body', {}).get('content', [])
        text = ""
        for element in content:
            if 'paragraph' in element:
                for run in element.get('paragraph').get('elements'):
                    text += run.get('textRun', {}).get('content', '')
        return text
    except Exception as e:
        print(f"Error reading doc: {e}")
        return ""


def format_trip_with_gemini(new_input, current_content):
    """Smart merge new trip info into the existing document content with clear separation."""
    prompt = f"""คุณคือผู้เชี่ยวชาญการจัดหน้าเอกสารและบรรณาธิการแผนการเดินทาง
หน้าที่ของคุณคืออัปเดต "ข้อมูลทริปใหม่" ลงใน "เอกสารปัจจุบัน" โดยต้องเน้นความเป็นระเบียบและสวยงามสูงสุด

เอกสารปัจจุบัน:
---
{current_content if current_content else "(เอกสารว่างเปล่า)"}
---

ข้อมูลใหม่ที่ต้องจัดการ:
"{new_input}"

กฎเหล็กในการจัดรูปแบบเอกสาร:
1. การแยกทริป: ต้องใช้เส้นคั่นทริปที่ชัดเจน เช่น "========================================" ระหว่างแต่ละทริป
2. หัวข้อทริป: ใช้ตัวหนาและมี Emoji นำหน้า (เช่น 📍 ทริป: [ชื่อทริป]) ให้เห็นเด่นชัด
3. โครงสร้างภายใน: จัดแบ่งเป็นหัวข้อ 🗺️ จุดหมาย, 📅 วันที่, 📋 รายละเอียด/แผนงาน โดยใช้ Bullet points
4. การอัปเดต:
   - ถ้าเป็นทริปที่มีอยู่แล้ว: ให้ปรับปรุงข้อมูลในส่วนเดิมให้สมบูรณ์ขึ้น ห้ามสร้างส่วนซ้ำ
   - ถ้าเป็นทริปใหม่: ให้เพิ่มต่อท้ายไฟล์ โดยต้องใส่เส้นคั่นแยกจากทริปก่อนหน้าให้ชัดเจน
5. ความสะอาด: ห้ามมีข้อความคุยกับผู้ใช้ ให้ส่งเฉพาะ "เนื้อหาทั้งหมดของเอกสาร" ที่จัดรูปแบบแล้วเท่านั้น
6. ภาษา: ใช้ภาษาไทยที่สุภาพและอ่านง่าย

ห้ามลบข้อมูลทริปอื่นๆ ที่มีอยู่แล้วเด็ดขาด!"""

    model_name = "gemini-3.1-flash-lite"
    try:
        response = gemini_client.models.generate_content(
            model=model_name,
            contents=prompt
        )
        return response.text, True
    except Exception as e:
        print(f"AI Error: {e}")
        fallback_text = f"{current_content}\n\n[อัปเดตข้อมูลดิบ]\n{new_input}"
        return fallback_text, False


def update_google_doc(full_text):
    """Completely overwrite the Google Doc with the new merged content."""
    if not docs_service:
        raise Exception("Google Docs ไม่ได้รับการตั้งค่าอย่างถูกต้อง")

    doc = docs_service.documents().get(documentId=DOC_ID).execute()
    content = doc.get('body', {}).get('content', [])
    end_index = content[-1].get('endIndex', 1) - 1 if content else 1

    requests = []

    # Clear current content
    if end_index > 1:
        requests.append({
            'deleteContentRange': {
                'range': {'startIndex': 1, 'endIndex': end_index}
            }
        })

    # Insert new merged content
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

    trip_input = text[5:].strip()

    if not trip_input:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="❓ วิธีใช้: !trip [ชื่อทริป] [รายละเอียด]\n\nตัวอย่าง:\n!trip สมุย ไปพะงันวันที่ 2\n!trip เชียงใหม่ จองโรงแรมแล้ว"
            )
        )
        return

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="📝 กำลังจัดการข้อมูลทริปให้คุณ กรุณารอสักครู่...")
    )

    try:
        # 1. อ่านข้อมูลเดิมจาก Doc
        current_content = get_current_doc_content()
        
        # 2. ให้ AI ช่วยรวมข้อมูล (หรือ fallback เป็นข้อมูลดิบ)
        merged_text, is_ai = format_trip_with_gemini(trip_input, current_content)
        
        # 3. อัปเดตลง Doc
        update_google_doc(merged_text)

        doc_link = f"https://docs.google.com/document/d/{DOC_ID}/edit"
        
        success_msg = "✅ จัดการข้อมูลทริปเรียบร้อยแล้ว!"
        if not is_ai:
            success_msg += "\n(หมายเหตุ: ใช้โหมดบันทึกข้อมูลดิบชั่วคราว)"

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
                text=f"{success_msg}\n\n📄 ดูแผนการเดินทางทั้งหมดได้ที่นี่:\n{doc_link}"
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
            TextSendMessage(text=f"❌ เกิดข้อผิดพลาด: {str(e)}")
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
