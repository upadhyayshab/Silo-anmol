import sqlalchemy as db

from SharedBackend.managers import BaseSchema, GenericManager


class ManureReportV1Schema(BaseSchema):
    """One manure scan and the report body returned for it. ``uid`` is the
    report_id. ``kb_reference_id`` is the ``kb_uid`` of the manure_kb entry
    the body was built from (not an FK: kb_uid is only unique per language)."""
    __tablename__ = "manure_reports_v1"
    __table_args__ = (
        db.CheckConstraint("manure_score BETWEEN 1 AND 5", name="ck_manure_reports_v1_score"),
    )

    user_id = db.Column(db.String, nullable=False, index=True)
    cow_id = db.Column(db.String, index=True)
    subs_id = db.Column(db.String)
    has_subs = db.Column(db.Boolean, nullable=False, server_default=db.true())
    ui_version = db.Column(db.String(8), nullable=False)
    lang_code = db.Column(db.String(8), nullable=False, server_default="en")
    img_url = db.Column(db.String)

    manure_score = db.Column(db.SmallInteger, nullable=False)
    manure_sub_score = db.Column(db.String(32))
    kb_reference_id = db.Column(db.String(36), nullable=False, index=True)
    title = db.Column(db.String)
    severity = db.Column(db.String(20))
    priority = db.Column(db.String(20))
    call_vet = db.Column(db.Boolean, nullable=False)
    vet_gate = db.Column(db.String(10))

    score_breakdown = db.Column(db.JSON)
    visual_evidence_as_tags = db.Column(db.JSON)
    data_meta_json = db.Column(db.JSON)
    premium_meta_json = db.Column(db.JSON)
    pr = db.Column(db.JSON)

    reasoning = db.Column(db.Text)
    model = db.Column(db.String(64))
    latency_ms = db.Column(db.Integer)


class ManureReportV1Manager(GenericManager[ManureReportV1Schema]):
    pass


__all__ = ["ManureReportV1Schema", "ManureReportV1Manager"]
