"""随口记 · 微信通道。

自建 iLink 机器人通道，仅使用 Python 标准库。

    aes     纯标准库 AES-128-ECB（iLink 的媒体走加密 CDN，收表格要用）
    ilink   iLink Bot API 客户端（扫码登录 / 长轮询收消息 / 发消息）
    bot     把消息接到记账引擎上

协议要点见 docs/ilink-protocol-notes.md。
"""
