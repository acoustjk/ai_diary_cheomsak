import os
import json
import urllib.request
import urllib.parse
from fastapi import FastAPI, HTTPException, Request, Form, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, messaging, firestore, auth


# Initialize Firebase Admin SDK
firebase_creds_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
FIREBASE_INITIALIZED = False

if firebase_creds_json:
    try:
        creds_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin initialized successfully with environment variable credentials.")
        FIREBASE_INITIALIZED = True
    except Exception as e:
        print(f"Error initializing Firebase Admin with env var: {e}")
else:
    # Fallback to local serviceAccountKey.json
    local_key_path = "serviceAccountKey.json"
    if os.path.exists(local_key_path):
        try:
            cred = credentials.Certificate(local_key_path)
            firebase_admin.initialize_app(cred)
            print("Firebase Admin initialized with local serviceAccountKey.json.")
            FIREBASE_INITIALIZED = True
        except Exception as e:
            print(f"Error initializing Firebase Admin with local key: {e}")
    else:
        print("Firebase Admin NOT initialized: No credentials found. Push notifications will be skipped.")

if not FIREBASE_INITIALIZED:
    # Define Mock Firestore classes for local development without credentials
    import copy
    
    class MockDocumentSnapshot:
        def __init__(self, data=None):
            self.exists = data is not None
            self._data = data or {}
        def to_dict(self):
            return self._data

    class MockDocumentReference:
        def __init__(self, collection_name, doc_id, mock_db):
            self.collection_name = collection_name
            self.id = doc_id
            self.mock_db = mock_db
        def get(self, transaction=None):
            data = self.mock_db.get(self.collection_name, {}).get(self.id)
            return MockDocumentSnapshot(copy.deepcopy(data) if data is not None else None)
        def set(self, data, merge=False):
            if self.collection_name not in self.mock_db:
                self.mock_db[self.collection_name] = {}
            if merge and self.id in self.mock_db[self.collection_name]:
                self.mock_db[self.collection_name][self.id].update(data)
            else:
                self.mock_db[self.collection_name][self.id] = data
        def update(self, data):
            if self.collection_name not in self.mock_db:
                self.mock_db[self.collection_name] = {}
            if self.id not in self.mock_db[self.collection_name]:
                self.mock_db[self.collection_name][self.id] = {}
            self.mock_db[self.collection_name][self.id].update(data)

    class MockCollectionReference:
        def __init__(self, name, mock_db):
            self.name = name
            self.mock_db = mock_db
        def document(self, doc_id):
            return MockDocumentReference(self.name, doc_id, self.mock_db)

    class MockTransaction:
        def __init__(self, mock_db):
            self.mock_db = mock_db
        def get(self, ref):
            return ref.get(transaction=self)
        def update(self, ref, data):
            ref.update(data)
        def set(self, ref, data):
            ref.set(data)

    class MockFirestoreClient:
        _shared_db = {
            "config": {
                "prices": {
                    "single_price": 1000,
                    "bottle_price": 9900,
                    "pot_price": 27000,
                    "box_price": 39000
                }
            },
            "reviewers": {},
            "children": {},
            "receipts": {}
        }
        def collection(self, name):
            return MockCollectionReference(name, self._shared_db)
        def transaction(self):
            return MockTransaction(self._shared_db)

    class MockFirestoreModule:
        SERVER_TIMESTAMP = "mock_server_timestamp"
        @staticmethod
        def client():
            return MockFirestoreClient()
        @staticmethod
        def transactional(func):
            def wrapper(transaction, *args, **kwargs):
                return func(transaction, *args, **kwargs)
            return wrapper

    # Overwrite the imported firestore with the mock module
    firestore = MockFirestoreModule()
    print("Using MockFirestore client fallback.")

def send_fcm_notification(child_id: str, child_name: str):
    if not firebase_admin._apps:
        print("Firebase Admin is not initialized. Skipping notification.")
        return
    
    topic = f"child_{child_id}"
    display_name = child_name or "아이"
    
    message = messaging.Message(
        notification=messaging.Notification(
            title="✍️ 일기 작성 완료!",
            body=f"{display_name}이가 AI고치와 함께 일기 작성을 완료했어요! 결과를 확인해 보세요. 😊"
        ),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="diary_notification_channel",
                sound="default"
            )
        ),
        data={
            "childId": child_id,
            "childName": child_name or ""
        },
        topic=topic,
    )
    
    try:
        response = messaging.send(message)
        print(f"Successfully sent message to topic {topic}: {response}")
    except Exception as e:
        print(f"Failed to send FCM message: {e}")

def send_credit_notification(child_id: str, child_name: str, notification_type: str, credits_left: int = 0):
    if not firebase_admin._apps:
        print("Firebase Admin is not initialized. Skipping notification.")
        return
    
    topic = f"child_{child_id}"
    display_name = child_name or "아이"
    
    if notification_type == "low_credit":
        title = "🔋 마법이슬 부족 안내"
        body = f"{display_name}이의 남은 마법이슬이 {credits_left}개입니다. 끊김 없는 일기 작성을 위해 충전해 주세요! 🔌"
    elif notification_type == "request_credit":
        title = "🪙 마법이슬 충전 요청!"
        body = f"{display_name}이가 일기 작성을 위해 마법이슬 충전을 요청했어요! 지금 충전해 주세요. ⚡"
    else:
        return

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body
        ),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="diary_notification_channel",
                sound="default"
            )
        ),
        data={
            "childId": child_id,
            "childName": child_name or "",
            "type": "credit_alert"
        },
        topic=topic,
    )
    
    try:
        response = messaging.send(message)
        print(f"Successfully sent credit notification to topic {topic}: {response}")
    except Exception as e:
        print(f"Failed to send credit FCM message: {e}")

# Kakao REST API Key for web login
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY", "e6e12d90f46f99a6634487f83cbd62b9")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AI_TEACHER_PROMPT = """
당신은 10세 어린이의 일기 쓰기를 돕고 문해력을 함께 키워나가는 귀여운 AI 다이어리 친구 캐릭터 요정 'AI고치'입니다.
다음 규칙을 반드시 지켜서 아이의 일기를 분석하고 지정된 JSON 형식으로만 답변하세요.

[규칙]
1. 아이에게 격식 있는 존댓말 대신 친근하고 다정한 반말체(친구 어투, 예: "~했구나!", "~했어?", "~해봐!", "~하면 좋을 것 같아!")를 사용해 조언해 주세요. 가르치거나 훈계하는 차가운 어조가 아닌, 일기를 함께 쓰는 짝꿍 요정의 따뜻하고 귀여운 톤을 유지해야 합니다.
2. 아이가 입력한 일기에서 맞춤법 오류, 오타를 찾아내고 스스로 고칠 수 있게 쉬운 말로 귀여운 힌트를 던져주세요.
3. '좋았다', '나빴다' 같은 단순한 표현 대신 쓸 수 있는 더 다채롭고 생생한 표현(대안 어휘)을 2~3개 추천해 주세요.
4. 일기를 바탕으로 두 가지 점수(각 100점 만점)를 매기세요:
   - spelling_score: 맞춤법과 띄어쓰기 점수
   - expression_score: 어휘력과 표현력 점수
5. 점수에 따라 아래 3가지 도장 중 하나를 선택하세요:
   - 참 잘했어요 (두 점수의 평균이 85점 이상)
   - 좋은 시도예요 (두 점수의 평균이 60점 이상 85점 미만)
   - 힘내라 힘! (두 점수의 평균이 60점 미만)
6. 아이가 제출한 일기 원본을 바탕으로, 맞춤법과 띄어쓰기를 모두 완벽히 교정하고 좋은 추천 표현들을 자연스럽게 반영하여, 아이가 쓴 것처럼 친근하면서도 가장 모범적인 완성형 일기 전체(150~300자 내외)를 새롭게 작성하여 'corrected_diary' 필드에 추가해 주세요.

[반드시 아래의 JSON 형식으로만 답변하세요. 다른 설명은 생략하세요]
{
    "feedback": "AI고치가 이야기해주는 다정하고 귀여운 피드백 내용 (반말체)",
    "spelling_score": 90,
    "expression_score": 80,
    "stamp": "참 잘했어요",
    "corrected_diary": "AI고치가 예쁘게 다듬어 완성한 최종 모범 일기 전체 내용"
}
"""

def is_valid_content(content: str) -> bool:
    if not content:
        return False
    c = content.strip()
    return bool(c and c.lower() not in ("null", "none", "undefined", ""))

class DiaryInput(BaseModel):
    content: str
    original_content: str = None
    feedback: str = None
    api_key: str = None
    child_id: str = None
    child_name: str = None

AI_REWRITE_PROMPT = """
당신은 10세 어린이의 일기 쓰기를 돕고 문해력을 함께 키워나가는 귀여운 AI 다이어리 친구 캐릭터 요정 'AI고치'입니다.
아이가 이전 일기에서 당신이 준 피드백을 바탕으로 일기를 다시 작성(첨삭 반영)했습니다.
이전 일기 내용과 이전 피드백을 새로운 일기 내용과 비교하여 개선점을 칭찬하고 분석 결과를 지정된 JSON 형식으로만 답변하세요.

[입력 데이터 정보]
- 이전 일기: {original_content}
- 이전 피드백: {previous_feedback}
- 다시 쓴 일기: {new_content}

[규칙]
1. 아이에게 격식 있는 존댓말 대신 친근하고 다정한 반말체(친구 어투, 예: "~했구나!", "~했어?", "~해봐!", "~하면 좋을 것 같아!")를 사용해 조언해 주세요.
2. 아이가 이전 피드백을 참고하여 맞춤법 오류를 올바르게 수정했는지 확인하고 많이 기뻐하며 칭찬해 주세요.
3. 대안 어휘 추천을 실제로 활용하여 문장을 더 풍부하게 만들었는지 확인하고 칭찬해 주세요.
4. 바뀐 내용에 대해 "우와, ~하게 고쳤구나!", "내가 알려준 부분을 기억해줬네! 감동이야!" 처럼 기뻐하는 어조로 따뜻하게 격려해 주세요.
5. 새로운 일기를 바탕으로 다시 점수(각 100점 만점)를 매기세요. 이전 점수보다 개선된 점이 있다면 점수를 높여서 성취감을 느끼게 해 주세요:
   - spelling_score: 맞춤법과 띄어쓰기 점수
   - expression_score: 어휘력과 표현력 점수
6. 점수에 따라 아래 3가지 도장 중 하나를 선택하세요:
   - 참 잘했어요 (두 점수의 평균이 85점 이상)
   - 좋은 시도예요 (두 점수의 평균이 60점 이상 85점 미만)
   - 힘내라 힘! (두 점수의 평균이 60점 미만)
7. 개선 여부를 판단하여 'improved' 필드에 true 또는 false를 기록하세요. (조금이라도 나아졌다면 true)

