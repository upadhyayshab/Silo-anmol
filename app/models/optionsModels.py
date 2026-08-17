from pydantic import BaseModel


class AnswerOption(BaseModel):
    code: str
    label: str


class QuestionOption(BaseModel):
    id: str
    question: str
    type: str
    shown_when: str = "Always"
    options: list[AnswerOption] = []


class OptionsResponse(BaseModel):
    questions: list[QuestionOption]


__all__ = ["AnswerOption", "QuestionOption", "OptionsResponse"]
