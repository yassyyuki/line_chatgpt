import os
import logging
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, status
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    MessagingApiBlob,
    TextMessage
)
from linebot.v3.messaging.models import (
    RichMenuRequest,
    RichMenuSize,
    RichMenuArea,
    RichMenuBounds,
    MessageAction,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    FollowEvent,
    UnfollowEvent,
)
from linebot.v3.exceptions import InvalidSignatureError
from google.cloud import firestore
from google.auth.exceptions import DefaultCredentialsError
from openai import OpenAI, AuthenticationError, RateLimitError, APIError
import uvicorn
import os

load_dotenv()

# 環境変数から設定値を取得
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# グローバル変数・クライアント初期化
app = FastAPI()
handler = None
line_bot_api = None
openai_client = None
db = None

# 初期化処理
def initialize_clients():
    """各APIクライアントを初期化する"""
    global handler, line_bot_api, openai_client, db

    # LINE Bot SDKの初期化
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
        raise ValueError("LINE Bot credentials missing.")
    try:
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        api_client = ApiClient(configuration)
        line_bot_api = MessagingApi(api_client)
        handler = WebhookHandler(LINE_CHANNEL_SECRET)
    except Exception as e:
        raise

    # OpenAIクライアントの初期化
    if not OPENAI_API_KEY:
        raise ValueError("OpenAI API Key missing.")
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except AuthenticationError as e:
        raise
    except Exception as e:
        raise

    # Firestoreクライアントの初期化
    try:
        db = firestore.Client()
    except DefaultCredentialsError as e:
        raise
    except Exception as e:
        raise

# アプリケーション起動時にクライアントを初期化
try:
    initialize_clients()
except Exception:
    exit(1)

# 定数
RESET_COMMAND = "リセット"
DEFAULT_SYSTEM_PROMPT = "あなたは親切なAIアシスタントです。ユーザーの質問に答えたり、会話を楽しんだりします。"
CONVERSATION_COLLECTION = 'conversations'  # Firestoreのコレクション名
MAX_HISTORY_PAIRS = 10  # 会話履歴の最大保存数（ユーザーとアシスタントのペア数）

OPENAI_MODEL = "gpt-3.5-turbo"
OPENAI_MAX_TOKENS = 200  # 応答の最大トークン数
OPENAI_TEMPERATURE = 0.7  # 応答の温度（ランダム性の度合い）

# Firestore操作関数
def get_conversation_history(user_id: str) -> list:
    if not db:
        return []
    try:
        doc_ref = db.collection(CONVERSATION_COLLECTION).document(user_id)
        doc = doc_ref.get()
        if doc.exists:
            history = doc.to_dict().get('messages', [])
            if isinstance(history, list):
                 # 履歴の長さを制限（最新の MAX_HISTORY_PAIRS * 2 件）
                return history[-(MAX_HISTORY_PAIRS * 2):]
            else:
                reset_conversation_history(user_id)
                return []
        else:
            # ドキュメントが存在しない場合は空の履歴
            return []
    except Exception as e:
        return [] # エラー時も空の履歴を返す
    
# 会話履歴をFirestoreに保存する関数
def save_conversation_history(user_id: str, history: list):
    if not db:
        return
    try:
        doc_ref = db.collection(CONVERSATION_COLLECTION).document(user_id)
        # 保存前に履歴の長さを制限
        limited_history = history[-(MAX_HISTORY_PAIRS * 2):]
        doc_ref.set({'messages': limited_history}, merge=True)
    except Exception as e:
        raise

# 会話履歴をリセットする関数
def reset_conversation_history(user_id: str):
    if not db:
        return
    try:
        doc_ref = db.collection(CONVERSATION_COLLECTION).document(user_id)
        # ドキュメント自体は残し、messagesフィールドを空にする（他の情報があれば保持される）
        doc_ref.set({'messages': []}, merge=True)
    except Exception as e:
        raise

# OpenAI API呼び出し関数
def get_openai_response(history: list) -> str | None:
    if not openai_client:
        return "申し訳ありません、AIとの接続に問題が発生しました。"

    # OpenAI APIに渡すメッセージリストを作成
    messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}] + history

    try:
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            max_tokens=OPENAI_MAX_TOKENS,
            temperature=OPENAI_TEMPERATURE,
            # stream=False # ストリーミング応答を使わない場合
        )
        ai_message = response.choices[0].message.content.strip()
        return ai_message
    except AuthenticationError as e:
        return "AIサービスの認証に失敗しました。設定を確認してください。"
    except RateLimitError as e:
        return "AIサービスの利用制限に達しました。しばらくしてからお試しください。"
    except APIError as e: # OpenAIサーバー側のエラーなど
        return f"AIサービスでエラーが発生しました (エラーコード: {e.status_code})。"
    except Exception as e: # その他の予期せぬエラー
        return "申し訳ありません、AIの応答生成中に予期せぬエラーが発生しました。"

