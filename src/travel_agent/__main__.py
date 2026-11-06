"""python -m travel_agent：启动本地聊天服务（等价于 travel_agent.server）。"""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
