"""源中立的数据源错误类型。

插件可直接抛出这些类型(或其子类), 兜底链调度 (routing_health) 据此分类熔断:
  - ProviderAuthError: Key 失效 / 权限不足, 重试无意义 → 熔断到 Key 变更或手动重试
  - ProviderQuotaError: 限频或额度耗尽 → 熔断到北京时间次日 00:00 (retry_after 更短时取之)
  - ProviderDataError: 返回空 / 格式不符 / 校验失败 → 计一次失败
未识别的其它异常一律按瞬时错误 (超时 / 5xx) 处理。
"""

from __future__ import annotations


class ProviderError(Exception):
    """数据源错误基类。"""


class ProviderAuthError(ProviderError):
    """认证失败或权限不足。"""


class ProviderQuotaError(ProviderError):
    """请求频率超限或调用额度耗尽; retry_after 为建议等待秒数 (未知为 None)。"""

    def __init__(self, *args, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class ProviderDataError(ProviderError):
    """返回数据为空、格式不符或校验失败。"""
