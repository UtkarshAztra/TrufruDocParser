"""Data schema for a TruFru inbound receiving packet (BOL IQF Strawberries).

Copied from TruFru-exp/experiments/v1/Databricks_parser/schema.py; the models are the contract of the
final JSON written by ``packet_parser``.

Each PDF in docs/official-docs/BOL IQF Strawberries/ is one inbound truck of raw
strawberries arriving at the co-man (Van Drunen Farms, Crown Point). A packet
holds up to three documents:

  1. VDF/FC Incoming Material Inspection Report (IMIR)  - mostly handwritten
  2. Van Drunen Farms Purchase Order ("Receive PO")      - printed + handwritten notes
  3. Straight Bill of Lading (3PL warehouse -> VDF)      - printed + stamps

Why it matters (docs/analysis-docs/02 + 04): the VDF usage extract starts when a
lot is issued to a production order. It cannot say when a lot ARRIVED or from
which supplier shipment ("Gap 1 - raw material receipt is never confirmed").
These packets are that receiving record. `TraceabilityRecord` is the row that
joins to the usage data:

  * item_number  (e.g. C01547.AD) -> usage "Item Number"; the C prefix = raw material
  * vdf_lot_number (e.g. C142265) -> usage "Lot (userlot)" for C items
  * po_number_normalized (20260089938) -> same 2026 + 7-digit shape as P-item lots

PII: per org policy the schema captures *whether* a form was signed/initialled,
never the person's name, signature or initials.
"""

from __future__ import annotations

import copy
from datetime import date
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- enums
class Check(str, Enum):
    """A Yes / No / N/A tick-box on the IMIR form."""

    yes = "yes"
    no = "no"
    na = "na"
    blank = "blank"  # nothing ticked / unreadable


class ProductTemperatureClass(str, Enum):
    frozen = "frozen"  # acceptance: <= 10 F
    refrigerated = "refrigerated"  # acceptance: <= 41 F
    ambient = "ambient"
    unknown = "unknown"


class Disposition(str, Enum):
    accepted = "accepted"
    accepted_with_corrective_action = "accepted_with_corrective_action"
    supervisor_qc_contacted = "supervisor_qc_contacted"
    unknown = "unknown"


class DocumentType(str, Enum):
    inspection_report = "inspection_report"
    purchase_order = "purchase_order"
    bill_of_lading = "bill_of_lading"
    coa = "coa"
    packing_list = "packing_list"
    other = "other"


# ------------------------------------------------------------------ shared parts
class Party(BaseModel):
    name: Optional[str] = Field(None, description="Company / facility name")
    address: Optional[str] = Field(None, description="Full address on one line")
    phone: Optional[str] = None
    fax: Optional[str] = None
    code: Optional[str] = Field(None, description="Any account / ship-to code next to the name, e.g. 'web-006567'")


class Stamp(BaseModel):
    """'TRUCK ARRIVED' / 'TRUCK DEPARTED' stamp on the BOL (handwritten values)."""

    stamp_date: Optional[date] = None
    stamp_time: Optional[str] = Field(None, description="24h HH:MM, e.g. '12:00'")
    vdf_initialled: Optional[bool] = Field(None, description="True if the VDF initials box is filled. Never return the initials.")
    driver_initialled: Optional[bool] = Field(None, description="True if the driver initials box is filled. Never return the initials.")
    note: Optional[str] = Field(None, description="Any extra handwriting, e.g. 'seal broke @ 12:30pm'")


# ---------------------------------------------------------- 1. inspection report
class InspectionLot(BaseModel):
    manufacturing_lot: Optional[str] = Field(None, description="Supplier/manufacturer lot as written")
    vdf_lot_number: Optional[str] = Field(None, description="VDF/FC lot number as written")
    boxes_received: Optional[int] = None
    weight_per_box_lbs: Optional[float] = None
    total_weight_lbs: Optional[float] = None