[반드시 아래의 JSON 형식으로만 답변하세요. 다른 설명은 생략하세요]
{
    "feedback": "다시 쓴 일기에 대한 AI고치의 기쁜 칭찬과 피드백 내용 (반말체)",
    "spelling_score": 100,
    "expression_score": 90,
    "stamp": "참 잘했어요",
    "improved": true
}
"""

@app.post("/check-diary")
async def check_diary(data: DiaryInput):
    if not data.content.strip():
        raise HTTPException(status_code=400, detail="일기 내용을 입력해주세요.")
    
    # 크레딧 검증 및 차감 로직 (1차 작성 시에만 차감)
    is_rewrite = bool(is_valid_content(data.original_content) and is_valid_content(data.feedback))
    if not is_rewrite and data.child_id:
        try:
            db_client = firestore.client()
            child_ref = db_client.collection("children").document(data.child_id)
            doc = child_ref.get()
            if doc.exists:
                doc_data = doc.to_dict()
                credits = doc_data.get("credits")
                if credits is None or credits <= 0:
                    # Initialize credits field to 0 if it doesn't exist
                    if credits is None:
                        child_ref.update({
                            "credits": 0,
                            "totalCreditsGranted": 0
                        })
                    raise HTTPException(status_code=403, detail="마법이슬이 부족합니다. 보호자님 앱에서 충전해 주세요! 🪙")
                else:
                    new_credits = credits - 1
                    child_ref.update({
                        "credits": new_credits
                    })
                    # 자동 크레딧 경고 발송 (1 이하 도달 시)
                    if new_credits <= 1:
                        try:
                            send_credit_notification(data.child_id, data.child_name, "low_credit", new_credits)
                        except Exception as ne:
                            print(f"Failed to send auto low credit notification: {ne}")
            else:
                # 문서가 없으면 생성하고 기본 0개로 초기화하여 403 반환
                child_ref.set({
                    "childId": data.child_id,
                    "childName": data.child_name or "무명 어린이",
                    "credits": 0,
                    "totalCreditsGranted": 0,
                    "pairedReviewers": []
                })
                raise HTTPException(status_code=403, detail="마법이슬이 부족합니다. 보호자님 앱에서 충전해 주세요! 🪙")
        except HTTPException as he:
            raise he
        except Exception as e:
            print(f"Firestore credit verification/deduction error: {e}")
            # Firebase 연동 에러로 인해 완전히 막히지 않도록 로그를 출력하고 우회합니다.

    
    try:
        api_key = data.api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise HTTPException(status_code=500, detail="GEMINI_API_KEY가 설정되지 않았습니다. 개인 API 키를 등록하거나 서버 설정을 확인해주세요.")
            
        client = genai.Client(api_key=api_key)
        
        # 다시 쓰기 모드인 경우 프롬프트 구성 다르게 처리
        if data.original_content and data.feedback:
            prompt = AI_REWRITE_PROMPT.replace(
                "{original_content}", data.original_content
            ).replace(
                "{previous_feedback}", data.feedback
            ).replace(
                "{new_content}", data.content
            )
            system_instruction = "당신은 아이의 글쓰기 실력을 격려하고 레벨업 시켜주는 초등학교 선생님입니다."
        else:
            prompt = data.content
            system_instruction = AI_TEACHER_PROMPT

        # 1. 일기 분석 및 텍스트/점수 생성 (503 에러 발생 시 자동 재시도 적용)
        import time
        max_retries = 3
        retry_delay = 1.0
        response = None
        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=0.3,
                        response_mime_type="application/json"
                    )
                )
                break
            except Exception as e:
                err_str = str(e)
                if ("503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str) and attempt < max_retries - 1:
                    print(f"Gemini API returned 503. Retrying in {retry_delay}s... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    retry_delay *= 2.0
                else:
                    raise e
        
        result = json.loads(response.text)
        
        # 만약 고쳐 쓰기 완료(original_content가 있는 경우)이고 child_id가 전달된 경우 FCM 알림 전송
        if is_valid_content(data.original_content) and data.child_id:
            try:
                send_fcm_notification(data.child_id, data.child_name)
            except Exception as e:
                print(f"Error during push notification execution: {e}")
                
        return result
        
    except HTTPException as he:
        raise he
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            raise HTTPException(
                status_code=429,
                detail=f"AI 선생님 요청 한도(1분당 제한)를 초과했습니다. 약 1분 후에 다시 시도해주세요! ⏳ (상세: {err_str})"
            )
        elif "503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str:
            raise HTTPException(
                status_code=503,
                detail=f"현재 Google AI 서버에 일시적으로 많은 요청이 몰려 대기 중입니다. 잠시 후 다시 시도해주세요! ⏳ (상세: {err_str})"
            )
        raise HTTPException(status_code=500, detail=f"AI 서버 오류가 발생했습니다: {err_str}")


class NotificationInput(BaseModel):
    child_id: str
    child_name: str

@app.post("/send-notification")
async def send_notification(data: NotificationInput):
    if not data.child_id:
        raise HTTPException(status_code=400, detail="child_id가 필요합니다.")
    try:
        send_fcm_notification(data.child_id, data.child_name)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class CreditRequestInput(BaseModel):
    child_id: str
    child_name: str

@app.post("/request-credits")
async def request_credits(data: CreditRequestInput):
    if not data.child_id:
        raise HTTPException(status_code=400, detail="child_id가 필요합니다.")
    try:
        send_credit_notification(data.child_id, data.child_name, "request_credit")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SocialAuthInput(BaseModel):
    accessToken: str

@app.post("/auth/naver")
async def auth_naver(data: SocialAuthInput):
    if not data.accessToken:
         raise HTTPException(status_code=400, detail="access token is required")
    
    uid = None
    email = None
    nickname = None
    
    if data.accessToken.startswith("mock_"):
        raw_id = data.accessToken.replace("mock_", "")
        uid = f"naver_{raw_id}"
        email = f"{raw_id}@naver.com"
        nickname = f"네이버회원_{raw_id[:4]}"
    else:
        try:
            req = urllib.request.Request(
                "https://openapi.naver.com/v1/nid/me",
                headers={"Authorization": f"Bearer {data.accessToken}"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                if res_data.get("resultcode") == "00":
                    account = res_data.get("response", {})
                    uid = f"naver_{account.get('id')}"
                    email = account.get("email")
                    nickname = account.get("nickname")
        except Exception as e:
            print(f"Naver profile fetch failed: {e}")
            raise HTTPException(status_code=401, detail="Naver authentication failed")

    if not uid:
        raise HTTPException(status_code=401, detail="Invalid Naver token")
        
    try:
        if firebase_admin._apps:
            try:
                auth.get_user(uid)
            except Exception:
                auth.create_user(
                    uid=uid,
                    email=email,
                    display_name=nickname
                )
            custom_token = auth.create_custom_token(uid)
            return {
                "status": "success",
                "customToken": custom_token.decode('utf-8') if isinstance(custom_token, bytes) else custom_token,
                "uid": uid,
                "email": email,
                "nickname": nickname
            }
        else:
            return {
                "status": "mock_success",
                "customToken": f"mock_custom_token_for_{uid}",
                "uid": uid,
                "email": email,
                "nickname": nickname
            }
    except Exception as e:
        print(f"Error creating Firebase custom token: {e}")
        raise HTTPException(status_code=500, detail=f"Firebase custom token generation error: {e}")

@app.post("/auth/kakao")
async def auth_kakao(data: SocialAuthInput):
    if not data.accessToken:
         raise HTTPException(status_code=400, detail="access token is required")
         
    uid = None
    email = None
    nickname = None
    
    if data.accessToken.startswith("mock_"):
        raw_id = data.accessToken.replace("mock_", "")
        uid = f"kakao_{raw_id}"
        email = f"{raw_id}@kakao.com"
        nickname = f"카카오회원_{raw_id[:4]}"
    else:
        try:
            req = urllib.request.Request(
                "https://kapi.kakao.com/v2/user/me",
                headers={"Authorization": f"Bearer {data.accessToken}"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                res_data = json.loads(response.read().decode('utf-8'))
                uid = f"kakao_{res_data.get('id')}"
                kakao_account = res_data.get("kakao_account", {})
                email = kakao_account.get("email")
                properties = res_data.get("properties", {})
                nickname = properties.get("nickname")
        except Exception as e:
            print(f"Kakao profile fetch failed: {e}")
            raise HTTPException(status_code=401, detail="Kakao authentication failed")

    if not uid:
        raise HTTPException(status_code=401, detail="Invalid Kakao token")
        
    try:
        if firebase_admin._apps:
            try:
                auth.get_user(uid)
            except Exception:
                auth.create_user(
                    uid=uid,
                    email=email,
                    display_name=nickname
                )
            custom_token = auth.create_custom_token(uid)
            return {
                "status": "success",
                "customToken": custom_token.decode('utf-8') if isinstance(custom_token, bytes) else custom_token,
                "uid": uid,
                "email": email,
                "nickname": nickname
            }
        else:
            return {
                "status": "mock_success",
                "customToken": f"mock_custom_token_for_{uid}",
                "uid": uid,
                "email": email,
                "nickname": nickname
            }
    except Exception as e:
        print(f"Error creating Firebase custom token: {e}")
        raise HTTPException(status_code=500, detail=f"Firebase custom token generation error: {e}")



@app.get("/images/{filename}")
async def get_image(filename: str):
    file_path = os.path.join("images", filename)
    if os.path.exists(file_path):
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/shorten")
async def shorten(url: str):
    try:
        api_url = f"http://tinyurl.com/api-create.php?url={urllib.parse.quote(url)}"
        req = urllib.request.Request(
            api_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            short_url = response.read().decode('utf-8').strip()
            if short_url.startswith("http"):
                return {"shorturl": short_url}
            return {"shorturl": url}
    except Exception as e:
        print(f"URL Shortening failed: {e}")
        return {"shorturl": url}

@app.get("/terms", response_class=HTMLResponse)
async def get_terms():
    html_content = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI고치 서비스 이용약관</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #F7FAFC;
            color: #2D3748;
            margin: 0;
            padding: 40px 20px;
            line-height: 1.6;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: #FFFFFF;
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }
        h1 {
            font-size: 28px;
            font-weight: 700;
            color: #1A365D;
            margin-bottom: 8px;
            text-align: center;
        }
        .subtitle {
            text-align: center;
            color: #718096;
            margin-bottom: 40px;
            font-size: 14px;
        }
        h2 {
            font-size: 20px;
            font-weight: 600;
            color: #2B6CB0;
            border-bottom: 2px solid #E2E8F0;
            padding-bottom: 8px;
            margin-top: 32px;
            margin-bottom: 16px;
        }
        p, li {
            font-size: 15px;
            color: #4A5568;
        }
        ul {
            padding-left: 20px;
        }
        li {
            margin-bottom: 8px;
        }
        .footer {
            margin-top: 40px;
            text-align: center;
            font-size: 13px;
            color: #A0AEC0;
            border-top: 1px solid #E2E8F0;
            padding-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>AI고치 서비스 이용약관</h1>
        <div class="subtitle">시행일자: 2026년 6월 12일</div>
        
        <h2>제 1 조 (목적)</h2>
        <p>본 약관은 "AI고치"(이하 "서비스")가 제공하는 아동 일기 첨삭 및 보호자 관리 기능의 이용 조건 및 절차, 회사와 회원 간의 권리, 의무 및 책임 사항을 규정함을 목적으로 합니다.</p>
        
        <h2>제 2 조 (용어의 정의)</h2>
        <p>본 약관에서 사용하는 용어의 정의는 다음과 같습니다:</p>
        <ul>
            <li><strong>회원(보호자)</strong>: 본 약관에 동의하고 카카오 소셜 로그인을 통해 계정을 생성하여 서비스를 이용하는 자를 말합니다.</li>
            <li><strong>자녀(학생)</strong>: 회원의 연결 코드를 통해 서비스에 연동되어 일기 작성 및 첨삭 지도를 받는 아동을 말합니다.</li>
            <li><strong>마법이슬</strong>: 일기 분석 및 첨삭 서비스를 이용하기 위해 사용하는 서비스 내 디지털 화폐를 말합니다.</li>
        </ul>
        
        <h2>제 3 조 (약관의 효력 및 변경)</h2>
        <p>본 약관은 회원이 서비스 화면에 게시하거나 카카오 로그인 시 동의함으로써 효력이 발생합니다. 서비스는 관련 법령을 위배하지 않는 범위에서 본 약관을 개정할 수 있으며, 변경된 약관은 공지사항 또는 이메일을 통해 공지합니다.</p>
        
        <h2>제 4 조 (서비스 이용 및 마법이슬)</h2>
        <ul>
            <li>회원은 자녀의 앱을 자신의 계정과 연동하여 마법이슬을 충전하고 결제를 관리할 수 있습니다.</li>
            <li>일기 분석 및 AI 피드백 1회 작성 시 마법이슬 1개가 차감됩니다.</li>
            <li>기타 충전 및 환불 규정은 관계 법령 및 플랫폼 결제 가이드라인을 준수합니다.</li>
        </ul>
        
        <h2>제 5 조 (의무 및 책임)</h2>
        <p>회원은 카카오 계정의 관리 책임을 가지며, 타인에게 계정을 대여하거나 누출해서는 안 됩니다. 또한 자녀가 서비스를 이용하는 과정에서 부적절한 언어를 사용하지 않도록 성실히 지도하여야 합니다.</p>
        
        <div class="footer">
            © 2026 제이케이. All rights reserved.
        </div>
    </div>
</body>
</html>"""
    return html_content

