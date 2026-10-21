"""Huawei R48xx (R4850G2, R4830, R4875) rectifier CAN protocol, community
reverse-engineered. See docs/refs/huawei/NOTES.md for every source."""
from .ids import HuaweiId, BITRATE, request_id, reply_id, looks_huawei  # noqa: F401
from .regs import (decode, set_frame, request_frame, get_frame, StatusBurst,  # noqa: F401
                   ELabel, CURRENT_SCALES, VOLTAGE_RANGES, ONLINE_TIMEOUT_S)
