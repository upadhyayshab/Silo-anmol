from typing import Optional

from fastapi import APIRouter, Depends, Query

from config import get_engine, get_settings
from dependencies.auth import require_jwt
from managers.cropManagers import CropManager
from models.wizardModels import (
    FarmContext, WizardQuestion, WizardQuestionOption, WizardQuestionsResponse,
)
from services.agroInternalService import AgroInternalService

router = APIRouter(tags=["Wizard v2"])
settings = get_settings()
engine = get_engine(settings.name)

crop_manager = CropManager(engine)
agro_internal_service = AgroInternalService(
    base_url=settings.agro_internal_base_url,
    shared_secret=settings.internal_service_secret,
)

ALL_QUESTIONS: list[WizardQuestion] = [
    WizardQuestion(id="location", type="map_pin", label="Where is your farm?", required=True),
    WizardQuestion(
        id="season", type="radio", label="Which season are you planning to sow for?",
        options=[
            WizardQuestionOption(label="Kharif (Monsoon: Jun - Oct)", value="Kharif"),
            WizardQuestionOption(label="Rabi (Winter: Oct - Mar)", value="Rabi"),
            WizardQuestionOption(label="Summer / Zaid (Mar - Jun)", value="Summer"),
            WizardQuestionOption(label="Perennial / Annual", value="Annual"),
        ],
    ),
    WizardQuestion(
        id="has_active_crop", type="radio", label="Do you currently have a crop growing?",
        options=[WizardQuestionOption(label="Yes", value="Yes"), WizardQuestionOption(label="No", value="No")],
    ),
    WizardQuestion(id="crop", type="search", label="Which crop is growing?", required=False, description="Shown only if has_active_crop=Yes"),
    WizardQuestion(id="sowing_date", type="date", label="When did you sow?", required=False, description="Shown only if has_active_crop=Yes"),
    WizardQuestion(id="previous_crop", type="search", label="What was grown here before?", required=False),
    WizardQuestion(
        id="irrigation", type="multi_select", label="How do you water this field?",
        options=[
            WizardQuestionOption(label="Rainfed", value="Rainfed"),
            WizardQuestionOption(label="Surface (flood/canal/furrow)", value="Surface"),
            WizardQuestionOption(label="Drip", value="Drip"),
            WizardQuestionOption(label="Sprinkler", value="Sprinkler"),
        ],
    ),
    WizardQuestion(
        id="budget", type="radio", label="What is your planned investment capacity per acre?",
        options=[
            WizardQuestionOption(label="Low (< ₹15,000 / acre)", value="low"),
            WizardQuestionOption(label="Medium (₹15,000 - ₹35,000 / acre)", value="medium"),
            WizardQuestionOption(label="High (> ₹35,000 / acre)", value="high"),
        ],
    ),
    WizardQuestion(
        id="livestock", type="radio", label="Do you have livestock on your farm?",
        options=[
            WizardQuestionOption(label="Cattle / Buffaloes", value="cattle"),
            WizardQuestionOption(label="Goats / Sheep", value="small_ruminants"),
            WizardQuestionOption(label="No livestock", value="none"),
        ],
    ),
    WizardQuestion(id="farm_name", type="text", label="Give your farm a name", required=False),
]

# TRD §8.2 suppression logic, keyed by which farm field (on the Agro internal
# response) implies the question is already answered.
SUPPRESSION_RULES: list[tuple[str, list[str]]] = [
    ("center", ["location"]),
    ("name", ["farm_name"]),
    ("crop", ["has_active_crop", "crop", "sowing_date"]),
    ("previous_crop", ["previous_crop"]),
    ("irrigation_methods", ["irrigation"]),
]


@router.get("/wizard/questions", response_model=WizardQuestionsResponse, dependencies=[Depends(require_jwt)])
async def get_wizard_questions(farmId: Optional[str] = Query(None)):
    farm_context = None
    suppressed_ids: set[str] = set()

    if farmId:
        farm = await agro_internal_service.get_farm(farmId, user_id="")
        if farm:
            farm_context = FarmContext(
                farm_id=farm.get("farm_id") or farm.get("id"),
                name=farm.get("name"),
                location=farm.get("location") or farm.get("center"),
                crop=farm.get("crop"),
                has_active_crop=bool(farm.get("has_active_crop")),
                irrigation=farm.get("irrigation") or farm.get("irrigation_methods"),
                boundary_status=farm.get("boundary_status"),
            )
            for farm_field, suppressed in SUPPRESSION_RULES:
                if farm.get(farm_field):
                    suppressed_ids.update(suppressed)

    questions = [q for q in ALL_QUESTIONS if q.id not in suppressed_ids]
    return WizardQuestionsResponse(farm_context=farm_context, questions=questions)