class InspectionReport(BaseModel):
    form_id: Optional[str] = Field(None, description="Footer form code, e.g. '4-FRC-COR-031 VDF/FC IMIR V4 12/16/25'")
    receiving_facility: Optional[str] = Field(None, description="Handwritten facility code, e.g. 'CP' (Crown Point)")
    product_description: Optional[str] = None
    item_number: Optional[str] = Field(None, description="e.g. 'C01547.AD'")
    country_of_origin: Optional[str] = None
    vendor: Optional[str] = None
    order_number: Optional[str] = Field(None, description="PO as printed, e.g. '2026-00-89938'")
    carrier_name: Optional[str] = None
    trailer_seal_number: Optional[str] = None

    order_matches_bol: Check = Check.blank
    returned_product: Check = Check.blank
    ltl_truck_padlocked: Check = Check.blank
    seal_present_matches_bol: Check = Check.blank
    allergens: Optional[str] = Field(None, description="Allergen / sensitivity list if written, else null")
    organic: Check = Check.blank
    kosher: Check = Check.blank
    kosher_symbol_on_label: Check = Check.blank
    packing_list_or_coa_present: Check = Check.blank
    manufacturer_lot_matches_packing_list: Check = Check.blank
    approved_vendor: Check = Check.blank
    all_product_accounted_for: Check = Check.blank

    lots: list[InspectionLot] = Field(default_factory=list, description="Only rows that contain writing")

    # trailer condition
    trailer_clean: Check = Check.blank
    trailer_free_of_damage: Check = Check.blank
    trailer_free_of_odors: Check = Check.blank
    trailer_free_of_pests: Check = Check.blank
    trailer_free_of_chemicals: Check = Check.blank

    # product condition & temperature
    properly_stacked_no_damage: Check = Check.blank
    damaged_boxes_count: Optional[int] = Field(None, description="Number written; null if 'N/A' or empty")
    damage_noted_on_bol: Check = Check.blank
    product_clearly_labeled: Check = Check.blank
    label_matches_po: Check = Check.blank
    product_temperature_class: ProductTemperatureClass = ProductTemperatureClass.unknown
    product_temperature_f: Optional[float] = Field(None, description="Handwritten reading in F, e.g. -30")
    temperature_meets_criteria: Check = Check.blank
    trailer_temp_recorder_present: Check = Check.blank

    pallet_stack_1_ok: Check = Check.blank
    pallet_stack_2_ok: Check = Check.blank
    pallet_stack_3_ok: Check = Check.blank

    disposition: Disposition = Disposition.unknown
    corrective_action: Optional[str] = None
    inspector_signed: Optional[bool] = Field(None, description="Signature present? Never return the name.")
    inspection_date: Optional[date] = None
    verifier_signed: Optional[bool] = Field(None, description="Signature present? Never return the name.")
    verification_date: Optional[date] = None


# -------------------------------------------------------------- 2. purchase order
class POLine(BaseModel):
    our_item_number: Optional[str] = Field(None, description="VDF item #, e.g. 'C01547.AD'")
    legacy_item_number: Optional[str] = None
    vendor_item_number: Optional[str] = None
    item_description: Optional[str] = None
    quantity: Optional[float] = None
    unit_of_measure: Optional[str] = Field(None, description="e.g. 'LB', 'EA'")
    vendor_lot_number: Optional[str] = Field(None, description="Printed 'Vendor/Custom Lot Number' column")


class PurchaseOrder(BaseModel):
    po_number: Optional[str] = Field(None, description="As printed, e.g. '2026-00-89938'")
    facility: Optional[str] = None
    order_date: Optional[date] = None
    ship_via: Optional[str] = None
    freight_terms: Optional[str] = Field(None, description="e.g. 'Prepaid and Add'")
    vendor: Party = Field(default_factory=Party)
    receiving_instructions: Optional[str] = None
    lines: list[POLine] = Field(default_factory=list, description="Include freight lines too")
    handwritten_lot: Optional[str] = Field(None, description="Handwritten lot note, e.g. 'C142265 / IQF / 1-10'")
    handwritten_date: Optional[date] = Field(None, description="Handwritten date near the lot note")
    handwritten_notes: list[str] = Field(default_factory=list, description="Other handwriting, e.g. '70 boxes / full pallet', '20 pallets'")


# --------------------------------------------------------------- 3. bill of lading
class BOLLine(BaseModel):
    actual_qty: Optional[float] = Field(None, description="Cases / boxes shipped")
    item_code: Optional[str] = Field(None, description="Warehouse item code, e.g. '10610'")
    item_description: Optional[str] = None
    lot: Optional[str] = None
    lot_description: Optional[str] = Field(None, description="e.g. 'NATURIPE /NA/CHILE'")
    customer_lot: Optional[str] = None
    upc: Optional[str] = None
    unit_weight_lbs: Optional[float] = None
    best_before_date: Optional[date] = Field(None, description="BBD column")
    net_weight_lbs: Optional[float] = None


