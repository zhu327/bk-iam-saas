# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-权限中心(BlueKing-IAM) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import logging
from abc import ABC, abstractmethod
from functools import wraps
from typing import Dict, Type

from rest_framework.request import Request

from backend.audit.models import get_event_model
from backend.common.local import local

from .constants import AuditSourceType

logger = logging.getLogger("app")


class NoNeedAuditException(Exception):
    """
    不需要的审计
    """

    pass


class DataProvider(ABC):
    type = ""  # 可以使用 property 覆盖

    def __init__(self, request: Request):
        """
        初始化会在view调用之前, 允许做一些前置的查询
        """
        self.request = request

    @property
    @abstractmethod
    def extra(self) -> Dict:
        pass

    @property
    @abstractmethod
    def object_type(self) -> str:
        pass

    @property
    @abstractmethod
    def object_name(self) -> str:
        pass

    @property
    @abstractmethod
    def object_id(self) -> str:
        pass

    @property
    def system_id(self) -> str:
        return ""


def view_audit_decorator(provider_cls: Type[DataProvider]):
    """
    记录审计信息的装饰器
    """

    def decorate(func):
        @wraps(func)
        def wrapper(view, request, *args, **kwargs):
            # 实例化审计信息提供者
            provider = provider_cls(request)

            response = func(view, request, *args, **kwargs)

            try:
                # 记录审计信息
                log_api_event(request, provider)
            except NoNeedAuditException:
                pass
            except Exception:  # pylint: disable=broad-except
                logger.exception("audit error")

            return response

        return wrapper

    return decorate


def log_api_event(request, provider: DataProvider):
    """
    记录审计事件
    """
    Event = get_event_model()

    event = Event(
        source_data_request_id=request.request_id,
        type=provider.type,
        username=request.user.username,
        role_type=request.role.type,
        role_id=request.role.id,
        system_id=provider.system_id,
        object_type=provider.object_type,
        object_id=provider.object_id,
        object_name=provider.object_name,
    )

    event.extra = provider.extra

    event.source_type, event.source_data_app_code = _parse_request_audit_type(request)

    event.save(force_insert=True)


def _parse_request_audit_type(request):
    if "/api/v1/open/" in request.path:
        return AuditSourceType.OPENAPI.value, request.bk_app_code

    return AuditSourceType.WEB.value, ""


def audit_context_setter(**kwargs):
    """
    设置audit context到请求对象
    """
    # Django Request
    request = local.request
    if not request:
        return

    if not hasattr(request, "_audit_context"):
        setattr(request, "_audit_context", {})
    request._audit_context.update(kwargs)


def audit_context_getter(request: Request, key: str):
    """
    获取请求audit context的属性
    """
    # 从DRF Request获取Django Request
    _request = request._request

    if not hasattr(_request, "_audit_context"):
        setattr(_request, "_audit_context", {})
    return _request._audit_context.get(key)


def add_audit(provider_cls: Type[DataProvider], request: Request, **kwargs):
    """
    直接记录审计信息（非通过装饰器方式）
    """
    # 设置审计对象和额外信息，直接覆盖，避免传递过来的request对象重复使用导致_audit_context存储了上次调用的信息
    # 这里使用的是Django Request，provider_cls获取相关内容时使用的audit_context_getter方法也是从Django Request里获取
    setattr(request._request, "_audit_context", kwargs)
    # 实例化审计信息提供者
    provider = provider_cls(request)
    try:
        # 记录审计信息
        log_api_event(request, provider)
    except NoNeedAuditException:
        pass
    except Exception:  # pylint: disable=broad-except
        logger.exception("audit error")