@app.get("/privacy", response_class=HTMLResponse)
async def get_privacy():
    html_content = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI고치 개인정보 처리방침</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: #F7FAFC;
            color: #2D3748;
            margin: 0;
            padding: 40px 20px;
            line-height: 1.6;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: #FFFFFF;
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }
        h1 {
            font-size: 28px;
            font-weight: 700;
            color: #1A365D;
            margin-bottom: 8px;
            text-align: center;
        }
        .subtitle {
            text-align: center;
            color: #718096;
            margin-bottom: 40px;
            font-size: 14px;
        }
        h2 {
            font-size: 20px;
            font-weight: 600;
            color: #2B6CB0;
            border-bottom: 2px solid #E2E8F0;
            padding-bottom: 8px;
            margin-top: 32px;
            margin-bottom: 16px;
        }
        p, li {
            font-size: 15px;
            color: #4A5568;
        }
        ul {
            padding-left: 20px;
        }
        li {
            margin-bottom: 8px;
        }
        .footer {
            margin-top: 40px;
            text-align: center;
            font-size: 13px;
            color: #A0AEC0;
            border-top: 1px solid #E2E8F0;
            padding-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>AI고치 개인정보 처리방침</h1>
        <div class="subtitle">시행일자: 2026년 6월 12일</div>
        
        <h2>1. 개인정보의 수집 및 이용 목적</h2>
        <p>서비스는 다음의 목적을 위해 최소한의 개인정보를 수집하고 이용합니다. 수집된 개인정보는 목적 외의 용도로 사용되지 않으며, 목적이 변경될 경우 사전에 동의를 구합니다.</p>
        <ul>
            <li><strong>회원 가입 및 식별</strong>: 카카오 소셜 로그인을 통한 회원제 서비스 제공, 본인 확인 및 연동 관계 생성</li>
            <li><strong>AI 서비스 제공</strong>: 자녀의 일기 데이터 분석 및 맞춤 피드백 작성</li>
            <li><strong>알림 및 푸시 메시지</strong>: 일기 첨삭 완료 실시간 푸시 알림 발송</li>
        </ul>
        
        <h2>2. 수집하는 개인정보의 항목</h2>
        <p>서비스는 회원 가입 및 자녀 연동 시 아래와 같은 정보를 수집할 수 있습니다:</p>
        <ul>
            <li><strong>보호자(회원)</strong>: 카카오 고유 UID, 프로필 이메일 주소, 프로필 닉네임</li>
            <li><strong>자녀(학생)</strong>: 자녀의 닉네임, 작성한 일기 원본 및 AI 첨삭 결과 일기 데이터</li>
            <li><strong>자동 생성 정보</strong>: 푸시 알림 토큰(FCM), 기기 식별값(ID)</li>
        </ul>
        
        <h2>3. 개인정보의 보유 및 이용 기간</h2>
        <p>이용자의 개인정보는 서비스 탈퇴 시 즉시 파기하는 것을 원칙으로 합니다. 다만 관계 법령의 규정에 따라 일정 기간 보존할 필요가 있는 경우 해당 법령에 따라 보관합니다.</p>
        
        <h2>4. 개인정보의 파기 절차 및 방법</h2>
        <p>전자적 파일 형태로 저장된 개인정보는 기록을 재생할 수 없는 기술적 방법을 사용하여 삭제하며, 종이 문서에 출력된 개인정보는 분쇄기로 분쇄하여 파기합니다.</p>
        
        <h2>5. 정보주체의 권리 행사 방법</h2>
        <p>회원은 언제든지 자신의 개인정보를 열람, 수정할 수 있으며 회원 탈퇴(연결 해제 및 계정 삭제)를 요구할 권리가 있습니다. 회원 탈퇴는 앱 내 설정 메뉴에서 간편하게 처리하실 수 있습니다.</p>
        
        <div class="footer">
            © 2026 제이케이. All rights reserved.
        </div>
    </div>
</body>
</html>"""
    return html_content

@app.get("/naverf43348555ee55db3290df887a97ee7ea.html")
async def naver_verification():
    return FileResponse("naverf43348555ee55db3290df887a97ee7ea.html")

@app.get("/licenses", response_class=HTMLResponse)
async def get_licenses():
    html_content = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI고치 오픈소스 라이선스 고지</title>
    <link href="https://fonts.googleapis.com/css2?family=Cute+Font&family=Dongle:wght@300;400;700&family=Gaegu:wght@300;400;700&family=Gamja+Flower&family=Hi+Melody&family=Jua&family=Nanum+Pen+Script&family=Poor+Story&family=Single+Day&family=Outfit:wght@300;400;600;700;900&family=Nanum+Gothic:wght@400;700&display=swap" rel="stylesheet">
    <style>
        body {
            font-family: 'Nanum Gothic', 'Pretendard', sans-serif;
            background-color: #faf6f0;
            color: #2d3748;
            margin: 0;
            padding: 40px 20px;
            line-height: 1.6;
            background-image: radial-gradient(#ebdcd0 1px, transparent 1px);
            background-size: 24px 24px;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: #ffffff;
            padding: 40px;
            border-radius: 24px;
            border: 1.5px solid #eeddcc;
            box-shadow: 0 10px 30px rgba(183, 157, 137, 0.1);
        }
        h1 {
            font-family: 'Jua', sans-serif;
            font-size: 32px;
            color: #6c5ce7;
            margin-bottom: 8px;
            text-align: center;
        }
        .subtitle {
            text-align: center;
            color: #8a9ba8;
            margin-bottom: 30px;
            font-size: 14px;
            font-weight: bold;
        }
        .intro {
            font-size: 15px;
            color: #4a5568;
            text-align: center;
            margin-bottom: 40px;
        }
        .license-card {
            background: #fdfdfb;
            border: 1.5px solid #eeddcc;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 4px 6px rgba(183, 157, 137, 0.03);
            text-align: left;
        }
        .license-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px dashed #eeddcc;
            padding-bottom: 12px;
            margin-bottom: 16px;
        }
        .license-name {
            font-family: 'Jua', sans-serif;
            font-size: 20px;
            color: #ff8e72;
        }
        .license-type {
            font-family: 'Outfit', sans-serif;
            background: #fff5f5;
            border: 1px solid #feb2b2;
            color: #c53030;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
        }
        .license-body {
            font-family: 'Outfit', 'Courier New', Courier, monospace;
            background: #faf8f5;
            padding: 15px;
            border-radius: 10px;
            font-size: 12px;
            color: #4a5568;
            max-height: 200px;
            overflow-y: auto;
            white-space: pre-wrap;
            border: 1px solid #e2e8f0;
        }
        .footer {
            margin-top: 40px;
            text-align: center;
            font-size: 13px;
            color: #A0AEC0;
            border-top: 1.5px solid #eeddcc;
            padding-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>👾 AI고치 오픈소스 라이선스</h1>
        <div class="subtitle">Open Source Software Notice</div>
        <div class="intro">AI고치 서비스 개발에 힘이 되어준 자랑스러운 오픈소스 소프트웨어 라이선스 목록입니다.</div>
        
        <!-- FastAPI -->
        <div class="license-card">
            <div class="license-header">
                <span class="license-name">FastAPI</span>
                <span class="license-type">MIT License</span>
            </div>
            <div class="license-body">Copyright (c) 2018 Sebastián Ramírez

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.</div>
        </div>

        <!-- Uvicorn -->
        <div class="license-card">
            <div class="license-header">
                <span class="license-name">Uvicorn</span>
                <span class="license-type">BSD 3-Clause License</span>
            </div>
            <div class="license-body">Copyright (c) 2017-present, Tom Christie. All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

* Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

* Redistributions in binary form must reproduce the above copyright notice,
  this list of conditions and the following disclaimer in the documentation
  and/or other materials provided with the distribution.

* Neither the name of the copyright holder nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.</div>
        </div>

        <!-- Firebase Python Admin SDK -->
        <div class="license-card">
            <div class="license-header">
                <span class="license-name">Firebase Admin Python SDK</span>
                <span class="license-type">Apache 2.0 License</span>
            </div>
            <div class="license-body">Copyright 2017 Google Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.</div>
        </div>

        <!-- Google GenAI SDK -->
        <div class="license-card">
            <div class="license-header">
                <span class="license-name">Google GenAI SDK</span>
                <span class="license-type">Apache 2.0 License</span>
            </div>
            <div class="license-body">Copyright 2024 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.</div>
        </div>

        <!-- html2canvas -->
        <div class="license-card">
            <div class="license-header">
                <span class="license-name">html2canvas</span>
                <span class="license-type">MIT License</span>
            </div>
            <div class="license-body">Copyright (c) 2012 Niklas von Hertzen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.</div>
        </div>

        <!-- Firebase JS SDK -->
        <div class="license-card">
            <div class="license-header">
                <span class="license-name">Firebase JS SDK</span>
                <span class="license-type">Apache 2.0 License</span>
            </div>
            <div class="license-body">Copyright 2020 Google LLC

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.</div>
        </div>

        <!-- Google Fonts -->
        <div class="license-card">
            <div class="license-header">
                <span class="license-name">Google Fonts (Jua, Gaegu, Nanum Gothic, Dongle, Single Day, Outfit, etc.)</span>
                <span class="license-type">SIL Open Font License 1.1</span>
            </div>
            <div class="license-body">This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is available with a FAQ at: http://scripts.sil.org/OFL</div>
        </div>

        <div class="footer">
            © 2026 제이케이. All rights reserved.
        </div>
    </div>
</body>
</html>"""
    return html_content

# ----------------------------------------------------
# 관리자(Admin) 페이지 대시보드
# ----------------------------------------------------
ACTIVE_ADMIN_SESSIONS = set()

ADMIN_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI고치 관리자 로그인</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Nanum+Gothic:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --text-color: #f3f4f6;
            --primary-color: #8b5cf6;
            --primary-hover: #7c3aed;
            --glass-bg: rgba(17, 24, 39, 0.7);
            --glass-border: rgba(255, 255, 255, 0.08);
        }
        body {
            font-family: 'Outfit', 'Nanum Gothic', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(139, 92, 246, 0.15) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(236, 72, 153, 0.15) 0px, transparent 50%);
            color: var(--text-color);
            margin: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            overflow: hidden;
        }
        .login-card {
            background: var(--glass-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--glass-border);
            padding: 40px;
            border-radius: 24px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3);
            width: 100%;
            max-width: 400px;
            text-align: center;
            box-sizing: border-box;
            animation: fadeIn 0.8s ease-out;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .logo {
            font-size: 50px;
            margin-bottom: 15px;
            animation: bounce 2s infinite ease-in-out;
            display: inline-block;
        }
        @keyframes bounce {
            0%, 100% { transform: translateY(0); }
            50% { transform: translateY(-8px); }
        }
        h1 {
            font-size: 26px;
            font-weight: 700;
            margin: 0 0 10px 0;
            background: linear-gradient(135deg, #a78bfa, #f472b6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle {
            font-size: 13px;
            color: #9ca3af;
            margin-bottom: 30px;
            font-weight: 400;
        }
        .input-group {
            margin-bottom: 20px;
            text-align: left;
        }
        label {
            display: block;
            font-size: 12px;
            color: #9ca3af;
            margin-bottom: 8px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        input {
            width: 100%;
            padding: 14px 16px;
            background: rgba(31, 41, 55, 0.5);
            border: 1px solid var(--glass-border);
            border-radius: 12px;
            color: var(--text-color);
            font-size: 14px;
            box-sizing: border-box;
            outline: none;
            transition: all 0.3s ease;
        }
        input:focus {
            border-color: var(--primary-color);
            box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.2);
            background: rgba(31, 41, 55, 0.8);
        }
        .btn-submit {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, var(--primary-color), #ec4899);
            border: none;
            border-radius: 12px;
            color: white;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(139, 92, 246, 0.3);
            margin-top: 10px;
        }
        .btn-submit:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(139, 92, 246, 0.4);
        }
        .btn-submit:active {
            transform: translateY(0);
        }
        .error-msg {
            color: #ef4444;
            font-size: 12px;
            margin-top: 15px;
            font-weight: bold;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo">👾</div>
        <h1>AI고치 Admin</h1>
        <div class="subtitle">관리자 계정으로 로그인해주세요.</div>
        <form action="/admin/login" method="POST">
            <div class="input-group">
                <label for="username">ID</label>
                <input type="text" id="username" name="username" placeholder="아이디 입력" required autocomplete="username">
            </div>
            <div class="input-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" placeholder="비밀번호 입력" required autocomplete="current-password">
            </div>
            <button type="submit" class="btn-submit">로그인</button>
            {error_placeholder}
        </form>
    </div>
</body>
</html>"""

ADMIN_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI고치 관리자 대시보드</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Nanum+Gothic:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --text-color: #f3f4f6;
            --card-bg: rgba(17, 24, 39, 0.65);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary: #8b5cf6;
            --primary-light: #a78bfa;
            --secondary: #ec4899;
            --accent: #10b981;
        }
        body {
            font-family: 'Outfit', 'Nanum Gothic', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(139, 92, 246, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.12) 0px, transparent 50%);
            color: var(--text-color);
            margin: 0;
            padding: 40px 20px;
            min-height: 100vh;
            box-sizing: border-box;
        }
        .container {
            max-width: 1100px;
            margin: 0 auto;
            animation: fadeIn 0.6s ease-out;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(15px); }
            to { opacity: 1; transform: translateY(0); }
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 40px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 20px;
        }
        .header-title {
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .logo {
            font-size: 36px;
        }
        h1 {
            font-size: 28px;
            font-weight: 700;
            margin: 0;
            background: linear-gradient(135deg, var(--primary-light), var(--secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .btn-logout {
            padding: 10px 20px;
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #ef4444;
            border-radius: 10px;
            font-weight: bold;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        .btn-logout:hover {
            background: #ef4444;
            color: white;
            box-shadow: 0 4px 15px rgba(239, 68, 68, 0.3);
        }
        
        /* Stats Grid */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }
        .stat-card {
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            padding: 24px;
            border-radius: 20px;
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.15);
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        .stat-label {
            font-size: 12px;
            color: #9ca3af;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .stat-value {
            font-size: 32px;
            font-weight: 700;
            color: white;
        }
        .stat-value.primary {
            background: linear-gradient(135deg, #a78bfa, #c084fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .stat-value.secondary {
            background: linear-gradient(135deg, #f472b6, #fb7185);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .stat-value.accent {
            background: linear-gradient(135deg, #34d399, #6ee7b7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        /* Controls & Search */
        .controls-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            gap: 15px;
        }
        .search-wrapper {
            position: relative;
            flex: 1;
            max-width: 400px;
        }
        .search-input {
            width: 100%;
            padding: 12px 16px 12px 40px;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: white;
            font-size: 14px;
            outline: none;
            box-sizing: border-box;
            transition: all 0.3s ease;
        }
        .search-input:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.15);
        }
        .search-icon {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: #9ca3af;
            font-size: 16px;
        }
        
        /* Table styles */
        .table-container {
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            border-radius: 24px;
            overflow: hidden;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.2);
        }
        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }
        th, td {
            padding: 18px 24px;
            border-bottom: 1px solid var(--border-color);
        }
        th {
            background: rgba(31, 41, 55, 0.4);
            font-size: 13px;
            font-weight: 700;
            color: #9ca3af;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        tr:last-child td {
            border-bottom: none;
        }
        tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }
        .user-name {
            font-weight: 700;
            color: white;
            font-size: 15px;
        }
        .user-email {
            font-size: 12px;
            color: #9ca3af;
            margin-top: 3px;
        }
        .child-badge {
            background: rgba(139, 92, 246, 0.12);
            border: 1px solid rgba(139, 92, 246, 0.25);
            color: var(--primary-light);
            padding: 6px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: bold;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            margin-right: 8px;
            margin-bottom: 8px;
        }
        .credit-badge {
            background: rgba(245, 158, 11, 0.12);
            border: 1px solid rgba(245, 158, 11, 0.3);
            color: #f59e0b;
            padding: 4px 8px;
            border-radius: 8px;
            font-size: 11px;
            font-weight: bold;
        }
        .uid-badge {
            font-family: monospace;
            background: rgba(31, 41, 55, 0.6);
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 12px;
            color: #9ca3af;
        }
        .last-login-time {
            font-size: 13px;
            color: #d1d5db;
        }
        .no-data {
            text-align: center;
            color: #9ca3af;
            padding: 40px;
            font-size: 15px;
        }
        .btn-charge {
            background: linear-gradient(135deg, #f59e0b, #d97706);
            border: none;
            color: white;
            padding: 2px 6px;
            border-radius: 6px;
            font-size: 11px;
            cursor: pointer;
            margin-left: 6px;
            font-weight: bold;
            transition: all 0.2s ease;
        }
        .btn-charge:hover {
            transform: scale(1.15);
            box-shadow: 0 0 8px rgba(245, 158, 11, 0.6);
        }
        .btn-link-child {
            background: rgba(139, 92, 246, 0.12);
            border: 1px solid rgba(139, 92, 246, 0.25);
            color: #a78bfa;
            padding: 4px 8px;
            border-radius: 8px;
            font-size: 11px;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }
        .btn-link-child:hover {
            background: #8b5cf6;
            color: white;
            box-shadow: 0 0 8px rgba(139, 92, 246, 0.4);
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-title">
                <span class="logo">🔍</span>
                <h1>AI고치 서비스 어드민</h1>
            </div>
            <form action="/admin/logout" method="POST" style="margin: 0;">
                <button type="submit" class="btn-logout">로그아웃</button>
            </form>
        </header>

        <!-- Pricing Config Card -->
        <div class="card" style="margin-bottom: 40px; background: var(--card-bg); backdrop-filter: blur(12px); border: 1px solid var(--border-color); padding: 28px; border-radius: 20px; box-shadow: 0 10px 25px rgba(0, 0, 0, 0.15);">
            <h2 style="font-size: 18px; font-weight: 700; margin-top: 0; margin-bottom: 8px; background: linear-gradient(135deg, var(--primary-light), var(--secondary)); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">💰 달빛 샘터 마법이슬 판매 가격 설정</h2>
            <p style="font-size: 13px; color: #9ca3af; margin-bottom: 20px;">각 마법이슬 패키지별 금액을 설정합니다. 저장 시 보호자 앱 구매 페이지에 실시간으로 반영됩니다.</p>
            <form id="priceForm" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; align-items: flex-end;" onsubmit="savePrices(event)">
                <div class="form-group" style="margin-bottom: 0;">
                    <label for="single_price" style="display: block; font-size: 12px; color: #9ca3af; margin-bottom: 6px; font-weight: bold;">💧 작은 이슬 한 방울 (이슬 1개)</label>
                    <input type="number" id="single_price" name="single_price" value="{single_price}" required style="width: 100%; padding: 12px; background: rgba(31, 41, 55, 0.5); border: 1px solid var(--border-color); border-radius: 10px; color: white; box-sizing: border-box; font-size: 14px; outline: none;">
                </div>
                <div class="form-group" style="margin-bottom: 0;">
                    <label for="bottle_price" style="display: block; font-size: 12px; color: #9ca3af; margin-bottom: 6px; font-weight: bold;">🍾 반짝이는 이슬 병 (이슬 10개)</label>
                    <input type="number" id="bottle_price" name="bottle_price" value="{bottle_price}" required style="width: 100%; padding: 12px; background: rgba(31, 41, 55, 0.5); border: 1px solid var(--border-color); border-radius: 10px; color: white; box-sizing: border-box; font-size: 14px; outline: none;">
                </div>
                <div class="form-group" style="margin-bottom: 0;">
                    <label for="pot_price" style="display: block; font-size: 12px; color: #9ca3af; margin-bottom: 6px; font-weight: bold;">🍯 달콤한 이슬 항아리 (이슬 30개)</label>
                    <input type="number" id="pot_price" name="pot_price" value="{pot_price}" required style="width: 100%; padding: 12px; background: rgba(31, 41, 55, 0.5); border: 1px solid var(--border-color); border-radius: 10px; color: white; box-sizing: border-box; font-size: 14px; outline: none;">
                </div>
                <div class="form-group" style="margin-bottom: 0;">
                    <label for="box_price" style="display: block; font-size: 12px; color: #9ca3af; margin-bottom: 6px; font-weight: bold;">🌊 마르지 않는 샘물 상자 (구독 월 금액)</label>
                    <input type="number" id="box_price" name="box_price" value="{box_price}" required style="width: 100%; padding: 12px; background: rgba(31, 41, 55, 0.5); border: 1px solid var(--border-color); border-radius: 10px; color: white; box-sizing: border-box; font-size: 14px; outline: none;">
                </div>
                <div>
                    <button type="submit" style="width: 100%; padding: 12px; background: linear-gradient(135deg, var(--primary), var(--secondary)); border: none; border-radius: 10px; color: white; font-weight: bold; cursor: pointer; font-size: 14px; transition: all 0.2s ease;">설정 가격 저장</button>
                </div>
            </form>
        </div>

        <!-- Stats Grid -->
        <div class="stats-grid">
            <div class="stat-card">
                <span class="stat-label">전체 회원 수</span>
                <span class="stat-value primary">{total_users}명</span>
            </div>
            <div class="stat-card">
                <span class="stat-label">등록된 자녀 수</span>
                <span class="stat-value secondary">{total_children}명</span>
            </div>
            <div class="stat-card">
                <span class="stat-label">총 지급된 마법이슬</span>
                <span class="stat-value accent">{total_credits}🪙</span>
            </div>
        </div>

        <!-- Controls -->
        <div class="controls-bar">
            <div class="search-wrapper">
                <span class="search-icon">🔍</span>
                <input type="text" id="search" class="search-input" placeholder="보호자 또는 이메일 검색..." onkeyup="filterTable()">
            </div>
        </div>

        <!-- Table -->
        <div class="table-container">
            <table id="adminTable">
                <thead>
                    <tr>
                        <th style="width: 25%;">보호자 정보</th>
                        <th style="width: 20%;">최근 접속</th>
                        <th style="width: 20%;">식별 코드 (UID)</th>
                        <th style="width: 35%;">연결된 자녀 & 마법이슬</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>
        </div>
    </div>

    <script>
        function savePrices(e) {
            e.preventDefault();
            const single_price = parseInt(document.getElementById('single_price').value, 10);
            const bottle_price = parseInt(document.getElementById('bottle_price').value, 10);
            const pot_price = parseInt(document.getElementById('pot_price').value, 10);
            const box_price = parseInt(document.getElementById('box_price').value, 10);
            
            fetch('/admin/save-prices', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    single_price,
                    bottle_price,
                    pot_price,
                    box_price
                })
            })
            .then(res => {
                if (res.status === 401) {
                    alert("세션이 만료되었습니다. 다시 로그인 해주세요.");
                    window.location.reload();
                    return;
                }
                return res.json();
            })
            .then(data => {
                if (data && data.status === 'success') {
                    alert("설정 가격이 성공적으로 저장되었습니다!");
                } else {
                    alert("가격 저장 중 오류가 발생했습니다.");
                }
            })
            .catch(err => {
                console.error(err);
                alert("서버 통신 오류가 발생했습니다.");
            });
        }

        function filterTable() {
            const query = document.getElementById('search').value.toLowerCase().trim();
            const rows = document.querySelectorAll('#adminTable tbody tr');
            
            rows.forEach(row => {
                if (row.classList.contains('no-data-row')) return;
                
                const userName = row.querySelector('.user-name').innerText.toLowerCase();
                const userEmail = row.querySelector('.user-email').innerText.toLowerCase();
                const uid = row.querySelector('.uid-badge').innerText.toLowerCase();
                
                if (userName.includes(query) || userEmail.includes(query) || uid.includes(query)) {
                    row.style.display = '';
                } else {
                    row.style.display = 'none';
                }
            });
        }

        function chargeCredit(childId, childName) {
            const amountStr = prompt(`👦 [${childName}] 어린이에게 지급할 마법이슬 수량을 입력하세요:`, "10");
            if (amountStr === null) return;
            const amount = parseInt(amountStr, 10);
            if (isNaN(amount) || amount <= 0) {
                alert("올바른 수량을 입력하세요 (1 이상의 정수).");
                return;
            }
            
            fetch('/admin/add-credit', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ child_id: childId, amount: amount })
            })
            .then(res => {
                if (res.status === 401) {
                    alert("세션이 만료되었습니다. 다시 로그인 해주세요.");
                    window.location.reload();
                    return;
                }
                return res.json();
            })
            .then(data => {
                if (data && data.status === 'success') {
                    const countEl = document.getElementById(`credit-count-${childId}`);
                    const totalEl = document.getElementById(`total-count-${childId}`);
                    if (countEl) countEl.innerText = data.new_credits;
                    if (totalEl) totalEl.innerText = data.new_total;
                    alert(`[${childName}] 어린이에게 ${amount} 마법이슬을 성공적으로 지급했습니다!`);
                } else {
                    alert("마법이슬 지급 중 오류가 발생했습니다.");
                }
            })
            .catch(err => {
                console.error(err);
                alert("서버 통신 오류가 발생했습니다.");
            });
        }

        function linkChild(reviewerUid) {
            const childId = prompt("연결할 자녀의 고유 ID(childId)를 입력하세요:");
            if (!childId) return;
            const cleanId = childId.trim();
            if (cleanId.length < 3) {
                alert("올바른 자녀 ID를 입력하세요.");
                return;
            }
            
            fetch('/admin/link-child', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ reviewer_uid: reviewerUid, child_id: cleanId })
            })
            .then(res => {
                if (res.status === 401) {
                    alert("세션이 만료되었습니다. 다시 로그인 해주세요.");
                    window.location.reload();
                    return;
                }
                return res.json();
            })
            .then(data => {
                if (data && data.status === 'success') {
                    alert("자녀 연결이 성공적으로 완료되었습니다!");
                    window.location.reload();
                } else {
                    alert("자녀 연결 중 오류가 발생했습니다.");
                }
            })
            .catch(err => {
                console.error(err);
                alert("서버 통신 오류가 발생했습니다.");
            });
        }

        function chargeParentCredit(parentUid, parentName) {
            const amountStr = prompt(`👩 [${parentName}] 보호자에게 지급할 마법이슬 수량을 입력하세요:`, "10");
            if (amountStr === null) return;
            const amount = parseInt(amountStr, 10);
            if (isNaN(amount) || amount <= 0) {
                alert("올바른 수량을 입력하세요 (1 이상의 정수).");
                return;
            }
            
            fetch('/admin/add-parent-credit', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ parent_uid: parentUid, amount: amount })
            })
            .then(res => {
                if (res.status === 401) {
                    alert("세션이 만료되었습니다. 다시 로그인 해주세요.");
                    window.location.reload();
                    return;
                }
                return res.json();
            })
            .then(data => {
                if (data && data.status === 'success') {
                    const countEl = document.getElementById(`parent-credit-${parentUid}`);
                    if (countEl) countEl.innerText = data.new_credits;
                    alert(`[${parentName}] 보호자에게 ${amount} 마법이슬을 성공적으로 지급했습니다!`);
                } else {
                    alert("마법이슬 지급 중 오류가 발생했습니다.");
                }
            })
            .catch(err => {
                console.error(err);
                alert("서버 통신 오류가 발생했습니다.");
            });
        }
    </script>
</body>
</html>"""

PARENT_LOGIN_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI고치 보호자 달빛 샘터 로그인</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Nanum+Gothic:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --text-color: #f3f4f6;
            --primary-color: #f59e0b;
            --primary-hover: #d97706;
            --glass-bg: rgba(17, 24, 39, 0.7);
            --glass-border: rgba(255, 255, 255, 0.08);
        }
        body {
            font-family: 'Outfit', 'Nanum Gothic', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(245, 158, 11, 0.15) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(236, 72, 153, 0.15) 0px, transparent 50%);
            color: var(--text-color);
            margin: 0;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            padding: 40px 20px;
            box-sizing: border-box;
        }
        .login-card {
            background: var(--glass-bg);
            backdrop-filter: blur(16px);
            border: 1px solid var(--glass-border);
            padding: 40px;
            border-radius: 24px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3);
            width: 100%;
            max-width: 400px;
            text-align: center;
            box-sizing: border-box;
        }
        .logo {
            font-size: 50px;
            margin-bottom: 15px;
            display: inline-block;
        }
        h1 {
            font-size: 26px;
            font-weight: 700;
            margin: 0 0 10px 0;
            background: linear-gradient(135deg, #fde047, #f59e0b);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle {
            font-size: 13px;
            color: #9ca3af;
            margin-bottom: 30px;
        }
        .input-group {
            margin-bottom: 20px;
            text-align: left;
        }
        label {
            display: block;
            font-size: 12px;
            color: #9ca3af;
            margin-bottom: 8px;
            font-weight: 600;
        }
        input {
            width: 100%;
            padding: 14px 16px;
            background: rgba(31, 41, 55, 0.5);
            border: 1px solid var(--glass-border);
            border-radius: 12px;
            color: var(--text-color);
            font-size: 14px;
            box-sizing: border-box;
            outline: none;
            transition: all 0.3s ease;
        }
        input:focus {
            border-color: var(--primary-color);
            box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.2);
            background: rgba(31, 41, 55, 0.8);
        }
        .btn-submit {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, var(--primary-color), #ec4899);
            border: none;
            border-radius: 12px;
            color: white;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(245, 158, 11, 0.3);
            margin-top: 10px;
        }
        .btn-submit:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(245, 158, 11, 0.4);
        }
        .kakao-btn {
            background-color: #FEE500;
            color: #191919;
            border: none;
            border-radius: 12px;
            padding: 14px;
            width: 100%;
            font-weight: 700;
            font-size: 15px;
            cursor: pointer;
            margin-top: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            text-decoration: none;
            box-shadow: 0 4px 15px rgba(254, 229, 0, 0.2);
            transition: all 0.3s ease;
            box-sizing: border-box;
        }
        .kakao-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(254, 229, 0, 0.4);
        }
        .demo-btn {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #d1d5db;
            margin-top: 10px;
            width: 100%;
            padding: 12px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 13px;
            transition: all 0.2s ease;
        }
        .demo-btn:hover {
            background: rgba(255, 255, 255, 0.1);
            color: white;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="logo">🪙</div>
        <h1>AI고치 달빛 샘터</h1>
        <div class="subtitle">카카오 로그인 후 마법이슬 충전 및 선물 전송이 가능합니다.</div>
        
        <!-- Real Kakao Login Button -->
        <a href="{kakao_auth_url}" class="kakao-btn">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="margin-right: 8px;">
                <path d="M12 3C6.477 3 2 6.48 2 10.77c0 2.76 1.83 5.17 4.58 6.57-.18.66-.66 2.42-.76 2.82-.13.52.19.51.39.37.16-.1 2.57-1.74 3.6-2.44.7.1 1.43.15 2.19.15 5.523 0 10-3.48 10-7.77S17.523 3 12 3z"/>
            </svg>
            카카오 로그인
        </a>

        <!-- Developer Bypass Login -->
        <details style="margin-top: 25px; text-align: left; background: rgba(255,255,255,0.03); border: 1px solid var(--glass-border); border-radius: 12px; padding: 10px 15px;">
            <summary style="font-size: 12px; color: #9ca3af; cursor: pointer; user-select: none; font-weight: 600;">🛠️ 개발자용 바이패스 로그인</summary>
            <div style="margin-top: 15px;">
                <form action="/purchase/login" method="POST">
                    <div class="input-group">
                        <label for="uid">Kakao UID</label>
                        <input type="text" id="uid" name="uid" placeholder="예: kakao_test_parent" required value="kakao_test_parent">
                    </div>
                    <button type="submit" class="btn-submit">로그인</button>
                </form>
                <button onclick="location.href='/purchase?uid=kakao_test_parent'" class="demo-btn">데모 계정으로 바로 시작</button>
            </div>
        </details>
    </div>

    <!-- 규정 준수 푸터 -->
    <footer style="margin-top: 50px; border-top: 1px solid rgba(255, 255, 255, 0.08); padding-top: 25px; font-size: 11px; color: #9ca3af; line-height: 1.8; text-align: center; width: 100%; max-width: 400px; box-sizing: border-box;">
        <div style="font-weight: 700; color: white; margin-bottom: 6px; font-size: 12px;">(주)제이케이 (JK Inc.)</div>
        <div>대표자: 홍길동 | 사업자등록번호: 000-00-00000 | 통신판매업신고번호: 제 2026-서울강남-0000호</div>
        <div>고객센터: 02-1234-5678 (평일 10:00 ~ 17:00, 점심시간 12:00 ~ 13:00) | 이메일: support@ai-gochi.com</div>
        <div>주소: 서울특별시 강남구 테헤란로 123, 4층</div>
        <div style="margin-top: 12px; display: flex; justify-content: center; gap: 18px;">
            <a href="/terms" target="_blank" style="color: var(--primary-color); text-decoration: none; font-weight: 600;">이용약관</a>
            <a href="/privacy" target="_blank" style="color: var(--primary-color); text-decoration: none; font-weight: 600;">개인정보처리방침</a>
            <a href="/refund" target="_blank" style="color: var(--primary-color); text-decoration: none; font-weight: 600;">환불정책</a>
        </div>
        <div style="margin-top: 10px; font-size: 10px; color: #6b7280;">© 2026 제이케이. All rights reserved.</div>
    </footer>
</body>
</html>"""

PARENT_PURCHASE_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI고치 보호자 마법이슬 달빛 샘터</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Nanum+Gothic:wght@400;700&display=swap" rel="stylesheet">
    <script src="https://cdn.iamport.kr/v1/iamport.js"></script>
    <style>
        :root {
            --bg-color: #0b0f19;
            --text-color: #f3f4f6;
            --card-bg: rgba(17, 24, 39, 0.65);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary: #f59e0b;
            --primary-light: #fbbf24;
            --secondary: #ec4899;
            --accent: #10b981;
        }
        body {
            font-family: 'Outfit', 'Nanum Gothic', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(245, 158, 11, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.12) 0px, transparent 50%);
            color: var(--text-color);
            margin: 0;
            padding: 40px 20px;
            min-height: 100vh;
            box-sizing: border-box;
        }
        .container {
            max-width: 900px;
            margin: 0 auto;
            animation: fadeIn 0.6s ease-out;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(15px); }
            to { opacity: 1; transform: translateY(0); }
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 40px;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 20px;
        }
        h1 {
            font-size: 24px;
            font-weight: 700;
            margin: 0;
            background: linear-gradient(135deg, var(--primary-light), var(--secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .btn-logout {
            padding: 8px 16px;
            background: rgba(239, 68, 68, 0.15);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #ef4444;
            border-radius: 10px;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        .btn-logout:hover {
            background: #ef4444;
            color: white;
        }
        .grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 30px;
        }
        @media(max-width: 768px) {
            .grid { grid-template-columns: 1fr; }
        }
        .card {
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            padding: 30px;
            border-radius: 24px;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.2);
        }
        .profile-section {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 20px;
        }
        .avatar {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 24px;
        }
        .profile-info h3 { margin: 0; font-size: 18px; }
        .profile-info p { margin: 3px 0 0 0; font-size: 13px; color: #9ca3af; }
        
        .balance-badge {
            background: rgba(245, 158, 11, 0.1);
            border: 1px solid rgba(245, 158, 11, 0.25);
            padding: 15px 20px;
            border-radius: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
        }
        .balance-label { font-size: 14px; color: #fbbf24; font-weight: 600; }
        .balance-value { font-size: 28px; font-weight: 700; color: white; display: flex; align-items: center; gap: 8px; }
        
        /* Package grid */
        .packages-title { font-size: 18px; font-weight: 700; margin-bottom: 20px; }
        .package-item {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            padding: 20px;
            border-radius: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            transition: all 0.3s ease;
            cursor: pointer;
        }
        .package-item:hover {
            background: rgba(255, 255, 255, 0.06);
            border-color: var(--primary);
            transform: translateY(-2px);
        }
        .package-details { display: flex; flex-direction: column; gap: 4px; }
        .package-name { font-size: 16px; font-weight: 700; color: white; }
        .package-price { font-size: 13px; color: #9ca3af; }
        .btn-buy {
            padding: 10px 18px;
            background: linear-gradient(135deg, var(--primary), #ec4899);
            border: none;
            color: white;
            border-radius: 10px;
            font-weight: 700;
            font-size: 13px;
            cursor: pointer;
        }
        
        /* Form controls */
        .form-group { margin-bottom: 20px; }
        label { display: block; font-size: 13px; color: #9ca3af; margin-bottom: 8px; font-weight: 600; }
        select, input[type="number"] {
            width: 100%;
            padding: 14px;
            background: rgba(31, 41, 55, 0.5);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            color: white;
            font-size: 14px;
            outline: none;
            box-sizing: border-box;
            transition: all 0.3s ease;
        }
        select:focus, input[type="number"]:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.15);
        }
        .btn-transfer {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #10b981, #059669);
            border: none;
            border-radius: 12px;
            color: white;
            font-size: 15px;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 4px 15px rgba(16, 185, 129, 0.2);
            transition: all 0.3s ease;
        }
        .btn-transfer:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.3);
        }
        
        /* Modal */
        .modal {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0, 0, 0, 0.7);
            backdrop-filter: blur(8px);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal-content {
            background: #111827;
            border: 1px solid var(--border-color);
            border-radius: 24px;
            width: 90%;
            max-width: 400px;
            padding: 30px;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
            text-align: center;
            box-sizing: border-box;
            animation: modalSlide 0.3s ease-out;
        }
        @keyframes modalSlide {
            from { transform: translateY(20px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        .modal-title { font-size: 20px; font-weight: 700; margin-bottom: 10px; color: white; }
        .modal-desc { font-size: 13px; color: #9ca3af; margin-bottom: 25px; }
        .payment-methods { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 25px; }
        .method-card {
            border: 1.5px solid var(--border-color);
            background: rgba(255, 255, 255, 0.02);
            padding: 15px;
            border-radius: 12px;
            cursor: pointer;
            font-size: 14px;
            font-weight: 700;
            transition: all 0.2s ease;
        }
        .method-card:hover { border-color: var(--primary); background: rgba(255, 255, 255, 0.05); }
        .method-card.active { border-color: var(--primary); background: rgba(245, 158, 11, 0.1); color: var(--primary-light); }
        .btn-pay-submit {
            width: 100%;
            padding: 14px;
            background: #3b82f6;
            border: none;
            color: white;
            font-weight: bold;
            border-radius: 12px;
            cursor: pointer;
            font-size: 15px;
            transition: all 0.3s ease;
        }
        .btn-pay-submit:hover { background: #2563eb; }
        .btn-cancel {
            background: none; border: none; color: #9ca3af; cursor: pointer; font-size: 13px; margin-top: 15px; text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>AI고치 보호자 달빛 샘터</h1>
                <div style="font-size: 12px; color: #9ca3af; margin-top: 4px;">UID: {parent_uid}</div>
            </div>
            <form action="/purchase/logout" method="POST" style="margin: 0;">
                <button type="submit" class="btn-logout">로그아웃</button>
            </form>
        </header>

        <div class="grid">
            <!-- Left Side: Balance & Top up -->
            <div class="card">
                <div class="profile-section">
                    <div class="avatar">👩</div>
                    <div class="profile-info">
                        <h3>{parent_name} 보호자님</h3>
                        <p>{parent_email}</p>
                    </div>
                </div>

                <div class="balance-badge">
                    <span class="balance-label">나의 보유 마법이슬</span>
                    <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 4px;">
                        <span class="balance-value">🪙 <span id="parent-credits-val">{parent_credits}</span></span>
                        {subscription_badge_html}
                    </div>
                </div>

                <div class="packages-title">💳 마법이슬 패키지 구매</div>
                
                <div class="package-item" onclick="openPaymentModal(1, {single_price})">
                    <div class="package-details">
                        <span class="package-name">💧 작은 이슬 한 방울</span>
                        <span class="package-desc" style="font-size: 11px; color: #9ca3af; margin-top: 2px;">이슬 1개 - 맛보기용</span>
                        <span class="package-price">₩ {single_price_formatted}</span>
                    </div>
                    <button type="button" class="btn-buy" onclick="openPaymentModal(1, {single_price})">구매</button>
                </div>

                <div class="package-item" onclick="openPaymentModal(10, {bottle_price})">
                    <div class="package-details">
                        <span class="package-name">🍾 반짝이는 이슬 병</span>
                        <span class="package-desc" style="font-size: 11px; color: #9ca3af; margin-top: 2px;">이슬 10개 - 일주일 패키지</span>
                        <span class="package-price">₩ {bottle_price_formatted}</span>
                    </div>
                    <button type="button" class="btn-buy" onclick="openPaymentModal(10, {bottle_price})">구매</button>
                </div>

                <div class="package-item" onclick="openPaymentModal(30, {pot_price})">
                    <div class="package-details">
                        <span class="package-name">🍯 달콤한 이슬 항아리</span>
                        <span class="package-desc" style="font-size: 11px; color: #9ca3af; margin-top: 2px;">이슬 30개 - 한 달 열공 패키지</span>
                        <span class="package-price">₩ {pot_price_formatted}</span>
                    </div>
                    <button type="button" class="btn-buy" onclick="openPaymentModal(30, {pot_price})">구매</button>
                </div>

                <div class="package-item" onclick="openPaymentModal(100, {box_price}, true)">
                    <div class="package-details">
                        <span class="package-name">🌊 마르지 않는 샘물 상자</span>
                        <span class="package-desc" style="font-size: 11px; color: #34d399; margin-top: 2px; font-weight: 600;">정기 구독제 - 매달 이슬 자동 충전</span>
                        <span class="package-price">₩ {box_price_formatted} / 월</span>
                    </div>
                    <button type="button" class="btn-buy" style="background: linear-gradient(135deg, #10b981, #059669);" onclick="openPaymentModal(100, {box_price}, true)">구독</button>
                </div>
            </div>

            <!-- Right Side: Transfer -->
            <div class="card">
                <h2 style="font-size: 20px; font-weight: 700; margin-top: 0; background: linear-gradient(135deg, #34d399, #059669); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">🪙 자녀에게 마법이슬 보내기</h2>
                <p style="font-size: 13px; color: #9ca3af; line-height: 1.6; margin-bottom: 30px;">
                    보호자님이 보유하신 마법이슬을 연동된 자녀의 계정으로 전송합니다. 전송 즉시 자녀 앱에서 일기를 작성할 때 사용할 수 있습니다.
                </p>

                <div class="form-group">
                    <label for="childSelect">자녀 선택</label>
                    <select id="childSelect">
                        {children_options}
                    </select>
                </div>

                <div class="form-group">
                    <label for="transferAmount">보낼 마법이슬 수량</label>
                    <input type="number" id="transferAmount" min="1" value="10" placeholder="수량 입력">
                </div>

                <button class="btn-transfer" onclick="executeTransfer()">보내기</button>
            </div>
        </div>

        <!-- 규정 준수 푸터 -->
        <footer style="margin-top: 50px; border-top: 1px solid rgba(255, 255, 255, 0.08); padding-top: 25px; font-size: 11px; color: #9ca3af; line-height: 1.8; text-align: center;">
            <div style="font-weight: 700; color: white; margin-bottom: 6px; font-size: 12px;">(주)제이케이 (JK Inc.)</div>
            <div>대표자: 홍길동 | 사업자등록번호: 000-00-00000 | 통신판매업신고번호: 제 2026-서울강남-0000호</div>
            <div>고객센터: 02-1234-5678 (평일 10:00 ~ 17:00, 점심시간 12:00 ~ 13:00) | 이메일: support@ai-gochi.com</div>
            <div>주소: 서울특별시 강남구 테헤란로 123, 4층</div>
            <div style="margin-top: 12px; display: flex; justify-content: center; gap: 18px;">
                <a href="/terms" target="_blank" style="color: var(--primary-light); text-decoration: none; font-weight: 600;">이용약관</a>
                <a href="/privacy" target="_blank" style="color: var(--primary-light); text-decoration: none; font-weight: 600;">개인정보처리방침</a>
                <a href="/refund" target="_blank" style="color: var(--primary-light); text-decoration: none; font-weight: 600;">환불정책</a>
            </div>
            <div style="margin-top: 10px; font-size: 10px; color: #6b7280;">© 2026 제이케이. All rights reserved.</div>
        </footer>
    </div>

    <!-- Toss/KakaoPay Simulator Modal -->
    <div id="paymentModal" class="modal">
        <div class="modal-content">
            <div class="modal-title">💳 안전한 포트원 결제</div>
            <div class="modal-desc">결제 수단을 선택하신 뒤 결제 진행을 누르면 결제창이 나타납니다.</div>
            
            <div id="modal-product-info" style="font-size: 15px; font-weight: bold; margin-bottom: 20px; color: #fbbf24;">
                선택 상품: 마법이슬 <span id="modal-credits">0</span>개 (₩<span id="modal-price">0</span>)
            </div>

            <div class="payment-methods">
                <div class="method-card active" onclick="selectMethod(this, 'card')">신용카드</div>
                <div class="method-card" onclick="selectMethod(this, 'kakaopay')">카카오페이</div>
            </div>

            <button class="btn-pay-submit" onclick="submitMockPayment()">결제 진행</button>
            <button class="btn-cancel" onclick="closePaymentModal()">결제 취소</button>
        </div>
    </div>

    <script>
        const portOne = window.IMP;
        if (portOne) {
            portOne.init("{portone_imp_code}");
        } else {
            console.error("PortOne SDK (window.IMP) is not loaded. Safe mock/simulation fallback mode enabled.");
        }

        let currentPurchaseCredits = 0;
        let currentPurchasePrice = 0;
        let selectedPaymentMethod = 'card';
        let isSubscription = false;

        function openPaymentModal(credits, price, isSub = false) {
            currentPurchaseCredits = credits;
            currentPurchasePrice = price;
            isSubscription = isSub;
            
            const titleEl = document.querySelector('#paymentModal .modal-title');
            const descEl = document.querySelector('#paymentModal .modal-desc');
            const infoEl = document.getElementById('modal-product-info');
            
            if (isSub) {
                titleEl.innerText = "🌊 정기 구독 신청";
                descEl.innerText = "결제 수단을 선택하신 뒤 결제 진행을 누르면 매월 정기 구독 결제가 등록됩니다.";
                infoEl.innerHTML = `선택 상품: <span style="color: #10b981;">샘물 상자 정기구독</span> (매달 마법이슬 100개, 월 ₩${price.toLocaleString()})`;
            } else {
                titleEl.innerText = "💳 안전한 포트원 결제";
                descEl.innerText = "결제 수단을 선택하신 뒤 결제 진행을 누르면 결제창이 나타납니다.";
                infoEl.innerHTML = `선택 상품: 마법이슬 <span>${credits}</span>개 (₩<span>${price.toLocaleString()}</span>)`;
            }
            
            document.getElementById('paymentModal').style.display = 'flex';
        }

        function closePaymentModal() {
            document.getElementById('paymentModal').style.display = 'none';
        }

        function selectMethod(el, method) {
            document.querySelectorAll('.method-card').forEach(c => c.classList.remove('active'));
            el.classList.add('active');
            selectedPaymentMethod = method;
        }

        function submitMockPayment() {
            const portOne = window.IMP;
            if (!portOne) {
                // If PortOne SDK is blocked/not loaded, run direct verification simulation
                const mockImpUid = "mock_imp_" + new Date().getTime();
                const mockMerchantUid = "mock_order_" + new Date().getTime();
                
                if (confirm("포트원 SDK가 로드되지 않았습니다. 시뮬레이션(가상 결제)으로 진행하시겠습니까?")) {
                    fetch('/api/payment/verify', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            imp_uid: mockImpUid,
                            merchant_uid: mockMerchantUid,
                            package_type: isSubscription ? "box" : (currentPurchaseCredits === 1 ? "single" : currentPurchaseCredits === 10 ? "bottle" : "pot")
                        })
                    })
                    .then(res => res.json())
                    .then(data => {
                        if (data && data.status === 'success') {
                            alert(`[시뮬레이션] 결제가 성공적으로 승인되었습니다! 🪙 ${currentPurchaseCredits} 마법이슬이 충전되었습니다.`);
                            window.location.reload();
                        } else {
                            alert("결제 승인 검증 오류: " + (data.detail || "검증에 실패했습니다."));
                        }
                    })
                    .catch(err => {
                        console.error(err);
                        alert("서버 연결 실패로 결제 검증을 완료할 수 없습니다.");
                    });
                }
                return;
            }

            const pgCode = selectedPaymentMethod === 'kakaopay' ? 'kakaopay.TC00000000' : 'html5_inicis';
            const merchantUid = "order_" + new Date().getTime();
            
            portOne.request_pay({
                pg: pgCode,
                pay_method: "card",
                merchant_uid: merchantUid,
                name: isSubscription ? "샘물 상자 정기구독" : `마법이슬 ${currentPurchaseCredits}개`,
                amount: currentPurchasePrice,
                buyer_email: '{parent_email}'
            }, function(rsp) {
                if (rsp.success) {
                    // Send to backend for verification
                    fetch('/api/payment/verify', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            imp_uid: rsp.imp_uid,
                            merchant_uid: rsp.merchant_uid,
                            package_type: isSubscription ? "box" : (currentPurchaseCredits === 1 ? "single" : currentPurchaseCredits === 10 ? "bottle" : "pot")
                        })
                    })
                    .then(res => res.json())
                    .then(data => {
                        if (data && data.status === 'success') {
                            alert(`결제가 성공적으로 승인되었습니다! 🪙 ${currentPurchaseCredits} 마법이슬이 충전되었습니다.`);
                            window.location.reload();
                        } else {
                            alert("결제 승인 검증 오류: " + (data.detail || "검증에 실패했습니다."));
                        }
                    })
                    .catch(err => {
                        console.error(err);
                        alert("서버 연결 실패로 결제 검증을 완료할 수 없습니다.");
                    });
                } else {
                    alert("결제에 실패하였습니다: " + rsp.error_msg);
                }
            });
        }

        function executeTransfer() {
            const childId = document.getElementById('childSelect').value;
            if (!childId) {
                alert("마법이슬을 전송할 자녀를 먼저 선택해 주세요.");
                return;
            }
            const amountStr = document.getElementById('transferAmount').value;
            const amount = parseInt(amountStr, 10);
            if (isNaN(amount) || amount <= 0) {
                alert("보낼 마법이슬 수량을 올바르게 입력해 주세요 (1 이상의 정수).");
                return;
            }

            const currentVal = parseInt(document.getElementById('parent-credits-val').innerText, 10);
            if (amount > currentVal) {
                alert("보유하신 마법이슬 잔액이 부족합니다.");
                return;
            }

            fetch('/credits/transfer', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    parent_uid: '{parent_uid}',
                    child_id: childId,
                    amount: amount
                })
            })
            .then(res => {
                if (res.status === 400) {
                    return res.json().then(d => { throw new Error(d.detail || "마법이슬이 부족합니다."); });
                } else if (res.status === 403) {
                    throw new Error("자녀와 연결 권한이 없습니다.");
                } else if (!res.ok) {
                    throw new Error("서버에서 에러가 발생했습니다.");
                }
                return res.json();
            })
            .then(data => {
                if (data && data.status === 'success') {
                    document.getElementById('parent-credits-val').innerText = data.parent_credits;
                    alert(`자녀에게 성공적으로 🪙 ${amount} 마법이슬을 전송했습니다!`);
                    window.location.reload();
                }
            })
            .catch(err => {
                alert("마법이슬 전송 실패: " + err.message);
            });
        }
    </script>
</body>
</html>"""

@app.get("/purchase", response_class=HTMLResponse)
async def get_purchase(request: Request, uid: str = None):
    response = None
    if uid:
        response = RedirectResponse(url="/purchase", status_code=303)
        response.set_cookie(key="parent_session", value=uid, httponly=True)
        return response
        
    session = request.cookies.get("parent_session")
    if not session:
        # Build Kakao Authorize URL dynamically depending on request host header
        host = request.headers.get("host", "ai-gochi.com")
        scheme = "https" if "ai-gochi.com" in host or request.url.scheme == "https" else "http"
        redirect_uri = f"{scheme}://{host}/purchase/kakao/callback"
        kakao_auth_url = f"https://kauth.kakao.com/oauth/authorize?client_id={KAKAO_REST_API_KEY}&redirect_uri={urllib.parse.quote(redirect_uri)}&response_type=code"
        
        login_html = PARENT_LOGIN_HTML.replace("{kakao_auth_url}", kakao_auth_url)
        return HTMLResponse(content=login_html)
        
    try:
        db_client = firestore.client()
        
        parent_ref = db_client.collection("reviewers").document(session)
        parent_doc = parent_ref.get()
        if not parent_doc.exists:
            parent_ref.set({
                "reviewerUid": session,
                "name": "테스트 보호자",
                "credits": 0,
                "pairedChildren": []
            })
            parent_doc = parent_ref.get()
            
        parent_data = parent_doc.to_dict()
        parent_name = parent_data.get("name") or "보호자"
        parent_credits = parent_data.get("credits") or 0
        paired_children = parent_data.get("pairedChildren") or []
        
        parent_email = "이메일 정보 없음"
        if firebase_admin._apps:
            try:
                user_record = auth.get_user(session)
                parent_email = user_record.email or "이메일 정보 없음"
                parent_name = user_record.display_name or parent_name
            except Exception:
                pass
                
        children_options_html = ""
        for cid in paired_children:
            child_doc = db_client.collection("children").document(cid).get()
            cname = "등록 대기 자녀"
            ccredits = 0
            if child_doc.exists:
                cdata = child_doc.to_dict()
                cname = cdata.get("childName") or "무명 자녀"
                ccredits = cdata.get("credits") or 0
            children_options_html += f'<option value="{cid}">👦 {cname} (현재: {ccredits}🪙, ID: {cid[:6]}...)</option>'
            
        if not paired_children:
            children_options_html = '<option value="" disabled selected>연결된 자녀가 없습니다.</option>'
            
        # Fetch prices from Firestore
        prices_ref = db_client.collection("config").document("prices").get()
        prices = {}
        if prices_ref.exists:
            prices = prices_ref.to_dict()
            
        single_price = prices.get("single_price", 1000)
        bottle_price = prices.get("bottle_price", 9900)
        pot_price = prices.get("pot_price", 27000)
        box_price = prices.get("box_price", 39000)
        
        # Subscription badge
        subscription_active = parent_data.get("subscriptionActive") or False
        subscription_badge_html = ""
        if subscription_active:
            subscription_badge_html = '<span style="font-size: 11px; background: rgba(16, 185, 129, 0.2); border: 1px solid rgba(16, 185, 129, 0.4); padding: 2px 8px; border-radius: 99px; color: #34d399; font-weight: bold;">🌊 정기 구독 중</span>'
            
        portone_imp_code = os.getenv("PORTONE_IMP_CODE", "imp37887752")
        html_content = PARENT_PURCHASE_HTML.replace("{parent_uid}", session) \
                                           .replace("{parent_name}", parent_name) \
                                           .replace("{parent_email}", parent_email) \
                                           .replace("{parent_credits}", str(parent_credits)) \
                                           .replace("{children_options}", children_options_html) \
                                           .replace("{subscription_badge_html}", subscription_badge_html) \
                                           .replace("{single_price}", str(single_price)) \
                                           .replace("{bottle_price}", str(bottle_price)) \
                                           .replace("{pot_price}", str(pot_price)) \
                                           .replace("{box_price}", str(box_price)) \
                                           .replace("{single_price_formatted}", f"{single_price:,}") \
                                           .replace("{bottle_price_formatted}", f"{bottle_price:,}") \
                                           .replace("{pot_price_formatted}", f"{pot_price:,}") \
                                           .replace("{box_price_formatted}", f"{box_price:,}") \
                                           .replace("{portone_imp_code}", portone_imp_code)
        return HTMLResponse(content=html_content)
    except Exception as e:
        print(f"Purchase page error: {e}")
        portone_imp_code = os.getenv("PORTONE_IMP_CODE", "imp37887752")
        html_content = PARENT_PURCHASE_HTML.replace("{parent_uid}", session).replace("{parent_name}", "데모 보호자").replace("{parent_email}", "demo@kakao.com").replace("{parent_credits}", "0").replace("{children_options}", '<option value="mock_child">👦 데모 자녀 (현재: 0🪙)</option>').replace("{subscription_badge_html}", "").replace("{single_price}", "1000").replace("{bottle_price}", "9900").replace("{pot_price}", "27000").replace("{box_price}", "39000").replace("{single_price_formatted}", "1,000").replace("{bottle_price_formatted}", "9,900").replace("{pot_price_formatted}", "27,000").replace("{box_price_formatted}", "39,000").replace("{portone_imp_code}", portone_imp_code)
        return HTMLResponse(content=html_content)

@app.post("/purchase/login")
async def post_purchase_login(uid: str = Form(...)):
    resp = RedirectResponse(url="/purchase", status_code=303)
    resp.set_cookie(key="parent_session", value=uid, httponly=True)
    return resp

@app.post("/purchase/logout")
async def post_purchase_logout():
    resp = RedirectResponse(url="/purchase", status_code=303)
    resp.delete_cookie(key="parent_session")
    return resp

@app.get("/purchase/kakao/callback")
async def get_purchase_kakao_callback(request: Request, code: str = None, error: str = None):
    if error or not code:
        return HTMLResponse(content=f"<h3>로그인 실패: {error or '인가 코드가 없습니다.'}</h3><a href='/purchase'>돌아가기</a>")
        
    host = request.headers.get("host", "ai-gochi.com")
    scheme = "https" if "ai-gochi.com" in host or request.url.scheme == "https" else "http"
    redirect_uri = f"{scheme}://{host}/purchase/kakao/callback"
    
    token_url = "https://kauth.kakao.com/oauth/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {
        "grant_type": "authorization_code",
        "client_id": KAKAO_REST_API_KEY,
        "redirect_uri": redirect_uri,
        "code": code
    }
    
    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(token_url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            access_token = res_data.get("access_token")
    except Exception as e:
        print(f"Failed to exchange Kakao code: {e}")
        return HTMLResponse(content=f"<h3>로그인 실패 (토큰 발급 오류): {e}</h3><a href='/purchase'>돌아가기</a>")
        
    if not access_token:
        return HTMLResponse(content="<h3>로그인 실패 (토큰이 올바르지 않습니다.)</h3><a href='/purchase'>돌아가기</a>")
        
    uid = None
    email = None
    nickname = None
    try:
        req = urllib.request.Request(
            "https://kapi.kakao.com/v2/user/me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            uid = f"kakao_{res_data.get('id')}"
            kakao_account = res_data.get("kakao_account", {})
            email = kakao_account.get("email")
            properties = res_data.get("properties", {})
            nickname = properties.get("nickname")
    except Exception as e:
        print(f"Kakao profile fetch failed: {e}")
        return HTMLResponse(content=f"<h3>로그인 실패 (프로필 조회 오류): {e}</h3><a href='/purchase'>돌아가기</a>")
        
    if not uid:
        return HTMLResponse(content="<h3>로그인 실패 (사용자 식별 불가)</h3><a href='/purchase'>돌아가기</a>")
        
    # Firestore / Auth setup
    try:
        db_client = firestore.client()
        parent_ref = db_client.collection("reviewers").document(uid)
        parent_doc = parent_ref.get()
        if not parent_doc.exists:
            parent_ref.set({
                "reviewerUid": uid,
                "name": nickname or "보호자",
                "credits": 0,
                "pairedChildren": []
            })
        else:
            updates = {}
            if nickname:
                updates["name"] = nickname
            if updates:
                parent_ref.update(updates)
                
        if firebase_admin._apps:
            try:
                auth.get_user(uid)
            except Exception:
                auth.create_user(
                    uid=uid,
                    email=email,
                    display_name=nickname
                )
    except Exception as e:
        print(f"Firestore parent setup failed during callback: {e}")
        
    resp = RedirectResponse(url="/purchase", status_code=303)
    resp.set_cookie(key="parent_session", value=uid, httponly=True)
    return resp

class AddCreditInput(BaseModel):
    child_id: str
    amount: int

class LinkChildInput(BaseModel):
    reviewer_uid: str
    child_id: str

@app.post("/admin/add-credit")
async def admin_add_credit(request: Request, data: AddCreditInput):
    session = request.cookies.get("admin_session")
    if session not in ACTIVE_ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        db_client = firestore.client()
        child_ref = db_client.collection("children").document(data.child_id)
        doc = child_ref.get()
        if not doc.exists:
            # Create a new child document with the given credits if it doesn't exist
            child_ref.set({
                "childId": data.child_id,
                "childName": "등록 대기 자녀",
                "credits": data.amount,
                "totalCreditsGranted": data.amount,
                "pairedReviewers": []
            })
            return {"status": "success", "new_credits": data.amount, "new_total": data.amount}
        
        doc_data = doc.to_dict()
        current_credits = doc_data.get("credits") or 0
        current_total = doc_data.get("totalCreditsGranted") or 0
        
        new_credits = current_credits + data.amount
        new_total = current_total + data.amount
        
        child_ref.update({
            "credits": new_credits,
            "totalCreditsGranted": new_total
        })
        return {"status": "success", "new_credits": new_credits, "new_total": new_total}
    except Exception as e:
        print(f"Error adding credit: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/link-child")
async def admin_link_child(request: Request, data: LinkChildInput):
    session = request.cookies.get("admin_session")
    if session not in ACTIVE_ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        db_client = firestore.client()
        reviewer_ref = db_client.collection("reviewers").document(data.reviewer_uid)
        doc = reviewer_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Reviewer not found")
        
        reviewer_data = doc.to_dict()
        paired = reviewer_data.get("pairedChildren") or []
        if data.child_id not in paired:
            paired.append(data.child_id)
            reviewer_ref.update({"pairedChildren": paired})
            
        # Also update child's pairedReviewers
        child_ref = db_client.collection("children").document(data.child_id)
        child_doc = child_ref.get()
        if child_doc.exists:
            child_data = child_doc.to_dict()
            reviewers = child_data.get("pairedReviewers") or []
            if data.reviewer_uid not in reviewers:
                reviewers.append(data.reviewer_uid)
                child_ref.update({"pairedReviewers": reviewers})
        else:
            # Pre-create child doc if it doesn't exist
            child_ref.set({
                "childId": data.child_id,
                "childName": "등록 대기 자녀",
                "credits": 0,
                "totalCreditsGranted": 0,
                "pairedReviewers": [data.reviewer_uid]
            })
            
        return {"status": "success"}
    except Exception as e:
        print(f"Error linking child: {e}")
        raise HTTPException(status_code=500, detail=str(e))

class ParentPurchaseInput(BaseModel):
    parent_uid: str
    amount: int
    is_subscription: bool = False

class CreditTransferInput(BaseModel):
    parent_uid: str
    child_id: str
    amount: int

class AddParentCreditInput(BaseModel):
    parent_uid: str
    amount: int

class PaymentVerifyInput(BaseModel):
    imp_uid: str
    merchant_uid: str
    package_type: str  # "single", "bottle", "pot", "box"

def get_portone_token():
    api_key = os.getenv("PORTONE_API_KEY")
    api_secret = os.getenv("PORTONE_API_SECRET")
    if not api_key or not api_secret:
        print("Warning: PORTONE_API_KEY or PORTONE_API_SECRET not set. PortOne API calls will fail.")
        return None
        
    url = "https://api.iamport.kr/users/getToken"
    headers = {"Content-Type": "application/json"}
    payload = {
        "imp_key": api_key,
        "imp_secret": api_secret
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("code") == 0:
                return res_data.get("response", {}).get("access_token")
            else:
                print(f"PortOne token error: {res_data.get('message')}")
    except Exception as e:
        print(f"Failed to fetch PortOne token: {e}")
    return None

def get_portone_payment(imp_uid: str, token: str):
    url = f"https://api.iamport.kr/payments/{imp_uid}"
    headers = {"Authorization": token}
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=5) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            if res_data.get("code") == 0:
                return res_data.get("response")
            else:
                print(f"PortOne payment fetch error: {res_data.get('message')}")
    except Exception as e:
        print(f"Failed to fetch PortOne payment details: {e}")
    return None

@app.post("/api/payment/verify")
async def verify_payment(request: Request, data: PaymentVerifyInput):
    session = request.cookies.get("parent_session")
    if not session:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
        
    credits_map = {
        "single": 1,
        "bottle": 10,
        "pot": 30,
        "box": 100
    }
    
    if data.package_type not in credits_map:
        raise HTTPException(status_code=400, detail="유효하지 않은 패키지 타입입니다.")
        
    db_client = firestore.client()
    prices_ref = db_client.collection("config").document("prices").get()
    prices = {}
    if prices_ref.exists:
        prices = prices_ref.to_dict()
        
    price_key_map = {
        "single": "single_price",
        "bottle": "bottle_price",
        "pot": "pot_price",
        "box": "box_price"
    }
    default_prices = {
        "single": 1000,
        "bottle": 9900,
        "pot": 27000,
        "box": 39000
    }
    
    expected_price = int(prices.get(price_key_map[data.package_type], default_prices[data.package_type]))
    added_credits = credits_map[data.package_type]
    is_subscription = (data.package_type == "box")
    
    api_key = os.getenv("PORTONE_API_KEY")
    api_secret = os.getenv("PORTONE_API_SECRET")
    
    if not api_key or not api_secret:
        print(f"[SIMULATION] PortOne API credentials not configured. Simulating success for imp_uid: {data.imp_uid}")
        receipt_ref = db_client.collection("receipts").document(data.imp_uid)
        if receipt_ref.get().exists:
            raise HTTPException(status_code=400, detail="이미 처리된 결제 영수증입니다.")
            
        receipt_ref.set({
            "impUid": data.imp_uid,
            "merchantUid": data.merchant_uid,
            "amount": expected_price,
            "packageType": data.package_type,
            "parentUid": session,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "simulated": True
        })
        
        parent_ref = db_client.collection("reviewers").document(session)
        parent_doc = parent_ref.get()
        if not parent_doc.exists:
            raise HTTPException(status_code=404, detail="보호자 프로필을 찾을 수 없습니다.")
            
        p_data = parent_doc.to_dict()
        current_credits = p_data.get("credits") or 0
        new_credits = current_credits + added_credits
        
        update_data = {"credits": new_credits}
        if is_subscription:
            update_data["subscriptionActive"] = True
            
        parent_ref.update(update_data)
        return {"status": "success", "new_credits": new_credits}

    token = get_portone_token()
    if not token:
        raise HTTPException(status_code=500, detail="포트원 토큰 발급에 실패했습니다.")
        
    payment = get_portone_payment(data.imp_uid, token)
    if not payment:
        raise HTTPException(status_code=500, detail="포트원 결제 정보를 조회할 수 없습니다.")
        
    if payment.get("status") != "paid":
        raise HTTPException(status_code=400, detail=f"결제 상태가 완료(paid)가 아닙니다. 현재 상태: {payment.get('status')}")
        
    paid_amount = payment.get("amount")
    if paid_amount != expected_price:
        raise HTTPException(status_code=400, detail=f"위변조 감지: 결제 요청 금액({expected_price}원)과 실제 결제 금액({paid_amount}원)이 일치하지 않습니다.")
        
    receipt_ref = db_client.collection("receipts").document(data.imp_uid)
    if receipt_ref.get().exists:
        raise HTTPException(status_code=400, detail="이미 처리된 결제 영수증입니다.")
        
    receipt_ref.set({
        "impUid": data.imp_uid,
        "merchantUid": data.merchant_uid,
        "amount": paid_amount,
        "packageType": data.package_type,
        "parentUid": session,
        "timestamp": firestore.SERVER_TIMESTAMP
    })
    
    parent_ref = db_client.collection("reviewers").document(session)
    parent_doc = parent_ref.get()
    if not parent_doc.exists:
        raise HTTPException(status_code=404, detail="보호자 프로필을 찾을 수 없습니다.")
        
    p_data = parent_doc.to_dict()
    current_credits = p_data.get("credits") or 0
    new_credits = current_credits + added_credits
    
    update_data = {"credits": new_credits}
    if is_subscription:
        update_data["subscriptionActive"] = True
        
    parent_ref.update(update_data)
    return {"status": "success", "new_credits": new_credits}

@app.post("/credits/purchase-mock")
async def credits_purchase_mock(data: ParentPurchaseInput):
    if not data.parent_uid or data.amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid data")
    
    try:
        db_client = firestore.client()
        parent_ref = db_client.collection("reviewers").document(data.parent_uid)
        doc = parent_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Parent not found")
        
        doc_data = doc.to_dict()
        current_credits = doc_data.get("credits") or 0
        new_credits = current_credits + data.amount
        
        update_data = {
            "credits": new_credits
        }
        if data.is_subscription:
            update_data["subscriptionActive"] = True
            
        parent_ref.update(update_data)
        return {"status": "success", "new_credits": new_credits}
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error mock purchasing credits: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/credits/transfer")
async def credits_transfer(data: CreditTransferInput):
    if not data.parent_uid or not data.child_id or data.amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid data")
        
    try:
        db_client = firestore.client()
        parent_ref = db_client.collection("reviewers").document(data.parent_uid)
        child_ref = db_client.collection("children").document(data.child_id)
        
        @firestore.transactional
        def transfer_transaction(transaction, p_ref, c_ref, transfer_amount):
            p_snapshot = p_ref.get(transaction=transaction)
            if not p_snapshot.exists:
                raise HTTPException(status_code=404, detail="보호자를 찾을 수 없습니다.")
                
            p_data = p_snapshot.to_dict()
            
            paired = p_data.get("pairedChildren") or []
            if data.child_id not in paired:
                raise HTTPException(status_code=403, detail="연결되지 않은 자녀입니다.")
                
            p_credits = p_data.get("credits") or 0
            if (p_credits < transfer_amount):
                raise HTTPException(status_code=400, detail="보유하신 마법이슬 잔액이 부족합니다.")
                
            c_snapshot = c_ref.get(transaction=transaction)
            c_credits = 0
            c_total = 0
            if c_snapshot.exists:
                c_data = c_snapshot.to_dict()
                c_credits = c_data.get("credits") or 0
                c_total = c_data.get("totalCreditsGranted") or 0
                
            new_p_credits = p_credits - transfer_amount
            transaction.update(p_ref, {"credits": new_p_credits})
            
            new_c_credits = c_credits + transfer_amount
            new_c_total = c_total + transfer_amount
            if c_snapshot.exists:
                transaction.update(c_ref, {
                    "credits": new_c_credits,
                    "totalCreditsGranted": new_c_total
                })
            else:
                transaction.set(c_ref, {
                    "childId": data.child_id,
                    "childName": "등록 대기 자녀",
                    "credits": new_c_credits,
                    "totalCreditsGranted": new_c_total,
                    "pairedReviewers": [data.parent_uid]
                })
                
            return new_p_credits, new_c_credits
            
        transaction = db_client.transaction()
        new_parent_credits, new_child_credits = transfer_transaction(
            transaction, parent_ref, child_ref, data.amount
        )
        return {
            "status": "success",
            "parent_credits": new_parent_credits,
            "child_credits": new_child_credits
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error transferring credits: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/add-parent-credit")
async def admin_add_parent_credit(request: Request, data: AddParentCreditInput):
    session = request.cookies.get("admin_session")
    if session not in ACTIVE_ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    try:
        db_client = firestore.client()
        parent_ref = db_client.collection("reviewers").document(data.parent_uid)
        doc = parent_ref.get()
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Parent not found")
            
        doc_data = doc.to_dict()
        current_credits = doc_data.get("credits") or 0
        new_credits = current_credits + data.amount
        
        parent_ref.update({
            "credits": new_credits
        })
        return {"status": "success", "new_credits": new_credits}
    except HTTPException as he:
        raise he
    except Exception as e:
        print(f"Error adding parent credit: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/login", response_class=HTMLResponse)
async def get_admin_login(request: Request):
    session = request.cookies.get("admin_session")
    if session in ACTIVE_ADMIN_SESSIONS:
        return RedirectResponse(url="/admin", status_code=303)
    return ADMIN_LOGIN_HTML.replace("{error_placeholder}", "")

@app.post("/admin/login")
async def post_admin_login(
    response: Response,
    username: str = Form(...),
    password: str = Form(...)
):
    if username == "acoustjk" and password == "dkdlfjs18!*":
        import secrets
        session_id = secrets.token_hex(16)
        ACTIVE_ADMIN_SESSIONS.add(session_id)
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie(key="admin_session", value=session_id, httponly=True)
        return resp
    
    err_html = '<div class="error-msg">⚠️ 아이디 또는 비밀번호가 올바르지 않습니다.</div>'
    return HTMLResponse(content=ADMIN_LOGIN_HTML.replace("{error_placeholder}", err_html), status_code=401)

@app.post("/admin/logout")
async def post_admin_logout(request: Request):
    session = request.cookies.get("admin_session")
    if session in ACTIVE_ADMIN_SESSIONS:
        ACTIVE_ADMIN_SESSIONS.remove(session)
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(key="admin_session")
    return resp

class SavePricesInput(BaseModel):
    single_price: int
    bottle_price: int
    pot_price: int
    box_price: int

@app.post("/admin/save-prices")
async def save_prices(data: SavePricesInput, request: Request):
    session = request.cookies.get("admin_session")
    if session not in ACTIVE_ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    try:
        db_client = firestore.client()
        db_client.collection("config").document("prices").set({
            "single_price": data.single_price,
            "bottle_price": data.bottle_price,
            "pot_price": data.pot_price,
            "box_price": data.box_price
        })
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_dashboard(request: Request):
    session = request.cookies.get("admin_session")
    if session not in ACTIVE_ADMIN_SESSIONS:
        return RedirectResponse(url="/admin/login", status_code=303)
    
    try:
        db_client = firestore.client()
        
        # 1. Fetch children map
        children_docs = db_client.collection("children").get()
        children_map = {}
        for doc in children_docs:
            data = doc.to_dict()
            child_id = data.get("childId") or doc.id
            credits = data.get("credits") or 0
            children_map[child_id] = {
                "childId": child_id,
                "childName": data.get("childName") or "무명 어린이",
                "credits": credits,
                "totalCredits": data.get("totalCreditsGranted") or 0
            }
            
        # 2. Fetch reviewers (parents)
        reviewers_docs = db_client.collection("reviewers").get()
        
        # Calculate active child IDs and total credits from active reviewers (whose UID starts with "kakao")
        active_child_ids = set()
        for doc in reviewers_docs:
            data = doc.to_dict()
            uid = data.get("reviewerUid") or doc.id
            if uid.startswith("kakao"):
                paired_children_ids = data.get("pairedChildren") or []
                for cid in paired_children_ids:
                    if cid in children_map:
                        active_child_ids.add(cid)
                        
        total_children = len(active_child_ids)
        total_credits = sum(children_map[cid]["credits"] for cid in active_child_ids)
        
        table_rows = ""
        total_users = 0
        
        for doc in reviewers_docs:
            data = doc.to_dict()
            uid = data.get("reviewerUid") or doc.id
            if not uid.startswith("kakao"):
                continue
            name = data.get("name") or "보호자"
            paired_children_ids = data.get("pairedChildren") or []
            parent_credits = data.get("credits") or 0
            
            # Fetch email, nickname and last login from Firebase Auth
            email = "이메일 정보 없음"
            last_login = "기록 없음"
            display_name = ""
            if firebase_admin._apps:
                try:
                    user_record = auth.get_user(uid)
                    email = user_record.email or "이메일 정보 없음"
                    display_name = user_record.display_name or ""
                    metadata = user_record.user_metadata
                    if metadata and metadata.last_sign_in_timestamp:
                        from datetime import datetime, timezone, timedelta
                        # Convert UTC to KST (UTC+9)
                        kst = timezone(timedelta(hours=9))
                        dt = datetime.fromtimestamp(metadata.last_sign_in_timestamp / 1000, tz=timezone.utc).astimezone(kst)
                        last_login = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception as auth_err:
                    print(f"Auth fetch failed for {uid}: {auth_err}")
            
            total_users += 1
            
            display_name_badge = ""
            if display_name and display_name != name:
                display_name_badge = f'<span style="font-size: 11px; background: rgba(139, 92, 246, 0.2); padding: 2px 6px; border-radius: 4px; color: #a78bfa; margin-left: 6px;">카카오: {display_name}</span>'
            
            # Build children badges html
            children_html = ""
            if paired_children_ids:
                for cid in paired_children_ids:
                    c_info = children_map.get(cid)
                    if c_info:
                        children_html += f'''
                        <span class="child-badge">
                            👦 {c_info['childName']} 
                            <span class="credit-badge">🪙 <span id="credit-count-{c_info['childId']}">{c_info['credits']}</span>/<span id="total-count-{c_info['childId']}">{c_info['totalCredits']}</span></span>
                            <button class="btn-charge" onclick="chargeCredit('{c_info['childId']}', '{c_info['childName']}')" title="마법이슬 지급">+</button>
                        </span>
                        '''
                    else:
                        children_html += f'''
                        <span class="child-badge" style="background: rgba(156, 163, 175, 0.12); border-color: rgba(156, 163, 175, 0.25); color: #9ca3af;">
                            🔗 연결 대기 (ID: <span id="credit-count-{cid}" style="display:none;">0</span><span id="total-count-{cid}" style="display:none;">0</span>{cid[:6]}...)
                            <button class="btn-charge" onclick="chargeCredit('{cid}', '연결 대기 자녀')" title="마법이슬 지급">+</button>
                        </span>
                        '''
            else:
                children_html = '<span style="color: #9ca3af; font-size: 13px;">연결된 자녀 없음</span>'
                
            table_rows += f"""
            <tr>
                <td>
                    <div class="user-name">{name} {display_name_badge}</div>
                    <div class="user-email">{email}</div>
                    <div style="font-size: 13px; color: #fb7185; margin-top: 5px; font-weight: bold; display: flex; align-items: center; gap: 4px;">
                        <span>보유 마법이슬: <span id="parent-credit-{uid}">{parent_credits}</span>🪙</span>
                        <button class="btn-charge" style="background: linear-gradient(135deg, #ec4899, #db2777);" onclick="chargeParentCredit('{uid}', '{name}')" title="보호자 마법이슬 지급">+</button>
                    </div>
                    <div style="margin-top: 8px;">
                        <button class="btn-link-child" onclick="linkChild('{uid}')">🔗 자녀 연결</button>
                    </div>
                </td>
                <td>
                    <div class="last-login-time">{last_login}</div>
                </td>
                <td>
                    <span class="uid-badge">{uid}</span>
                </td>
                <td>
                    {children_html}
                </td>
            </tr>
            """
            
        if not table_rows:
            table_rows = '<tr class="no-data-row"><td colspan="4" class="no-data">가입된 회원이 없습니다.</td></tr>'
            
        # Fetch prices from Firestore
        prices_ref = db_client.collection("config").document("prices").get()
        prices = {}
        if prices_ref.exists:
            prices = prices_ref.to_dict()
            
        single_price = prices.get("single_price", 1000)
        bottle_price = prices.get("bottle_price", 9900)
        pot_price = prices.get("pot_price", 27000)
        box_price = prices.get("box_price", 39000)
            
        html_content = ADMIN_DASHBOARD_HTML.replace("{total_users}", str(total_users)) \
                                           .replace("{total_children}", str(total_children)) \
                                           .replace("{total_credits}", str(total_credits)) \
                                           .replace("{table_rows}", table_rows) \
                                           .replace("{single_price}", str(single_price)) \
                                           .replace("{bottle_price}", str(bottle_price)) \
                                           .replace("{pot_price}", str(pot_price)) \
                                           .replace("{box_price}", str(box_price))
        return html_content
        
    except Exception as e:
        print(f"Admin dashboard error: {e}")
        err_html = f"""
        <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); padding: 20px; border-radius: 12px; margin-top: 20px;">
            <h2 style="color: #ef4444; margin-top: 0;">데이터 로딩 오류</h2>
            <p style="color: #fca5a5; margin-bottom: 0;">데이터베이스 연동 중 오류가 발생했습니다. 로그를화면을 통해 확인해 주세요. ({str(e)})</p>
        </div>
        """
        return ADMIN_DASHBOARD_HTML.replace("{total_users}", "0").replace("{total_children}", "0").replace("{total_credits}", "0").replace("{table_rows}", f'<tr><td colspan="4">{err_html}</td></tr>').replace("{single_price}", "1000").replace("{bottle_price}", "9900").replace("{pot_price}", "27000").replace("{box_price}", "39000")

# ==========================================
# PG Compliance Pages & Footer constants
# ==========================================

TERMS_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>이용약관 - AI고치</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Nanum+Gothic:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --text-color: #f3f4f6;
            --card-bg: rgba(17, 24, 39, 0.65);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary: #f59e0b;
            --primary-light: #fbbf24;
        }
        body {
            font-family: 'Outfit', 'Nanum Gothic', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(245, 158, 11, 0.1) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.1) 0px, transparent 50%);
            color: var(--text-color);
            margin: 0;
            padding: 40px 20px;
            min-height: 100vh;
            box-sizing: border-box;
            line-height: 1.6;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            padding: 40px;
            border-radius: 24px;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.2);
        }
        .btn-back {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 10px 18px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            color: #d1d5db;
            border-radius: 12px;
            font-size: 14px;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s ease;
            margin-bottom: 30px;
        }
        .btn-back:hover {
            background: rgba(255, 255, 255, 0.1);
            color: white;
            border-color: var(--primary);
        }
        h1 {
            font-size: 28px;
            font-weight: 700;
            margin-top: 0;
            margin-bottom: 20px;
            color: white;
            border-bottom: 1.5px solid var(--primary);
            padding-bottom: 15px;
        }
        h2 {
            font-size: 18px;
            font-weight: 700;
            margin-top: 30px;
            margin-bottom: 12px;
            color: var(--primary-light);
        }
        p, li {
            font-size: 14px;
            color: #d1d5db;
            margin-bottom: 10px;
        }
        ul {
            padding-left: 20px;
            margin-bottom: 20px;
        }
        .footer-info {
            margin-top: 40px;
            border-top: 1px solid var(--border-color);
            padding-top: 20px;
            font-size: 12px;
            color: #9ca3af;
        }
    </style>