class BillOfLading(BaseModel):
    title: Optional[str] = Field(None, description="e.g. 'STRAIGHT BILL OF LADING-SHORT FORM-ORIGINAL-NOT NEGOTIABLE'")
    ship_from: Party = Field(default_factory=Party)
    customer: Party = Field(default_factory=Party)
    ship_to: Party = Field(default_factory=Party)
    principal_to: Party = Field(default_factory=Party)
    temperature_setpoint_f: Optional[float] = None
    booking_number: Optional[str] = None
    transaction_number: Optional[str] = Field(None, description="e.g. 'SCH142033'")
    trailer_number: Optional[str] = None
    ship_date: Optional[date] = None
    seal_number: Optional[str] = None
    po_sales_number: Optional[str] = Field(None, description="Exactly as printed, even if truncated")
    carrier: Optional[str] = None
    prepaid_or_collect: Optional[str] = None
    container_number: Optional[str] = None
    temp_recorder_number: Optional[str] = None
    bol_number: Optional[str] = None
    barcode_labels: list[str] = Field(default_factory=list, description="Text printed under barcodes, e.g. 'SCH142033', 'NAT160'")
    comment: Optional[str] = Field(None, description="Full 'Comment' line verbatim")
    lines: list[BOLLine] = Field(default_factory=list)
    total_qty: Optional[float] = None
    total_net_weight_lbs: Optional[float] = None
    total_gross_weight_lbs: Optional[float] = None
    truck_arrived: Stamp = Field(default_factory=Stamp)
    truck_departed: Stamp = Field(default_factory=Stamp)
    pallets_exchanged_in: Optional[str] = None
    pallets_exchanged_out: Optional[str] = None
    printed_timestamp: Optional[str] = Field(None, description="e.g. '08/31/2026 10:01:57 AM' next to 'CHECK WITH FORMAN'")


# ---------------------------------------------------- 4. traceability (the join row)
class TraceabilityRecord(BaseModel):
    """One receiving event = one row. Best value across the three documents."""

    po_number: Optional[str] = Field(None, description="As printed on the PO, e.g. '2026-00-89938'")
    item_number: Optional[str] = Field(None, description="VDF raw-material item, e.g. 'C01547.AD'")
    product_description: Optional[str] = None
    supplier_name: Optional[str] = Field(None, description="Vendor on the PO, e.g. 'Naturipe Foods LLC'")
    country_of_origin: Optional[str] = None
    supplier_lot: Optional[str] = Field(None, description="Manufacturing lot")
    vdf_lot_number: Optional[str] = Field(None, description="Lot VDF will post in its usage data")
    quantity_cases: Optional[float] = None
    case_weight_lbs: Optional[float] = None
    net_weight_lbs: Optional[float] = None
    gross_weight_lbs: Optional[float] = None
    pallets: Optional[int] = None
    ship_date: Optional[date] = None
    arrival_date: Optional[date] = Field(None, description="TRUCK ARRIVED stamp date, else IMIR date")
    arrival_time: Optional[str] = Field(None, description="24h HH:MM")
    departure_time: Optional[str] = Field(None, description="24h HH:MM")
    best_before_date: Optional[date] = None
    pack_or_production_date: Optional[date] = Field(None, description="Handwritten date next to the lot on the PO, if any (meaning unconfirmed)")
    shipped_from: Optional[str] = Field(None, description="3PL / warehouse name")
    received_at: Optional[str] = Field(None, description="Ship-to facility")
    carrier: Optional[str] = None
    seal_number: Optional[str] = None
    product_temperature_f: Optional[float] = None
    accepted: Optional[bool] = None


# ------------------------------------------------------------------ top-level doc
class ReceivingPacket(BaseModel):
    """The extracted content of one PDF (three documents + the merged traceability row)."""

    documents_present: list[DocumentType] = Field(default_factory=list)
    inspection_report: Optional[InspectionReport] = None
    purchase_order: Optional[PurchaseOrder] = None
    bill_of_lading: Optional[BillOfLading] = None
    traceability: TraceabilityRecord = Field(default_factory=TraceabilityRecord)
    low_confidence_fields: list[str] = Field(
        default_factory=list,
        description="Dotted paths of values that were hard to read (handwriting, stamps), e.g. 'inspection_report.product_temperature_f'",
    )


# ----------------------------------------------- added in Python after extraction
class CrossCheck(BaseModel):
    name: str
    status: str  # pass | fail | unknown
    detail: str


class ParsedPacket(BaseModel):
    """Final JSON written to disk: rule-based extraction + deterministic checks."""

    source_file: str
    parsed_at: str
    parser: dict[str, Any]
    po_number_normalized: Optional[str] = Field(None, description="Digits only, e.g. '20260089938'")
    lot_identifier_type: str = "supplier_lot"  # C item -> supplier lot (see analysis doc 04, Trap 1)
    packet: ReceivingPacket
    sources: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="field path -> {element, page, type, confidence} the value was read from")
    cross_checks: list[CrossCheck] = Field(default_factory=list)
    numeric_confirm: Optional[dict[str, Any]] = Field(
        None, description="Every number checked against the printed text and the math rules R1-R15 (packet_parser.numeric_confirm). None if the step was skipped.")
    master_match: Optional[dict[str, Any]] = Field(
        None, description="Fuzzy-matcher verdict against docs/official-docs/master_*.csv and checks of the extraction against it (packet_parser.master_check). None if the step was skipped.")

