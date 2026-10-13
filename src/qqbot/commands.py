"""User-facing recording, consent and style controls.

These commands change local preferences; reviewed exports remain an operator
operation. Memory pause/forget synchronization runs before delivery.
"""


class Commands:
    def __init__(self, bot):
        self.bot = bot

    def handle(self, message):
        if message.text in ("/help", "帮助"):
            answer = (
                "我是对话助手。私聊直接提问；群聊 @我、引用我的消息或 /ai 问题。\n"
                "/reset 清除上下文；/插件 查看只读工具。\n"
                "/动态 查看空间发布和互动命令（仅管理员私聊可用）。\n"
                "/我的偏好 看语气设置；/学习暂停、/学习恢复；/风格重置 恢复默认口吻。\n"
                "互动会保存到机器人服务器，默认不用于训练。/记录关闭 可停止记录；"
                "/记录开启 可恢复；/忘记我 删除已有记录并停记。\n"
                "/训练允许 或 /训练禁止 控制你的记录能否进入人工审核后的训练导出。"
            )
        elif message.text == "/reset":
            self.bot.history.pop(message.key, None)
            if self.bot.archive:
                self.bot.archive.reset_context(message.user, message.group)
            answer = "已清除当前会话的上下文；服务器存档保留。删除存档请发送 /忘记我。"
        elif message.text in ("/记录开启", "/记录关闭", "/训练允许", "/训练禁止", "/忘记我"):
            if not self.bot.archive:
                answer = "持久记录尚未启用。"
            elif message.text == "/忘记我":
                self.bot.archive.forget(message.user)
                for key in list(self.bot.history):
                    if key[1] == message.user:
                        self.bot.history.pop(key)
                answer = "已删除服务器中的你的互动记录，并关闭后续记录及训练导出。已导出的副本需要管理员另行删除。"
            elif message.text.startswith("/记录"):
                self.bot.archive.configure(message.user, capture=message.text == "/记录开启")
                answer = (
                    "已开启互动记录，训练用途仍按你的独立设置处理。"
                    if message.text == "/记录开启"
                    else "已关闭后续记录；已有记录保留，可用 /忘记我 删除。"
                )
            else:
                self.bot.archive.configure(message.user, training=message.text == "/训练允许")
                answer = (
                    "已允许你的记录参与人工审核后的训练导出，尚未提交任何训练任务。"
                    if message.text == "/训练允许"
                    else "已禁止你的记录进入后续训练导出。"
                )
        elif message.text == "/插件":
            answer = "已启用只读插件：" + (
                "、".join(self.bot.plugins.functions) if self.bot.plugin_url else "暂无"
            )
        elif message.text in ("/我的偏好", "/学习暂停", "/学习恢复", "/风格重置"):
            if not self.bot.styles:
                answer = "语气学习还没开。"
            else:
                actor = self.bot.archive.actor(message.user)
                scope = self.bot.archive.scope(message.user, message.group)
                if message.text == "/我的偏好":
                    answer = self.bot.styles.describe(actor, scope)
                elif message.text == "/风格重置":
                    self.bot.styles.reset(actor, scope)
                    answer = "这段会话的语气偏好已重置，不会再从同一批旧消息重新学。"
                else:
                    self.bot.styles.configure(actor, message.text == "/学习恢复")
                    answer = (
                        "先不学新的了，已有的语气偏好还会用。"
                        if message.text == "/学习暂停"
                        else "继续学。你说哪里不对，我会慢慢调整。"
                    )
        else:
            return None
        return answer
