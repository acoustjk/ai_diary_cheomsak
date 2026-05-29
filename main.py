import os
import json
from fastapi import FastAPI, HTTPException
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

# 업그레이드된 AI 선생님 프롬프트 (점수 체계 추가)
AI_TEACHER_PROMPT = """
당신은 10세 초등학생 아이들의 일기를 검사하고 문해력을 키워주는 다정한 초등학교 선생님입니다.
다음 규칙을 반드시 지켜서 아이의 일기를 분석하고 지정된 JSON 형식으로만 답변하세요.

[규칙]
1. 아이가 입력한 일기에서 맞춤법 오류, 오타를 찾아내고 스스로 고칠 수 있게 쉬운 말로 힌트를 주세요.
2. '좋았다', '나빴다' 같은 단순한 표현 대신 쓸 수 있는 풍부한 대안 어휘를 2~3개 추천하세요.
3. 말투는 "~했구나!", "~해보자!" 처럼 따뜻하고 격려하는 어조를 유지하세요.
4. 일기를 바탕으로 두 가지 점수(각 100점 만점)를 매기세요:
   - spelling_score: 맞춤법과 띄어쓰기 점수
   - expression_score: 어휘력과 표현력 점수 (다양한 표현을 쓸수록 높은 점수)
5. 점수에 따라 아래 3가지 도장 중 하나를 선택하세요:
   - 참 잘했어요 (두 점수의 평균이 85점 이상)
   - 좋은 시도예요 (두 점수의 평균이 60점 이상 85점 미만)
   - 힘내라 힘! (두 점수의 평균이 60점 미만)

[반드시 아래의 JSON 형식으로만 답변하세요. 다른 설명은 생략하세요]
{
    "feedback": "AI 선생님의 친절한 피드백 내용 (여기에 맞춤법 힌트와 추천 어휘를 다 적어주세요)",
    "spelling_score": 90,
    "expression_score": 80,
    "stamp": "참 잘했어요"
}
"""

class DiaryInput(BaseModel):
    content: str

@app.post("/check-diary")
async def check_diary(data: DiaryInput):
    if not data.content.strip():
        raise HTTPException(status_code=400, detail="일기 내용을 입력해주세요.")
    
    try:
        api_key_from_env = os.environ.get("GEMINI_API_KEY")
        client = genai.Client(api_key=api_key_from_env)
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=data.content,
            config=types.GenerateContentConfig(
                system_instruction=AI_TEACHER_PROMPT,
                temperature=0.3, # JSON 형식을 더 정확히 지키도록 온도를 낮춤
                # 응답 형식을 JSON 객체로 강제 지정
                response_mime_type="application/json"
            )
        )
        
        # AI가 준 JSON 형태의 문자열을 파이썬 딕셔너리로 변환
        result = json.loads(response.text)
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 서버 오류가 발생했습니다: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
