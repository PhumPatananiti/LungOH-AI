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

# --- Environment check ---
# Fail fast with a readable message instead of a bare KeyError traceback,
# which on Render just looks like an unexplained boot loop.
REQUIRED_ENV = [
    'LINE_CHANNEL_ACCESS_TOKEN',
    'LINE_CHANNEL_SECRET',
    'GEMINI_API_KEY',
    'GOOGLE_DOC_ID',
    'GOOGLE_CREDS_JSON',
]
missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
if missing:
    raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}")

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


def extract_text(elements):
    """Collect text from a list of structural elements, including inside tables.

    Table cells are walked too: update_google_doc deletes the whole body, so
    anything we fail to read here would be silently destroyed on the next write.
    """
    text = ""
    for element in elements:
        if 'paragraph' in element:
            for run in element['paragraph'].get('elements', []):
                text += run.get('textRun', {}).get('content', '')
        elif 'table' in element:
            for row in element['table'].get('tableRows', []):
                for cell in row.get('tableCells', []):
                    text += extract_text(cell.get('content', []))
    return text


def get_current_doc_content():
    """Read all text from the Google Doc, or None if it could not be read.

    None means "unknown", and callers must never overwrite the document on it.
    Returning "" here instead would be indistinguishable from a genuinely empty
    doc, and a single transient read error would wipe every stored trip.
    """
    if not docs_service:
        return None
    try:
        doc = docs_service.documents().get(documentId=DOC_ID).execute()
        return extract_text(doc.get('body', {}).get('content', []))
    except Exception as e:
        print(f"Error reading doc: {e}")
        return None


def finish_reason(response):
    """Best-effort finish reason from a Gemini response, or None if unavailable."""
    try:
        reason = response.candidates[0].finish_reason
    except Exception:
        return None
    if reason is None:
        return None
    return getattr(reason, 'name', None) or str(reason)


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
        merged = (response.text or "").strip()

        # The model rewrites the *entire* document every time, so a bad
        # generation doesn't just produce a poor answer — it destroys trips.
        # Refuse anything that looks incomplete and fall back to appending.
        if not merged:
            raise ValueError("model returned an empty document")

        reason = finish_reason(response)
        if reason and 'STOP' not in reason.upper():
            raise ValueError(f"model stopped early ({reason})")

        if len(merged) < len(current_content) * 0.8:
            raise ValueError(
                f"model returned {len(merged)} chars for a "
                f"{len(current_content)}-char document; looks truncated"
            )

        return merged, True
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


COMMAND = "!trip"


def parse_trip_command(text):
    """Return the trip details if text is a !trip command, else None.

    The command has to end there or be followed by whitespace, so an unrelated
    word like "!tripadvisor ดีมาก" isn't filed as a trip named "advisor ดีมาก".
    """
    if not text.lower().startswith(COMMAND):
        return None
    rest = text[len(COMMAND):]
    if rest and not rest[0].isspace():
        return None
    return rest.strip()


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()

    trip_input = parse_trip_command(text)
    if trip_input is None:
        return

    if not trip_input:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="❓ วิธีใช้: !trip [ชื่อทริป] [รายละเอียด]\n\nตัวอย่าง:\n!trip สมุย ไปพะงันวันที่ 2\n!trip เชียงใหม่ จองโรงแรมแล้ว"
            )
        )
        return

    # Do the work first, then spend the single reply token on the result.
    # reply_message is free and unlimited; push_message bills against the
    # monthly quota, so we never push.
    try:
        # 1. อ่านข้อมูลเดิมจาก Doc
        current_content = get_current_doc_content()
        if current_content is None:
            # We don't know what's in the doc, and update_google_doc replaces
            # the whole body — writing now would erase every existing trip.
            raise RuntimeError("could not read the trip document; skipping write")

        # 2. ให้ AI ช่วยรวมข้อมูล (หรือ fallback เป็นข้อมูลดิบ)
        merged_text, is_ai = format_trip_with_gemini(trip_input, current_content)

        # 3. อัปเดตลง Doc
        update_google_doc(merged_text)

        doc_link = f"https://docs.google.com/document/d/{DOC_ID}/edit"

        success_msg = "✅ จัดการข้อมูลทริปเรียบร้อยแล้ว!"
        if not is_ai:
            success_msg += "\n(หมายเหตุ: ใช้โหมดบันทึกข้อมูลดิบชั่วคราว)"

        reply_text = f"{success_msg}\n\n📄 ดูแผนการเดินทางทั้งหมดได้ที่นี่:\n{doc_link}"

    except Exception as e:
        # Log the detail, but don't echo the raw exception into the chat —
        # Google API errors embed the document ID and service account email.
        print(f"Trip update failed: {e}")
        reply_text = (
            "❌ เกิดข้อผิดพลาด ไม่สามารถอัปเดตแผนการเดินทางได้ "
            "กรุณาลองใหม่อีกครั้ง\n(ข้อมูลทริปเดิมของคุณยังอยู่ครบ)"
        )

    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    except Exception as e:
        # Reply tokens expire (~1 min). If we were too slow the message is lost,
        # but the doc was still updated — log it instead of falling back to push.
        print(f"Reply failed (token likely expired): {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
