"""Skema request endpoint Forecasting Demand."""
from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ForecastingRunRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    rute: str
    horizon_hari: int = Field(30, ge=1, le=90)
    # 'libur_akhir_pekan' adalah nama field lama dari frontend; tetap diterima sebagai alias.
    pakai_kalender: bool = Field(
        True, validation_alias=AliasChoices("pakai_kalender", "libur_akhir_pekan"))