</head>
<body>
    <div class="container">
        <a href="#" onclick="if(window.opener || window.history.length > 1) { window.close(); history.back(); } else { location.href='/purchase'; } return false;" class="btn-back">
            ← 돌아가기
        </a>
        <h1>AI고치 이용약관</h1>
        
        <h2>제 1 조 (목적)</h2>
        <p>본 약관은 (주)제이케이 (이하 "회사")가 제공하는 AI고치 및 관련 서비스(이하 "서비스")를 이용자가 이용함에 있어 "회사"와 "이용자" 간의 권리, 의무, 책임사항 및 기타 필요한 사항을 규정함을 목적으로 합니다.</p>

        <h2>제 2 조 (용어의 정의)</h2>
        <ul>
            <li><strong>서비스:</strong> 회사가 제공하는 AI 기반 일기 첨삭, 자녀 교육 관리, 알림 및 보호자 리포트 등 일체의 웹/앱 서비스를 의미합니다.</li>
            <li><strong>이용자:</strong> 본 약관에 동의하고 회사가 제공하는 서비스를 이용하는 보호자 및 자녀를 의미합니다.</li>
            <li><strong>마법이슬:</strong> 서비스 내에서 AI 첨삭, 리포트 확인 등 유료 기능을 이용하기 위해 사용하는 디지털 재화를 의미합니다.</li>
        </ul>

        <h2>제 3 조 (약관의 명시와 개정)</h2>
        <p>회사는 본 약관의 내용을 이용자가 쉽게 알 수 있도록 서비스 화면에 게시합니다. 회사는 필요한 경우 관련 법령을 위배하지 않는 범위 내에서 본 약관을 개정할 수 있습니다.</p>

        <h2>제 4 조 (서비스 이용 및 제한)</h2>
        <p>이용자는 회사가 제공하는 결제 수단을 통해 마법이슬을 충전하여 서비스를 이용할 수 있습니다. 타인의 명의나 부정한 방법을 사용한 이용자는 서비스 이용이 영구 제한될 수 있습니다.</p>

        <h2>제 5 조 (청약철회 및 환불)</h2>
        <p>이용자는 유료 결제로 구매한 마법이슬에 대해 구매일로부터 7일 이내에 청약철회(환불)를 요청할 수 있습니다. 단, 구매 후 이미 사용한 마법이슬 또는 자녀 계정으로 전송이 완료된 마법이슬에 대해서는 청약철회가 제한됩니다. 정기 구독 취소 시 해당 결제 주기가 끝난 후 다음 결제부터 청구되지 않습니다.</p>

        <div class="footer-info">
            <strong>(주)제이케이 (JK Inc.)</strong><br>
            대표자: 홍길동 | 사업자등록번호: 000-00-00000 | 통신판매업신고번호: 제 2026-서울강남-0000호<br>
            고객센터: 02-1234-5678 | 이메일: support@ai-gochi.com | 주소: 서울특별시 강남구 테헤란로 123, 4층
        </div>
    </div>