def create_rich_menu() -> str:
    """
    リッチメニューを作成してIDを返す
    """
    rich_menu = RichMenuRequest(
        size=RichMenuSize(width=2500, height=1686),
        selected=False,  # ユーザーに最初から選択状態にするならTrue
        name="デフォルトメニュー",               # 管理画面上での名前
        chat_bar_text="メニューを開く",  # トークルーム下部のテキスト
        areas=[
            # 左半分タップで「ヘルプ」テキストを送る
            RichMenuArea(
                bounds=RichMenuBounds(x=0, y=0, width=1250, height=1686),
                action=MessageAction(label="リセット", text="リセット")
            ),
            # 右半分タップで「問い合わせ」を返信
            RichMenuArea(
                bounds=RichMenuBounds(x=1250, y=0, width=1250, height=1686),
                action=MessageAction(label="こんにちは", text="こんにちは")
            ),
        ]
    )
    resp = line_bot_api.create_rich_menu(rich_menu)
    return resp.rich_menu_id


def upload_rich_menu_image(rich_menu_id: str, image_path: str):
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    with ApiClient(configuration) as api_client:
        messaging_api_blob = MessagingApiBlob(api_client)
        with open(image_path, "rb") as img_file:
            image_data = img_file.read()
            messaging_api_blob.set_rich_menu_image(
                rich_menu_id=rich_menu_id,
                body=image_data,
                _headers={'Content-Type': 'image/jpeg'}
            )

def link_rich_menu_to_user(user_id: str, rich_menu_id: str):
    line_bot_api.link_rich_menu_to_user(user_id, rich_menu_id)


@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get('X-Line-Signature')
    body = await request.body()
    body_str = body.decode('utf-8')

    try:
        handler.handle(body_str, signature)
    except Exception as e:
        raise

    return 'OK'

# テキストメッセージイベントの処理
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_id = event.source.user_id
    user_message_text = event.message.text
    reply_token = event.reply_token

    # リセットコマンドの処理
    if user_message_text.strip().lower() == RESET_COMMAND.lower():
        reset_conversation_history(user_id)
        reply_text = "会話履歴をリセットしました。新しい会話を始めましょう！"

    # 通常の会話処理
    else:
        history = get_conversation_history(user_id)
        history.append({"role": "user", "content": user_message_text})
        ai_response = get_openai_response(history)
        if ai_response:
            history.append({"role": "assistant", "content": ai_response})
            save_conversation_history(user_id, history)
            reply_text = ai_response
        else:
            reply_text = "申し訳ありません、現在応答を生成できません。"

    # ユーザーに応答を送信
    if not line_bot_api:
        return # 返信できない場合はここで終了
    
    try:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )
    except Exception as e:
        print(f"Error sending reply: {e}")

# フォローイベントの処理
@handler.add(FollowEvent)
def handle_follow(event: FollowEvent):
    user_id = event.source.user_id
    reply_token = event.reply_token

    welcome_message = TextMessage(
        text=f"初めまして！\nGPTくんです！\n\n会話を記憶するけど、「{RESET_COMMAND}」と入力すると会話履歴をリセットするよ！"
    )

    if not line_bot_api:
        return
    try:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[welcome_message]
            )
        )
    except Exception as e:
        raise

# アンフォローイベントの処理
@handler.add(UnfollowEvent)
def handle_unfollow(event: UnfollowEvent):
    user_id = event.source.user_id

# ルートエンドポイント（動作確認用）
@app.get("/", summary="Health Check", description="Check if the API server is running.")
async def root():
    """サーバーの動作確認用エンドポイント"""
    return {"message": "AI Conversation LINE Bot API is running."}


if __name__ == "__main__":
    rm_id = create_rich_menu()
    upload_rich_menu_image(rm_id, "./app/richmenu.jpg")
    print('richmenu_id:', rm_id)
    line_bot_api.set_default_rich_menu(rm_id)
    
    port = int(os.getenv("PORT", 8000))
    if handler and line_bot_api and openai_client and db:
        uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
    else:
        exit(1)
