"""Rule-based extraction of TruFru receiving packets from ``ai_parse_document`` JSON. No LLM.

    from packet_parser import build_final, write_final
    result = build_final("docs/my_docs/data/many/<stem>/<stem>.ai_parse.json")
"""

from .parse import build_final, find_parses, parse_doc, write_final
from .schema import ParsedPacket, ReceivingPacket

__all__ = ["build_final", "find_parses", "parse_doc", "write_final", "ParsedPacket", "ReceivingPacket"]