</body>
</html>"""

PRIVACY_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>개인정보처리방침 - AI고치</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Nanum+Gothic:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --text-color: #f3f4f6;
            --card-bg: rgba(17, 24, 39, 0.65);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary: #f59e0b;
            --primary-light: #fbbf24;
        }
        body {
            font-family: 'Outfit', 'Nanum Gothic', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(245, 158, 11, 0.1) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.1) 0px, transparent 50%);
            color: var(--text-color);
            margin: 0;
            padding: 40px 20px;
            min-height: 100vh;
            box-sizing: border-box;
            line-height: 1.6;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            padding: 40px;
            border-radius: 24px;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.2);
        }
        .btn-back {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 10px 18px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            color: #d1d5db;
            border-radius: 12px;
            font-size: 14px;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s ease;
            margin-bottom: 30px;
        }
        .btn-back:hover {
            background: rgba(255, 255, 255, 0.1);
            color: white;
            border-color: var(--primary);
        }
        h1 {
            font-size: 28px;
            font-weight: 700;
            margin-top: 0;
            margin-bottom: 20px;
            color: white;
            border-bottom: 1.5px solid var(--primary);
            padding-bottom: 15px;
        }
        h2 {
            font-size: 18px;
            font-weight: 700;
            margin-top: 30px;
            margin-bottom: 12px;
            color: var(--primary-light);
        }
        p, li {
            font-size: 14px;
            color: #d1d5db;
            margin-bottom: 10px;
        }
        ul {
            padding-left: 20px;
            margin-bottom: 20px;
        }
        .footer-info {
            margin-top: 40px;
            border-top: 1px solid var(--border-color);
            padding-top: 20px;
            font-size: 12px;
            color: #9ca3af;
        }
    </style>
</head>
<body>
    <div class="container">
        <a href="#" onclick="if(window.opener || window.history.length > 1) { window.close(); history.back(); } else { location.href='/purchase'; } return false;" class="btn-back">
            ← 돌아가기
        </a>
        <h1>AI고치 개인정보처리방침</h1>
        
        <p>(주)제이케이(이하 "회사")는 이용자의 개인정보를 매우 중요시하며, '개인정보 보호법' 등 관련 법령을 준수하고 있습니다. 회사는 본 개인정보처리방침을 통해 이용자가 제공하는 개인정보가 어떠한 용도와 방식으로 이용되고 있으며, 개인정보보호를 위해 어떠한 조치가 취해지고 있는지 알려드립니다.</p>

        <h2>1. 수집하는 개인정보 항목 및 수집방법</h2>
        <ul>
            <li><strong>보호자 수집항목:</strong> 카카오 소셜 로그인 식별 정보 (이메일, 닉네임, 프로필 이미지, 고유식별ID), 대금 결제 시 결제 승인 결과 정보(상태, 금액, 주문ID, 포트원 거래식별자).</li>
            <li><strong>자녀 수집항목:</strong> 닉네임, 나이, 성별, 자녀가 앱에 직접 작성한 일기 텍스트 및 음성 녹음 데이터.</li>
            <li><strong>수집방법:</strong> 앱 가입 및 로그인, 유료 서비스 이용 결제 창, 자녀의 직접 입력.</li>
        </ul>

        <h2>2. 개인정보의 수집 및 이용 목적</h2>
        <p>회사는 다음의 목적을 위해 개인정보를 처리합니다. 처리하고 있는 개인정보는 목적 이외의 용도로는 이용되지 않으며, 이용 목적이 변경되는 경우에는 관련 법령에 따라 별도의 동의를 받는 등 필요한 조치를 이행할 예정입니다.</p>
        <ul>
            <li><strong>회원 관리:</strong> 본인 식별, 연동 회원 간 마법이슬 전송 및 확인, 불량 이용 제한.</li>
            <li><strong>서비스 제공:</strong> AI 기반 자녀 일기 분석 피드백, 자녀 성장 상태 보고서(보호자 리포트) 발급.</li>
            <li><strong>결제 및 정산:</strong> 유료 패키지 결제, 결제 승인 및 취소(환불) 처리.</li>
        </ul>

        <h2>3. 개인정보의 보유 및 이용 기간</h2>
        <p>회사는 법령에 따른 개인정보 보유·이용기간 또는 이용자로부터 개인정보 수집 시에 동의받은 보유·이용기간 내에서 개인정보를 처리·보유합니다.</p>
        <ul>
            <li>서비스 가입 기간 동안 보유 및 이용하며, 회원 탈퇴 시 파기합니다.</li>
            <li>전자상거래법 등 관계법령에 따른 보존:
                <ul>
                    <li>계약 또는 청약철회 등에 관한 기록: 5년</li>
                    <li>대금결제 및 재화 등의 공급에 관한 기록: 5년</li>
                    <li>소비자의 불만 또는 분쟁처리에 관한 기록: 3년</li>
                </ul>
            </li>
        </ul>

        <div class="footer-info">
            <strong>(주)제이케이 (JK Inc.)</strong><br>
            대표자: 홍길동 | 사업자등록번호: 000-00-00000 | 통신판매업신고번호: 제 2026-서울강남-0000호<br>
            고객센터: 02-1234-5678 | 이메일: support@ai-gochi.com | 주소: 서울특별시 강남구 테헤란로 123, 4층
        </div>
    </div>
</body>
</html>"""

