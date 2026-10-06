import sqlalchemy as db

from SharedBackend.managers import BaseSchema, GenericManager

# Visual columns feed the Gemini prompt
VISUAL_COLUMNS = (
    "required_image_view",
    "detectable_from_side_image",
    "visual_detectability",
    "regions_to_inspect",
    "key_visual_signs",
    "supporting_visual_signs",
    "posture_gait_behaviour_cues",
    "look_alikes_and_how_to_differentiate",
    "signs_that_argue_against",
    "minimum_evidence_to_report",
    "confidence_rules",
    "image_quality_needed",
    "notes_for_model",
)

# Clinical and farmer-facing columns are looked up after the model answers
CLINICAL_COLUMNS = (
    "severity_and_urgency",
    "risk_context",
    "farmer_question",
    "confirmatory_action",
)


class DiseaseKbSchema(BaseSchema):
    """One cattle disease knowledge-base entry in one language and version.

    ``kb_uid`` identifies the entry across languages: the 'en' row and a future
    'hi' row of the same disease/version share it. A KB edit inserts a new
    version and deactivates the old one instead of updating in place.
    """
    __tablename__ = "disease_kb"
    __table_args__ = (
        db.UniqueConstraint("kb_uid", "lang_code", name="uq_disease_kb_kb_uid_lang"),
        # At most one live version of a disease per language.
        db.Index(
            "uq_disease_kb_active_disease_id_lang", "disease_id", "lang_code", unique=True,
            postgresql_where=db.text("is_active AND deleted_at IS NULL"),
        ),
    )

    kb_uid = db.Column(db.String(36), nullable=False, index=True)
    lang_code = db.Column(db.String(8), nullable=False, server_default="en", index=True)
    disease_id = db.Column(db.String(32), nullable=False, index=True)
    disease_name = db.Column(db.String, nullable=False)
    system = db.Column(db.String(128), index=True)
    also_known_as = db.Column(db.Text)
    version = db.Column(db.Integer, nullable=False, server_default="1")
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.true())

    required_image_view = db.Column(db.Text)
    detectable_from_side_image = db.Column(db.String(128))
    visual_detectability = db.Column(db.String(128))
    regions_to_inspect = db.Column(db.Text)
    key_visual_signs = db.Column(db.Text)
    supporting_visual_signs = db.Column(db.Text)
    posture_gait_behaviour_cues = db.Column(db.Text)
    look_alikes_and_how_to_differentiate = db.Column(db.Text)
    signs_that_argue_against = db.Column(db.Text)
    minimum_evidence_to_report = db.Column(db.Text)
    confidence_rules = db.Column(db.Text)
    image_quality_needed = db.Column(db.Text)
    notes_for_model = db.Column(db.Text)

    severity_and_urgency = db.Column(db.Text)
    risk_context = db.Column(db.Text)
    farmer_question = db.Column(db.Text)
    confirmatory_action = db.Column(db.Text)

    pr = db.Column(db.JSON)


class DiseaseKbManager(GenericManager[DiseaseKbSchema]):
    pass


__all__ = [
    "DiseaseKbSchema",
    "DiseaseKbManager",
    "VISUAL_COLUMNS",
    "CLINICAL_COLUMNS",
]
