import os
import json
import urllib.request
import urllib.parse
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
import firebase_admin
from firebase_admin import credentials, messaging, firestore, auth


# Initialize Firebase Admin SDK
firebase_creds_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
if firebase_creds_json:
    try:
        creds_dict = json.loads(firebase_creds_json)
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)
        print("Firebase Admin initialized successfully with environment variable credentials.")
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
        except Exception as e:
            print(f"Error initializing Firebase Admin with local key: {e}")
    else:
        print("Firebase Admin NOT initialized: No credentials found. Push notifications will be skipped.")

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
        title = "🔋 크레딧 부족 안내"
        body = f"{display_name}이의 남은 크레딧이 {credits_left}개입니다. 끊김 없는 일기 작성을 위해 충전해 주세요! 🔌"
    elif notification_type == "request_credit":
        title = "🪙 크레딧 충전 요청!"
        body = f"{display_name}이가 일기 작성을 위해 크레딧 충전을 요청했어요! 지금 충전해 주세요. ⚡"
    else:
        return

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body
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
    is_rewrite = bool(data.original_content and data.feedback)
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
                    raise HTTPException(status_code=403, detail="크레딧이 부족합니다. 부모님 앱에서 충전해 주세요! 🪙")
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
                raise HTTPException(status_code=403, detail="크레딧이 부족합니다. 부모님 앱에서 충전해 주세요! 🪙")
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
        if data.original_content and data.child_id:
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

@app.get("/")
async def read_index():
    return FileResponse("index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
