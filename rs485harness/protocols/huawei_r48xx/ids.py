"""Huawei R48xx rectifier CAN identifiers (29-bit), community-documented.

Layout (docs/refs/huawei/NOTES.md section 3; verified by patagonaa on
R4830S1/R4850G2/R4850G6, same in BotoX, esphome, iPuzzle and Huawei's own
R100030G1 guide):

    bits 28-23  protocol  0x21 controller<->PSU, 0x20 PSU<->PSU
    bits 22-16  address   0 broadcast, 1 first PSU, 2 second ...
    bits 15-8   command   0x40 data, 0x50 info, 0xD2 e-label, 0x80 set,
                          0x82 get, 0x11 unsolicited, 0x10 address negotiation
    bit  7      to_psu    1 controller -> PSU, 0 PSU -> controller
    bits 6-2    group     0x1F = no grouping
    bit  1      sw_addr   1 = software address (always 1 in practice)
    bit  0      cont      1 = more frames follow, 0 = last frame

    0x108040FE  status request to all      0x1081407F / 0x1081407E  PSU 1 burst
    0x108180FE  set register on PSU 1      0x1081807E               set ack
    0x1001117E  PSU 1 unsolicited (~377 ms), protocol 0x20
"""
from __future__ import annotations

from dataclasses import dataclass

PROTO_CTL = 0x21
PROTO_PSU = 0x20
CMD_ADDR = 0x10
CMD_UNSOL = 0x11
CMD_DATA = 0x40
CMD_INFO = 0x50
CMD_SET = 0x80
CMD_CFG = 0x81
CMD_GET = 0x82
CMD_ELABEL = 0xD2
CMD_NAMES = {CMD_ADDR: "addr-negotiation", CMD_UNSOL: "unsolicited", CMD_DATA: "data",
             CMD_INFO: "info", CMD_SET: "set", CMD_CFG: "config", CMD_GET: "get",
             CMD_ELABEL: "elabel"}
GROUP_ALL = 0x1F
BITRATE = 125_000


@dataclass(frozen=True)
class HuaweiId:
    proto: int
    addr: int
    cmd: int
    to_psu: bool
    cont: bool = False
    group: int = GROUP_ALL
    sw_addr: bool = True

    def pack(self) -> int:
        return ((self.proto & 0x3F) << 23 | (self.addr & 0x7F) << 16 |
                (self.cmd & 0xFF) << 8 | int(self.to_psu) << 7 |
                (self.group & 0x1F) << 2 | int(self.sw_addr) << 1 | int(self.cont))

    @classmethod
    def unpack(cls, can_id: int) -> "HuaweiId":
        return cls(proto=(can_id >> 23) & 0x3F, addr=(can_id >> 16) & 0x7F,
                   cmd=(can_id >> 8) & 0xFF, to_psu=bool(can_id & 0x80),
                   cont=bool(can_id & 1), group=(can_id >> 2) & 0x1F,
                   sw_addr=bool(can_id & 2))

    @property
    def is_huawei(self) -> bool:
        return self.proto in (PROTO_CTL, PROTO_PSU) and self.group == GROUP_ALL

    @property
    def cmd_name(self) -> str:
        return CMD_NAMES.get(self.cmd, f"cmd-{self.cmd:02X}")

    @property
    def flow(self) -> str:
        return "CTL->PSU" if self.to_psu else "PSU->CTL"

    def __str__(self) -> str:
        who = "all" if self.addr == 0 else f"psu{self.addr}"
        return (f"{self.cmd_name} {'to' if self.to_psu else 'from'} {who}"
                + (" (more)" if self.cont else ""))


def request_id(cmd: int, addr: int = 0) -> int:
    """Controller -> PSU identifier for a command (addr 0 = broadcast)."""
    return HuaweiId(PROTO_CTL, addr, cmd, True).pack()


def reply_id(cmd: int, addr: int, cont: bool) -> int:
    return HuaweiId(PROTO_CTL, addr, cmd, False, cont).pack()


def looks_huawei(can_id: int, ext: bool) -> bool:
    return ext and HuaweiId.unpack(can_id).is_huawei


# well-known identifiers (addr 1)
ID_STATUS_REQ_ALL = request_id(CMD_DATA, 0)        # 0x108040FE
ID_SET_PSU1 = request_id(CMD_SET, 1)               # 0x108180FE
ID_SET_ALL = request_id(CMD_SET, 0)                # 0x108080FE
ID_INFO_PSU1 = request_id(CMD_INFO, 1)             # 0x108150FE
ID_ELABEL_PSU1 = request_id(CMD_ELABEL, 1)         # 0x1081D2FE
ID_UNSOL_PSU1 = HuaweiId(PROTO_PSU, 1, CMD_UNSOL, False).pack()   # 0x1001117E
