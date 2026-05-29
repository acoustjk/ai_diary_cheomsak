import os
import json
import base64
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
6. 이 일기를 바탕으로 그림일기에 들어갈 만한 장면을 설명하는 영어 프롬프트를 작성하세요. 
   - 반드시 'image_prompt' 필드에 넣으세요. 따뜻하고 귀여운 3D 일러스트 스타일(cute 3D illustration style, warm, childhood, for kids)이어야 합니다.

[반드시 아래의 JSON 형식으로만 답변하세요. 다른 설명은 생략하세요]
{
    "feedback": "AI 선생님의 친절한 피드백 내용",
    "spelling_score": 90,
    "expression_score": 80,
    "stamp": "참 잘했어요",
    "image_prompt": "A cute 3D illustration of a child playing with a dog in a sunny park, warm colors, childish"
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
        
        # 1. 일기 분석 및 텍스트/점수 생성
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=data.content,
            config=types.GenerateContentConfig(
                system_instruction=AI_TEACHER_PROMPT,
                temperature=0.3,
                response_mime_type="application/json"
            )
        )
        
        result = json.loads(response.text)
        
        # 2. 분석 결과를 바탕으로 AI 그림 생성 (Imagen 3 모델 사용)
        try:
            image_prompt = result.get("image_prompt", "A cute children's drawing of a happy day")
            image_result = client.models.generate_images(
                model='imagen-3.0-generate-002',
                prompt=image_prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    output_mime_type="image/jpeg",
                    aspect_ratio="1:1"
                )
            )
            
            # 생성된 이미지를 웹화면에 띄울 수 있도록 Base64 데이터로 변환
            generated_image = image_result.generated_images[0]
            image_base64 = base64.b64encode(generated_image.image.image_bytes).decode('utf-8')
            result["image_data"] = f"data:image/jpeg;base64,{image_base64}"
            
        except Exception as img_err:
            # 그림 생성 오류 시 텍스트 피드백만이라도 가도록 예외 처리
            result["image_data"] = ""
            print(f"그림 생성 실패: {str(img_err)}")
            
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 서버 오류가 발생했습니다: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
