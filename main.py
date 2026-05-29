from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
import os

app = FastAPI()

# 프론트엔드 웹페이지와 원활하게 통신하기 위한 설정 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 테스트 환경을 위해 모두 허용
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 10세 아이를 위한 AI 선생님 프롬프트 설정
AI_TEACHER_PROMPT = """
당신은 10세 초등학생 아이들의 일기를 검사하고 문해력을 키워주는 다정한 초등학교 선생님입니다.
다음 규칙을 반드시 지켜서 아이에게 피드백을 주세요.

1. 아이가 입력한 일기에서 맞춤법 오류, 오타, 소리 나는 대로 쓴 표현을 찾아내세요.
2. 절대 정답을 바로 고쳐주지 말고, 아이가 이해할 수 있는 쉬운 말로 힌트를 주며 스스로 키보드를 두드려 고치도록 유도하세요.
3. '좋았다', '나빴다' 같은 단순한 감정 표현이 있다면, 문해력 향상을 위해 더 풍부한 대안 어휘(의성어, 의태어, 관용 표현 등)를 2~3개 친절하게 추천하세요.
4. 말투는 "~했구나!", "~해보자!" 처럼 따뜻하고 격려하는 어조를 유지하세요.
"""

# 구글 AI 스튜디오에서 발급받은 API 키 입력 (무료)
# (실제 배포 시에는 보안을 위해 환경변수로 관리해야 합니다)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 데이터 입력을 받기 위한 틀
class DiaryInput(BaseModel):
    content: str

@app.post("/check-diary")
async def check_diary(data: DiaryInput):
    if not data.content.strip():
        raise HTTPException(status_code=400, detail="일기 내용을 입력해주세요.")
    
    try:
        # 최신 Gemini Client 인스턴스 생성
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # 비용이 들지 않는 가장 빠르고 가벼운 2.5 Flash 모델 사용
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=data.content,
            config=types.GenerateContentConfig(
                system_instruction=AI_TEACHER_PROMPT,
                temperature=0.7,
            )
        )
        
        # AI 선생님의 답변 반환
        return {"feedback": response.text}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 서버 오류가 발생했습니다: {str(e)}")

# 로컬 테스트용 실행 코드
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main.py:app", host="127.0.0.1", port=8000, reload=True)