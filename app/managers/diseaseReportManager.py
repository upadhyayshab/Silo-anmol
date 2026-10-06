import sqlalchemy as db

from SharedBackend.managers import BaseSchema, GenericManager


class DiseaseReportV1Schema(BaseSchema):
    """One cattle disease screening scan and the findings returned for it.

    ``uid`` is the report_id. ``findings`` stores the JSON list of DiseaseFinding.
    """
    __tablename__ = "disease_reports_v1"

    user_id = db.Column(db.String, nullable=False, index=True)
    cow_id = db.Column(db.String, index=True)
    lang_code = db.Column(db.String(8), nullable=False, server_default="en")
    img_url = db.Column(db.String)

    is_cattle_image = db.Column(db.Boolean, nullable=False)
    image_view = db.Column(db.String(64))

    findings = db.Column(db.JSON)
    detected_count = db.Column(db.SmallInteger, default=0)
    suspected_count = db.Column(db.SmallInteger, default=0)
    primary_diagnosis = db.Column(db.String)

    reasoning = db.Column(db.Text)
    model = db.Column(db.String(64))
    latency_ms = db.Column(db.Integer)


class DiseaseReportV1Manager(GenericManager[DiseaseReportV1Schema]):
    pass


__all__ = ["DiseaseReportV1Schema", "DiseaseReportV1Manager"]