REFUND_HTML = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>환불정책 - AI고치</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Nanum+Gothic:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --text-color: #f3f4f6;
            --card-bg: rgba(17, 24, 39, 0.65);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary: #f59e0b;
            --primary-light: #fbbf24;
        }
        body {
            font-family: 'Outfit', 'Nanum Gothic', sans-serif;
            background: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(245, 158, 11, 0.1) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.1) 0px, transparent 50%);
            color: var(--text-color);
            margin: 0;
            padding: 40px 20px;
            min-height: 100vh;
            box-sizing: border-box;
            line-height: 1.6;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            padding: 40px;
            border-radius: 24px;
            box-shadow: 0 15px 35px rgba(0, 0, 0, 0.2);
        }
        .btn-back {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 10px 18px;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            color: #d1d5db;
            border-radius: 12px;
            font-size: 14px;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.2s ease;
            margin-bottom: 30px;
        }
        .btn-back:hover {
            background: rgba(255, 255, 255, 0.1);
            color: white;
            border-color: var(--primary);
        }
        h1 {
            font-size: 28px;
            font-weight: 700;
            margin-top: 0;
            margin-bottom: 20px;
            color: white;
            border-bottom: 1.5px solid var(--primary);
            padding-bottom: 15px;
        }
        h2 {
            font-size: 18px;
            font-weight: 700;
            margin-top: 30px;
            margin-bottom: 12px;
            color: var(--primary-light);
        }
        p, li {
            font-size: 14px;
            color: #d1d5db;
            margin-bottom: 10px;
        }
        ul {
            padding-left: 20px;
            margin-bottom: 20px;
        }
        .footer-info {
            margin-top: 40px;
            border-top: 1px solid var(--border-color);
            padding-top: 20px;
            font-size: 12px;
            color: #9ca3af;
        }
    </style>
