# LINE Trip Bot 🗺️

A LINE chatbot that reads your uncle's trip messages and formats them into a clean Google Doc — powered by Gemini AI (free).

## How to use
In your LINE group, type:
```
!trip [trip details here]
```
The bot will reply with a Google Docs link containing the formatted plan.
Sending `!trip` again updates the same doc.

## Environment Variables needed on Render:
| Variable | Where to get it |
|---|---|
| LINE_CHANNEL_ACCESS_TOKEN | LINE Developers → Messaging API |
| LINE_CHANNEL_SECRET | LINE Developers → Basic Settings |
| GEMINI_API_KEY | aistudio.google.com |
| GOOGLE_DOC_ID | From the Google Doc URL |
| GOOGLE_CREDS_JSON | Service account JSON key (paste entire contents) |

## Stack
- Flask (Python web server)
- LINE Messaging API
- Google Gemini 3.1 Flash-Lite
- Google Docs API
- Hosted on Render (free tier)
