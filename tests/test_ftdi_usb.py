"""Hardware-free tests for the FTDI-over-libusb transport helpers."""
import os
import pytest

from rs485harness.core import ftdi_usb as F


def test_baud_words_known_values():
    # 3 MHz / 9600 = 312.5 -> divisor 312, fraction .5 (code 1 -> bits 14-15 = 01)
    assert F.ftdi_baud_words(9600) == (312 | (1 << 14), 0)
    # 3 MHz / 19200 = 156.25 -> code for .25 is 2
    assert F.ftdi_baud_words(19200) == (156 | (2 << 14), 0)
    # 115200: 26.04 -> 26, no fraction
    assert F.ftdi_baud_words(115200) == (26, 0)
    # 3 Mbaud -> divisor 0 ; 2 Mbaud -> divisor 1 (AN232B-05 special cases)
    assert F.ftdi_baud_words(3_000_000) == (0, 0)
    assert F.ftdi_baud_words(2_000_000) == (1, 0)
    with pytest.raises(ValueError):
        F.ftdi_baud_words(0)


def test_strip_status_single_and_multi_packet():
    assert F.strip_status(b"\x01\x60abc", 64) == b"abc"
    assert F.strip_status(b"", 64) == b""
    assert F.strip_status(b"\x01\x60", 64) == b""           # status only, no data
    pkt1 = b"\x01\x60" + bytes(62)
    pkt2 = b"\x01\x60" + b"xyz"
    assert F.strip_status(pkt1 + pkt2, 64) == bytes(62) + b"xyz"


def test_parse_usb_spec_variants(monkeypatch):
    monkeypatch.delenv("RSH_USB_FD", raising=False)
    assert F.parse_usb_spec("usb:12:B") == (12, 1)
    assert F.parse_usb_spec("usb:12") == (12, 0)
    assert F.parse_usb_spec("USB:7:1") == (7, 1)
    with pytest.raises(ValueError):          # no fd anywhere
        F.parse_usb_spec("usb:A")
    monkeypatch.setenv("RSH_USB_FD", "33")
    assert F.parse_usb_spec("usb:A") == (33, 0)
    assert F.parse_usb_spec("usb:b") == (33, 1)
    assert F.parse_usb_spec("usb:") == (33, 0)
    with pytest.raises(ValueError):
        F.parse_usb_spec("usb:33:Z")
    with pytest.raises(ValueError):
        F.parse_usb_spec("COM5")


def test_open_link_dispatches_usb_spec(monkeypatch):
    """open_link must route usb: specs to the FTDI link (which fails cleanly
    without libusb/hardware instead of trying pyserial)."""
    from rs485harness.core.transport import open_link
    monkeypatch.setenv("RSH_USB_FD", "999")
    with pytest.raises(Exception) as ei:
        open_link("usb:A", 9600, name="x")
    # either libusb1 missing (ImportError) or wrapSysDevice failing on a bogus fd
    assert "serial" not in type(ei.value).__module__


def test_termux_exec_requires_fd(capsys):
    from rs485harness.termux_exec import main
    assert main.__module__ == "rs485harness.termux_exec"
    import sys
    old = sys.argv
    try:
        sys.argv = ["x", "ports"]
        assert main() == 2
    finally:
        sys.argv = old
