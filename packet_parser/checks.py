"""Deterministic cross-document checks on a validated packet (ported from the v1 ``bol_parser.cross_checks``).

Every check compares values that should agree across the IMIR, the PO and the BOL, or re-does a piece of
arithmetic printed on a form. Status is ``pass`` | ``warn`` | ``fail`` | ``unknown`` (fewer than two sources).
"""

from __future__ import annotations

import re
from typing import Optional

from .schema import CrossCheck, ProductTemperatureClass, ReceivingPacket


def digits(value: Optional[str]) -> Optional[str]:
    d = re.sub(r"\D", "", value or "")
    return d or None


def norm_lot(value: Optional[str]) -> Optional[str]:
    """Strip spaces, uppercase, keep only the first token of notes like 'C142265 / IQF / 1-10'."""
    if not value:
        return None
    return re.split(r"[/,;]", value)[0].replace(" ", "").upper() or None


def _agree(name: str, values: dict[str, Optional[str]], show: dict[str, Optional[str]] | None = None) -> CrossCheck:
    present = {k: v for k, v in values.items() if v}
    shown = ", ".join(f"{k}={(show or values)[k]!r}" for k in present)
    if len(present) < 2:
        return CrossCheck(name=name, status="unknown", detail=f"fewer than two sources ({shown or 'none'})")
    distinct = set(present.values())
    if len(distinct) == 1:
        return CrossCheck(name=name, status="pass", detail=shown)
    longest = max(distinct, key=len)
    if all(longest.startswith(v) for v in distinct):
        return CrossCheck(name=name, status="warn", detail=f"one value looks truncated: {shown}")
    return CrossCheck(name=name, status="fail", detail=shown)


def _close(a: Optional[float], b: Optional[float], tol: float = 0.5) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def cross_checks(p: ReceivingPacket) -> list[CrossCheck]:
    imir, po, bol = p.inspection_report, p.purchase_order, p.bill_of_lading
    bol_line = bol.lines[0] if bol and bol.lines else None
    po_line = next((l for l in (po.lines if po else []) if (l.unit_of_measure or "").upper() in ("LB", "LBS")), None)
    checks: list[CrossCheck] = []

    comment_po = None
    if bol and bol.comment:
        m = re.search(r"\d{4}-\d{2}-\d{5}", bol.comment)
        comment_po = m.group(0) if m else None
    po_raw = {
        "imir": imir.order_number if imir else None,
        "po": po.po_number if po else None,
        "bol": bol.po_sales_number if bol else None,
        "bol_comment": comment_po,
    }
    checks.append(_agree("po_number_consistent", {k: digits(v) for k, v in po_raw.items()}, po_raw))

    checks.append(_agree("item_number_consistent", {
        "imir": (imir.item_number or "").upper() or None if imir else None,
        "po": (po_line.our_item_number or "").upper() or None if po_line else None,
    }))

    lot_raw = {
        "imir_mfg_lot": imir.lots[0].manufacturing_lot if imir and imir.lots else None,
        "imir_vdf_lot": imir.lots[0].vdf_lot_number if imir and imir.lots else None,
        "po_handwritten": po.handwritten_lot if po else None,
        "bol_lot": bol_line.lot if bol_line else None,
    }
    lot_check = _agree("lot_consistent", {k: norm_lot(v) for k, v in lot_raw.items()}, lot_raw)
    if any("O" in (norm_lot(v) or "")[1:] for v in lot_raw.values()):
        lot_check.detail += " | contains letter O after prefix - check O/0 typing variant"
    checks.append(lot_check)

    seal = {"imir": imir.trailer_seal_number if imir else None, "bol": bol.seal_number if bol else None}
    checks.append(_agree("seal_consistent", {k: (digits(v) or "").lstrip("0") or None for k, v in seal.items()}, seal))

    if bol_line and bol_line.actual_qty and bol_line.unit_weight_lbs:
        calc = bol_line.actual_qty * bol_line.unit_weight_lbs
        ok = _close(calc, bol_line.net_weight_lbs)
        checks.append(CrossCheck(name="bol_weight_math", status="pass" if ok else "fail",
                                 detail=f"{bol_line.actual_qty} x {bol_line.unit_weight_lbs} = {calc} vs net {bol_line.net_weight_lbs}"))
    if imir and imir.lots and imir.lots[0].boxes_received and imir.lots[0].weight_per_box_lbs:
        lot = imir.lots[0]
        calc = lot.boxes_received * lot.weight_per_box_lbs
        ok = _close(calc, lot.total_weight_lbs)
        checks.append(CrossCheck(name="imir_weight_math", status="pass" if ok else "fail",
                                 detail=f"{lot.boxes_received} x {lot.weight_per_box_lbs} = {calc} vs total {lot.total_weight_lbs}"))

    qty = {
        "imir_boxes": float(imir.lots[0].boxes_received) if imir and imir.lots and imir.lots[0].boxes_received else None,
        "bol_qty": bol_line.actual_qty if bol_line else None,
    }
    checks.append(_agree("case_count_consistent", {k: (f"{v:g}" if v else None) for k, v in qty.items()}))
    weight = {
        "imir_total_lbs": imir.lots[0].total_weight_lbs if imir and imir.lots else None,
        "po_qty_lbs": po_line.quantity if po_line else None,
        "bol_net_lbs": bol.total_net_weight_lbs if bol else None,
    }
    checks.append(_agree("net_weight_consistent", {k: (f"{v:g}" if v else None) for k, v in weight.items()}))

    if imir and imir.product_temperature_f is not None:
        limit = {ProductTemperatureClass.frozen: 10, ProductTemperatureClass.refrigerated: 41}.get(imir.product_temperature_class)
        if limit is None:
            checks.append(CrossCheck(name="temperature_in_spec", status="unknown", detail="product class not ticked"))
        else:
            ok = imir.product_temperature_f <= limit
            checks.append(CrossCheck(name="temperature_in_spec", status="pass" if ok else "fail",
                                     detail=f"{imir.product_temperature_f} F vs <= {limit} F ({imir.product_temperature_class.value})"))
    return checks
