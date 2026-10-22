"""Rule-based extraction of TruFru receiving packets from ``ai_parse_document`` JSON. No LLM.

    from packet_parser import build_final, write_final
    result = build_final("docs/my_docs/data/many/<stem>/<stem>.ai_parse.json")
    result["numeric_confirm"]   # every number vs printed text + math rules
    result["master_match"]      # fuzzy-matcher verdict vs master CSVs + checks

The two check layers can also be used on their own::

    from packet_parser.fuzzy_matcher import get_matcher, match_text
    from packet_parser.numeric_confirm import numeric_confirm
    from packet_parser.text_reader import read_printed_text
"""

from .parse import build_final, find_parses, parse_doc, write_final
from .schema import ParsedPacket, ReceivingPacket

__all__ = ["build_final", "find_parses", "parse_doc", "write_final", "ParsedPacket", "ReceivingPacket"]
