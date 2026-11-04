"""自在量化公开数据源。"""


class ZzshareError(RuntimeError):
    """采集失败，区别于成功无记录；权限与限流可由调用方分别处理。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
