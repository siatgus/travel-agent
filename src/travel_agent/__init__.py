"""智能旅行规划助手：真实天气/路网/班次数据驱动的本地旅行规划服务。

包结构（src 布局）：
- 意图与对话：intent / dialogue / agent / composer
- 工具层：tools（门面）→ real_tools（真实数据）| mock_tools
- 服务：server（8787 聊天）| admin（8788 后台）
- 账号与持久化：auth / conversations / kb / llm
运行时数据统一放 data/（见 paths.py），可用 TRAVEL_DATA_DIR 重定向。
"""

__version__ = "1.0.0"
