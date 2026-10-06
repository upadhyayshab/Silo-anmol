import sqlalchemy as db

from SharedBackend.managers import BaseSchema, GenericManager

SEVERITIES = ("NORMAL_BASELINE", "MONITOR", "WARNING", "URGENT", "EMERGENCY")
VET_GATES = ("NO", "ADVISED", "MANDATORY")

# master_table_v2.csv columns, snake_cased. Visual columns feed the Gemini
# prompt; clinical columns are looked up into the report body afterwards.
VISUAL_COLUMNS = (
    "geometry_3d_form", "top_down_2d_contour_signature", "central_dimple_morphology",
    "perimeter_edge_slope", "texture_and_moisture_class", "radial_bleed_ring_extent",
    "substrate_floor_interaction", "surface_optical_reflectance", "biological_sheen_vs_flash_glare_rule",
    "hex_range", "lab_centroids", "dietary_color_confounder_disambiguation",
    "fiber_mastication_and_particle_length", "gas_bubbles_foam_morphology",
    "mucus_fibrin_and_blood_morphology", "freshness_vs_sun_crust_gate", "mandatory_visual_inclusions",
    "mandatory_visual_exclusions", "primary_structural_discriminator", "visual_lookalike_discriminator",
    "strict_negative_exclusion_logic", "camera_bias_and_confounder_gates", "animal_context_gate",
    "abstain_escalate_if",
)
CLINICAL_COLUMNS = (
    "diagnosed_clinical_condition", "root_cause_etiology", "milk_withdrawal_risk", "evidence_basis",
    "on_farm_confirmatory_check", "actionable_advice",
)


class ManureKbSchema(BaseSchema):
    """One manure knowledge-base entry in one language and version.

    Rows are either a 27-way sub-code (e.g. '4-S-W', the premium classification)
    or a score-level free-tier entry (e.g. '4_FREE'), which leaves most of the
    descriptive columns empty — hence they are nullable.

    ``kb_uid`` identifies the entry across languages: the 'en' row and a future
    'hi' row of the same sub-code/version share it. Reports store it as
    ``kb_reference_id``. A KB edit inserts a new version and deactivates the
    old one instead of updating in place.
    """
    __tablename__ = "manure_kb"
    __table_args__ = (
        db.UniqueConstraint("kb_uid", "lang_code", name="uq_manure_kb_kb_uid_lang"),
        db.CheckConstraint("score BETWEEN 1 AND 5", name="ck_manure_kb_score"),
        db.CheckConstraint(f"severity IN {SEVERITIES}", name="ck_manure_kb_severity"),
        db.CheckConstraint(f"vet_gate IN {VET_GATES}", name="ck_manure_kb_vet_gate"),
        db.CheckConstraint("min_confidence_to_report BETWEEN 0 AND 1", name="ck_manure_kb_min_confidence"),
        # At most one live version of a sub-code per language.
        db.Index(
            "uq_manure_kb_active_sub_code_lang", "sub_code", "lang_code", unique=True,
            postgresql_where=db.text("is_active AND deleted_at IS NULL"),
        ),
    )

    kb_uid = db.Column(db.String(36), nullable=False, index=True)
    lang_code = db.Column(db.String(8), nullable=False, server_default="en", index=True)
    sub_code = db.Column(db.String(32), nullable=False)
    version = db.Column(db.Integer, nullable=False, server_default="1")
    is_active = db.Column(db.Boolean, nullable=False, server_default=db.true())

    score = db.Column(db.SmallInteger, nullable=False, index=True)
    consistency_class = db.Column(db.String(32))
    sub_type_name = db.Column(db.String)
    colour_group = db.Column(db.String(32))
    severity = db.Column(db.String(20), index=True)
    action_type_enum = db.Column(db.String(64))
    vet_gate = db.Column(db.String(10))
    re_scan_window = db.Column(db.String)
    min_confidence_to_report = db.Column(db.Numeric(3, 2))

    geometry_3d_form = db.Column(db.Text)
    top_down_2d_contour_signature = db.Column(db.Text)
    central_dimple_morphology = db.Column(db.Text)
    perimeter_edge_slope = db.Column(db.Text)
    texture_and_moisture_class = db.Column(db.Text)
    radial_bleed_ring_extent = db.Column(db.Text)
    substrate_floor_interaction = db.Column(db.Text)
    surface_optical_reflectance = db.Column(db.Text)
    biological_sheen_vs_flash_glare_rule = db.Column(db.Text)
    hex_range = db.Column(db.Text)
    lab_centroids = db.Column(db.Text)
    dietary_color_confounder_disambiguation = db.Column(db.Text)
    fiber_mastication_and_particle_length = db.Column(db.Text)
    gas_bubbles_foam_morphology = db.Column(db.Text)
    mucus_fibrin_and_blood_morphology = db.Column(db.Text)
    freshness_vs_sun_crust_gate = db.Column(db.Text)
    mandatory_visual_inclusions = db.Column(db.Text)
    mandatory_visual_exclusions = db.Column(db.Text)
    primary_structural_discriminator = db.Column(db.Text)
    visual_lookalike_discriminator = db.Column(db.Text)
    strict_negative_exclusion_logic = db.Column(db.Text)
    camera_bias_and_confounder_gates = db.Column(db.Text)
    animal_context_gate = db.Column(db.Text)
    abstain_escalate_if = db.Column(db.Text)

    diagnosed_clinical_condition = db.Column(db.Text)
    root_cause_etiology = db.Column(db.Text)
    milk_withdrawal_risk = db.Column(db.Text)
    evidence_basis = db.Column(db.Text)
    on_farm_confirmatory_check = db.Column(db.Text)
    actionable_advice = db.Column(db.Text)

    # Not in master_table_v2.csv yet; filled in by the KB owners later.
    call_vet_if = db.Column(db.Text)
    pr = db.Column(db.JSON)


class ManureKbManager(GenericManager[ManureKbSchema]):
    pass


__all__ = [
    "ManureKbSchema", "ManureKbManager", "SEVERITIES", "VET_GATES", "VISUAL_COLUMNS", "CLINICAL_COLUMNS",
]
