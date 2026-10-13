"""Recognized memory commands and evidence/prompt boundaries."""

COMMANDS = {"/记忆", "/记忆暂停", "/记忆恢复", "/记忆清空"}
OPERATIONS = {
    "/记忆删除": "retract",
    "/记忆更正": "correct",
    "/计划完成": "complete",
    "/计划取消": "cancel",
    "/记忆保留": "pin",
    "/记忆淡化": "unpin",
}
