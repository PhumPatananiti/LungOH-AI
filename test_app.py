"""Regression tests for the trip-document update logic.

These run without network access or real credentials: third-party SDKs are
stubbed before app.py is imported, and the Google Docs service is replaced
with an in-memory fake.

Run with:  python3 test_app.py
"""
import os
import sys
import types

os.environ.setdefault('LINE_CHANNEL_ACCESS_TOKEN', 'dummy')
os.environ.setdefault('LINE_CHANNEL_SECRET', 'dummy')
os.environ.setdefault('GEMINI_API_KEY', 'dummy')
os.environ.setdefault('GOOGLE_DOC_ID', 'DOCID')
os.environ.setdefault('GOOGLE_CREDS_JSON', '{}')


def _stub_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Stub:
    def __init__(self, *args, **kwargs): pass
    def __call__(self, *args, **kwargs): return _Stub()
    def __getattr__(self, name): return _Stub()


_stub_module('flask', Flask=_Stub, request=_Stub(), abort=_Stub())
_stub_module('linebot', LineBotApi=_Stub, WebhookHandler=_Stub)
_stub_module('linebot.exceptions', InvalidSignatureError=type('E', (Exception,), {}))
_stub_module('linebot.models', MessageEvent=_Stub, TextMessage=_Stub, TextSendMessage=_Stub)
_google = _stub_module('google')
_google.genai = _stub_module('google.genai', Client=_Stub)
_google.oauth2 = _stub_module('google.oauth2')
_google.oauth2.service_account = _stub_module('google.oauth2.service_account', Credentials=_Stub())
_googleapiclient = _stub_module('googleapiclient')
_googleapiclient.discovery = _stub_module('googleapiclient.discovery', build=_Stub)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402


EXISTING = (
    "📍 ทริป: สมุย\n🗺️ จุดหมาย: เกาะสมุย\n"
    "========================================\n"
    "📍 ทริป: เชียงใหม่\n🗺️ จุดหมาย: เชียงใหม่\n"
)


class FakeDocs:
    """In-memory stand-in for the Google Docs service."""

    def __init__(self, text, fail_reads=0, table_text=""):
        self.text = text
        self.fail_reads = fail_reads
        self.table_text = table_text

    def _call(self, fn):
        return types.SimpleNamespace(execute=fn)

    def documents(self):
        return types.SimpleNamespace(get=self._get, batchUpdate=self._batch_update)

    def _get(self, documentId=None):
        def run():
            if self.fail_reads > 0:
                self.fail_reads -= 1
                raise Exception("503 Backend Error (transient)")
            content = [{'paragraph': {'elements': [{'textRun': {'content': self.text}}]}}]
            if self.table_text:
                content.append({'table': {'tableRows': [{'tableCells': [
                    {'content': [{'paragraph': {'elements': [
                        {'textRun': {'content': self.table_text}}]}}]}]}]}})
            content.append({'endIndex': len(self.text) + 1})
            return {'body': {'content': content}}
        return self._call(run)

    def _batch_update(self, documentId=None, body=None):
        def run():
            for req in body['requests']:
                if 'deleteContentRange' in req:
                    self.text = ""
                if 'insertText' in req:
                    self.text = req['insertText']['text']
            return {}
        return self._call(run)


def fake_gemini(text, reason='STOP'):
    def generate_content(model=None, contents=None):
        return types.SimpleNamespace(
            text=text,
            candidates=[types.SimpleNamespace(
                finish_reason=types.SimpleNamespace(name=reason))],
        )
    return types.SimpleNamespace(models=types.SimpleNamespace(
        generate_content=generate_content))


def run_update(trip_input):
    """Mirror the sequence in handle_message. Returns True if the doc was written."""
    current = app.get_current_doc_content()
    if current is None:
        return False
    merged, _ = app.format_trip_with_gemini(trip_input, current)
    app.update_google_doc(merged)
    return True


results = []


def check(name, condition):
    results.append((name, condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}")


# 1. A transient read failure must not destroy existing trips.
app.docs_service = FakeDocs(EXISTING, fail_reads=1)
app.gemini_client = fake_gemini("📍 ทริป: ภูเก็ต\n")  # what the model returns if told the doc is empty
wrote = run_update("ภูเก็ต ไปวันที่ 5")
check("transient read failure aborts instead of overwriting",
      not wrote and "สมุย" in app.docs_service.text and "เชียงใหม่" in app.docs_service.text)

# 2a. A truncated regeneration must be rejected.
app.docs_service = FakeDocs(EXISTING)
app.gemini_client = fake_gemini("📍 ทริป: ภูเก\n", reason='MAX_TOKENS')
run_update("ภูเก็ต")
check("truncated response (MAX_TOKENS) does not shrink the doc",
      "สมุย" in app.docs_service.text and "เชียงใหม่" in app.docs_service.text)

# 2b. An empty response must be rejected.
app.docs_service = FakeDocs(EXISTING)
app.gemini_client = fake_gemini("")
run_update("ภูเก็ต")
check("empty response does not wipe the doc",
      "สมุย" in app.docs_service.text and "เชียงใหม่" in app.docs_service.text)

# 2c. A suspiciously short response must be rejected even with a clean finish.
app.docs_service = FakeDocs(EXISTING)
app.gemini_client = fake_gemini("📍 ทริป: ภูเก็ต\n")
run_update("ภูเก็ต")
check("suspiciously short response does not drop existing trips",
      "สมุย" in app.docs_service.text and "เชียงใหม่" in app.docs_service.text)

# 2d. A good response is still accepted.
app.docs_service = FakeDocs(EXISTING)
good = EXISTING + "========================================\n📍 ทริป: ภูเก็ต\n"
app.gemini_client = fake_gemini(good)
run_update("ภูเก็ต")
check("valid response is written through",
      "ภูเก็ต" in app.docs_service.text and "สมุย" in app.docs_service.text)

# 3. Command parsing.
check("'!tripadvisor ดีมาก' is not a trip command",
      app.parse_trip_command("!tripadvisor ดีมาก") is None)
check("'!trip สมุย' parses to its details",
      app.parse_trip_command("!trip สมุย") == "สมุย")
check("bare '!trip' parses to empty (usage message)",
      app.parse_trip_command("!trip") == "")
check("'!TRIP สมุย' is case-insensitive",
      app.parse_trip_command("!TRIP สมุย") == "สมุย")
check("unrelated text is ignored",
      app.parse_trip_command("สวัสดีครับ") is None)

# 5. Table content is visible to the reader.
app.docs_service = FakeDocs(EXISTING, table_text="📍 ทริป: กระบี่\n")
check("text inside tables is read, not silently dropped",
      "กระบี่" in app.get_current_doc_content())

failed = [name for name, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
