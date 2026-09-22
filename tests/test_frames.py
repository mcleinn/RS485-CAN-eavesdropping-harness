"""Frame codec tests against hand-verified golden vectors."""
from rs485harness.protocols.pylontech import frames as F

# Real US2000B "get analog value" request captured in the wild
# (Photovoltaikforum); both checksums hand-verified.
GOLDEN = bytes.fromhex("7E323030313436343245303032303146443335 0D".replace(" ", ""))


def test_golden_vector_parse():
    pf = F.parse_frame(GOLDEN)
    assert pf.ok, pf.errors
    assert pf.ver == 0x20
    assert pf.adr == 0x01
    assert pf.cid1 == 0x46
    assert pf.cid2 == 0x42
    assert pf.lenid == 2
    assert pf.info == "01"


def test_golden_vector_roundtrip():
    assert F.encode_frame(0x20, 0x01, 0x46, 0x42, "01") == GOLDEN


def test_chksum_spec_example():
    # spec: sequence summing to 0x038E -> CHKSUM 0xFC72
    assert (~0x038E + 1) & 0xFFFF == 0xFC72
    assert F.chksum(b"20014642E00201") == 0xFD35


def test_lchksum_spec_example():
    # spec: LENID 0x012 -> LCHKSUM 0xD
    assert F.lchksum(0x012) == 0xD
    assert F.length_field(0x012) == "D012"
    assert F.lchksum(2) == 0xE
    # nibble-sum multiple of 16 wraps to 0 (4-bit truncation)
    assert F.lchksum(0x0F1) == 0x0


def test_empty_info_length_field():
    assert F.length_field(0) == "0000"
    frame = F.encode_frame(0x20, 0x02, 0x46, 0x61, "")
    pf = F.parse_frame(frame)
    assert pf.ok and pf.lenid == 0 and pf.info == ""


def test_chksum_mismatch_detected():
    bad = GOLDEN[:-3] + b"36\r"  # corrupt last checksum digit
    pf = F.parse_frame(bad)
    assert not pf.ok
    assert any("chksum-mismatch" in e for e in pf.errors)
    assert pf.ver == 0x20  # tolerant: fields still extracted


def test_lenid_mismatch_detected():
    # claim 4 info chars but provide 2
    body = "20014642" + F.length_field(4) + "01"
    frame = b"~" + body.encode() + f"{F.chksum(body.encode()):04X}".encode() + b"\r"
    pf = F.parse_frame(frame)
    assert any("lenid-mismatch" in e for e in pf.errors)


def test_missing_soi_eoi():
    pf = F.parse_frame(GOLDEN[1:])
    assert any("missing-SOI" in e for e in pf.errors)
    pf = F.parse_frame(GOLDEN[:-1])
    assert any("missing-EOI" in e for e in pf.errors)


def test_response_rtn_semantics():
    # response header carries RTN 00, not the command echo
    resp = F.encode_frame(0x20, 0x02, 0x46, 0x00, "AA")
    pf = F.parse_frame(resp)
    assert pf.ok
    assert pf.cid2 == 0x00
    assert "OK" in pf.describe("response")


def test_v35_doc_golden_vectors():
    """Worked examples from the V3.5/Deye design doc (docs/refs/
    pylontech-v35-design.txt) - system commands at master ADR 0x12."""
    for cid2, tail in ((0x60, "FDAB"), (0x61, "FDAA"), (0x62, "FDA9"),
                       (0x63, "FDA8")):
        want = f"~201246{cid2:02X}0000{tail}\r".encode()
        assert F.encode_frame(0x20, 0x12, 0x46, cid2, "") == want
    # 0x62 response example: RTN=00, 4 alarm bytes all zero
    pf = F.parse_frame(b"~20124600800800000000FC21\r")
    assert pf.ok and pf.adr == 0x12 and pf.cid2 == 0x00 and pf.info == "00000000"


def test_family_classification():
    assert F.cid2_family(0x42) == "4x"
    assert F.cid2_family(0x61) == "6x"
    assert F.cid2_family(0xEE) == "?"
