from fastapi import APIRouter

from config import get_settings, get_engine
from managers import CropManager
from models import AnswerOption, OptionsResponse, QuestionOption

router = APIRouter(prefix="/options")
settings = get_settings()
engine = get_engine(settings.name)
crop_manager = CropManager(engine)

STATIC_QUESTIONS: list[QuestionOption] = [
    QuestionOption(
        id="Q1", question="Where is your farm?", type="GPS + text fallback",
        options=[
            AnswerOption(code="GPS", label="Use my location"),
            AnswerOption(code="MANUAL", label="Type village / district"),
            AnswerOption(code="PINCODE", label="6-digit pincode"),
        ],
    ),
    QuestionOption(
        id="Q2", question="Which season are you planting for?", type="Single select",
        options=[
            AnswerOption(code="KHARIF", label="Kharif"),
            AnswerOption(code="RABI", label="Rabi"),
            AnswerOption(code="ZAID", label="Summer"),
            AnswerOption(code="LONGTERM", label="Long-term (fruit / plantation)"),
        ],
    ),
    QuestionOption(
        id="Q3", question="What is your main crop now?", type="Single, searchable",
        options=[],  # populated from the crops table at request time
    ),
    QuestionOption(
        id="Q3a", question="What was on this plot before?", type="Single select",
        options=[
            AnswerOption(code="SAME", label="Same"),
            AnswerOption(code="OTHER", label="Different"),
            AnswerOption(code="FALLOW", label="Fallow"),
        ],
    ),
    QuestionOption(
        id="Q3b", question="Which crop was it?", type="Single, searchable",
        shown_when="Only if Q3a=OTHER",
        options=[],  # populated from the crops table at request time
    ),
    QuestionOption(
        id="Q4", question="What is your soil texture?", type="Single select (13 USDA classes)",
        shown_when="Auto from soil map / farmer confirms",
        options=[
            AnswerOption(code="0", label="No Data"), AnswerOption(code="1", label="Clay"),
            AnswerOption(code="2", label="Silty Clay"), AnswerOption(code="3", label="Silty Clay Loam"),
            AnswerOption(code="4", label="Sandy Clay"), AnswerOption(code="5", label="Sandy Clay Loam"),
            AnswerOption(code="6", label="Clay Loam"), AnswerOption(code="7", label="Silt"),
            AnswerOption(code="8", label="Silt Loam"), AnswerOption(code="9", label="Loam"),
            AnswerOption(code="10", label="Sand"), AnswerOption(code="11", label="Loamy Sand"),
            AnswerOption(code="12", label="Sandy Loam"),
        ],
    ),
    QuestionOption(
        id="Q5", question="How do you water this field? (select all you have)", type="MULTI select (>=1)",
        options=[
            AnswerOption(code="RAINFED", label="Rainfed"),
            AnswerOption(code="SURFACE", label="Surface (flood/canal/furrow)"),
            AnswerOption(code="DRIP", label="Drip"),
            AnswerOption(code="SPRINKLER", label="Sprinkler"),
        ],
    ),
    QuestionOption(
        id="Q7", question="How much can you spend per acre next season?", type="Single select",
        options=[
            AnswerOption(code="B1", label="<₹10k"), AnswerOption(code="B2", label="₹10-25k"),
            AnswerOption(code="B3", label="₹25-50k"), AnswerOption(code="B4", label=">₹50k"),
        ],
    ),
]


@router.get("", response_model=OptionsResponse)
async def get_options():
    # A to Z: SharedBackend reads a bare or "+" sort as descending, "-" as ascending.
    crops = (await crop_manager.fetch_all(limit=0, sorts=["-name_en"])).items
    crop_options = [AnswerOption(code=crop.name_en, label=crop.name_en) for crop in crops]

    questions = []
    for q in STATIC_QUESTIONS:
        q = q.model_copy(deep=True)
        if q.id in ("Q3", "Q3b"):
            q.options = crop_options
        questions.append(q)

    return OptionsResponse(questions=questions)