</head>
<body>
    <div class="container">
        <a href="#" onclick="if(window.opener || window.history.length > 1) { window.close(); history.back(); } else { location.href='/purchase'; } return false;" class="btn-back">
            ← 돌아가기
        </a>
        <h1>AI고치 환불 및 취소 정책</h1>
        
        <h2>1. 청약철회 (환불) 조건</h2>
        <ul>
            <li>이용자가 유료 결제한 마법이슬 상품은 구매일로부터 7일 이내에 고객센터를 통해 청약철회(환불)를 요청할 수 있습니다.</li>
            <li>단, 다음의 경우에는 청약철회가 불가합니다:
                <ul>
                    <li>구매한 마법이슬 중 일부라도 이미 사용한 경우.</li>
                    <li>보호자 계정에서 자녀 계정으로 이미 전송이 완료된 경우.</li>
                </ul>
            </li>
        </ul>

        <h2>2. 정기 구독의 해지 및 취소</h2>
        <ul>
            <li>'마르지 않는 샘물 상자' 정기구독 상품은 매월 지정된 날짜에 정기적으로 결제가 발생합니다.</li>
            <li>구독 해지(자동 결제 중단)는 다음 결제 예정일 최소 24시간 전에 신청해야 당월 이후 추가 결제가 차단됩니다.</li>
            <li>해지 신청 후에도, 이미 결제된 당월 기간 동안은 마법이슬 정기 혜택이 정상 제공되며, 차기 주기부터 자동결제가 실행되지 않습니다.</li>
        </ul>

        <h2>3. 환불 신청 및 처리 절차</h2>
        <ul>
            <li>환불 신청은 고객센터 이메일(support@ai-gochi.com) 또는 유선전화(02-1234-5678)를 통해 접수합니다.</li>
            <li>회사는 영업일 기준 3일 이내에 결제대행사(PG)에 대금 지급 정지 또는 취소를 요청합니다.</li>
            <li>신용카드 결제 취소의 경우, 카드사 사정에 따라 한도 복구 및 실제 취소 반영까지 평일 기준 3~5일이 더 소요될 수 있습니다.</li>
        </ul>

        <div class="footer-info">
            <strong>(주)제이케이 (JK Inc.)</strong><br>
            대표자: 홍길동 | 사업자등록번호: 000-00-00000 | 통신판매업신고번호: 제 2026-서울강남-0000호<br>
            고객센터: 02-1234-5678 | 이메일: support@ai-gochi.com | 주소: 서울특별시 강남구 테헤란로 123, 4층
        </div>
    </div>
</body>
</html>"""

@app.get("/terms", response_class=HTMLResponse)
async def get_terms():
    return HTMLResponse(content=TERMS_HTML)

@app.get("/privacy", response_class=HTMLResponse)
async def get_privacy():
    return HTMLResponse(content=PRIVACY_HTML)

@app.get("/refund", response_class=HTMLResponse)
async def get_refund():
    return HTMLResponse(content=REFUND_HTML)

@app.get("/")
async def read_index():
    return FileResponse("index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
