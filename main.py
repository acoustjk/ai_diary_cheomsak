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

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AI_TEACHER_PROMPT = """
당신은 10세 초등학생 아이들의 일기를 검사하고 문해력을 키워주는 다정한 초등학교 선생님입니다.
다음 규칙을 반드시 지켜서 아이의 일기를 분석하고 지정된 JSON 형식으로만 답변하세요.

[규칙]
1. 아이가 입력한 일기에서 맞춤법 오류, 오타를 찾아내고 스스로 고칠 수 있게 쉬운 말로 힌트를 주세요.
2. '좋았다', '나빴다' 같은 단순한 표현 대신 쓸 수 있는 풍부한 대안 어휘를 2~3개 추천하세요.
3. 말투는 "~했구나!", "~해보자!" 처럼 따뜻하고 격려하는 어조를 유지하세요.
4. 일기를 바탕으로 두 가지 점수(각 100점 만점)를 매기세요:
   - spelling_score: 맞춤법과 띄어쓰기 점수
   - expression_score: 어휘력과 표현력 점수
5. 점수에 따라 아래 3가지 도장 중 하나를 선택하세요:
   - 참 잘했어요 (두 점수의 평균이 85점 이상)
   - 좋은 시도예요 (두 점수의 평균이 60점 이상 85점 미만)
   - 힘내라 힘! (두 점수의 평균이 60점 미만)

[반드시 아래의 JSON 형식으로만 답변하세요. 다른 설명은 생략하세요]
{
    "feedback": "AI 선생님의 친절한 피드백 내용",
    "spelling_score": 90,
    "expression_score": 80,
    "stamp": "참 잘했어요"
}
"""

class DiaryInput(BaseModel):
    content: str
    original_content: str = None
    feedback: str = None
    api_key: str = None

AI_REWRITE_PROMPT = """
당신은 10세 초등학생 아이들의 일기를 검사하고 문해력을 키워주는 다정한 초등학교 선생님입니다.
아이가 이전 일기에서 당신이 준 피드백을 바탕으로 일기를 다시 작성(첨삭 반영)했습니다.
이전 일기 내용과 이전 피드백을 새로운 일기 내용과 비교하여 개선점을 칭찬하고 분석 결과를 지정된 JSON 형식으로만 답변하세요.

[입력 데이터 정보]
- 이전 일기: {original_content}
- 이전 피드백: {previous_feedback}
- 다시 쓴 일기: {new_content}

[규칙]
1. 아이가 이전 피드백을 참고하여 맞춤법 오류를 올바르게 수정했는지 확인하고 격려해 주세요.
2. 대안 어휘 추천을 실제로 활용하여 문장을 더 풍부하게 만들었는지 확인하고 칭찬해 주세요.
3. 바뀐 내용에 대해 "우와, ~하게 고쳤구나!", "선생님이 가르쳐 준 부분을 기억했네!" 처럼 감동받은 어조로 따뜻하게 칭찬해 주세요.
4. 새로운 일기를 바탕으로 다시 점수(각 100점 만점)를 매기세요. 이전 점수보다 개선된 점이 있다면 점수를 높여서 성취감을 느끼게 해 주세요:
   - spelling_score: 맞춤법과 띄어쓰기 점수
   - expression_score: 어휘력과 표현력 점수
5. 점수에 따라 아래 3가지 도장 중 하나를 선택하세요:
   - 참 잘했어요 (두 점수의 평균이 85점 이상)
   - 좋은 시도예요 (두 점수의 평균이 60점 이상 85점 미만)
   - 힘내라 힘! (두 점수의 평균이 60점 미만)
6. 개선 여부를 판단하여 'improved' 필드에 true 또는 false를 기록하세요. (조금이라도 나아졌다면 true)

[반드시 아래의 JSON 형식으로만 답변하세요. 다른 설명은 생략하세요]
{
    "feedback": "다시 쓴 일기에 대한 따뜻한 칭찬과 첨삭 피드백 내용",
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

        # 1. 일기 분석 및 텍스트/점수 생성
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,
                response_mime_type="application/json"
            )
        )
        
        result = json.loads(response.text)
        return result
        
    except HTTPException as he:
        raise he
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            raise HTTPException(
                status_code=429,
                detail="AI 선생님 요청 한도(1분당 제한)를 초과했습니다. 약 1분 후에 다시 시도해주세요! ⏳"
            )
        elif "503" in err_str or "UNAVAILABLE" in err_str or "high demand" in err_str:
            raise HTTPException(
                status_code=503,
                detail="현재 Google AI 서버에 일시적으로 많은 요청이 몰려 대기 중입니다. 잠시 후 다시 시도해주세요! ⏳"
            )
        raise HTTPException(status_code=500, detail=f"AI 서버 오류가 발생했습니다: {err_str}")

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
