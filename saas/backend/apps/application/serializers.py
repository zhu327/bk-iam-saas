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
import time
from typing import List

from pydantic.tools import parse_obj_as
from rest_framework import serializers

from backend.apps.application.models import Application
from backend.apps.organization.models import User
from backend.apps.policy.serializers import PolicyActionSLZ, ResourceSLZ, ResourceTypeSLZ, ValueFiled
from backend.apps.role.serializers import RatingMangerCreateSLZ
from backend.biz.application import ApplicationBiz
from backend.biz.subject import SubjectInfoList
from backend.biz.system import SystemBiz
from backend.common.time import PERMANENT_SECONDS, expired_at_display
from backend.service.constants import ApplicationTypeEnum
from backend.service.models import Subject

from .base_serializers import BaseAggActionListSLZ, SystemInfoSLZ, validate_action_repeat


class ExpiredAtSLZ(serializers.Serializer):
    expired_at = serializers.IntegerField(label="过期时间", required=True, max_value=PERMANENT_SECONDS)

    def validate_expired_at(self, value):
        """
        验证过期时间
        """
        if value <= (time.time()):
            raise serializers.ValidationError("greater than now timestamp")
        return value


class ReasonSLZ(serializers.Serializer):
    reason = serializers.CharField(label="申请理由", max_length=255)


class AggActionListSLZ(BaseAggActionListSLZ):
    expired_at = serializers.IntegerField(label="过期时间", required=True, max_value=PERMANENT_SECONDS)


class ApplicationSLZ(ReasonSLZ):
    """
    申请数据
    """

    system = SystemInfoSLZ(label="系统信息", required=True)
    actions = serializers.ListField(label="操作策略", child=PolicyActionSLZ(label="策略"), required=False, default=list)
    aggregations = serializers.ListField(
        label="聚合操作", child=AggActionListSLZ(label="聚合操作"), required=False, default=list
    )

    def validate(self, data):

        # 检查操作是否重复
        validate_action_repeat(data)

        # 排除已过期的操作
        now = time.time()
        data["actions"] = [ac for ac in data["actions"] if ac["expired_at"] > now]
        data["aggregations"] = [agg for agg in data["aggregations"] if agg["expired_at"] > now]

        if len(data["actions"]) == 0 and len(data["aggregations"]) == 0:
            raise serializers.ValidationError("all actions expired")
        return data


class ApplicationIdSLZ(serializers.Serializer):
    """
    申请单结果
    """

    id = serializers.IntegerField(label="申请单ID")


class ConditionCompareSLZ(serializers.Serializer):
    """
    条件对比
    """

    policy_id = serializers.IntegerField(label="策略ID")
    related_resource_type = ResourceTypeSLZ(label="资源类型")


class ResourceTagSLZ(ResourceSLZ):
    tag = serializers.CharField(label="标签")


class InstanceTagSLZ(serializers.Serializer):
    tag = serializers.CharField(label="标签")
    type = serializers.CharField(label="资源类型", required=True)
    name = serializers.CharField(label="资源类型名称", required=True)
    path = serializers.ListField(
        label="层级链路", child=serializers.ListField(label="链路", child=ResourceTagSLZ(label="节点"))
    )


class ValueTagSLZ(serializers.Serializer):
    tag = serializers.CharField(label="标签")
    id = ValueFiled(label="属性VALUE", required=True)
    name = serializers.CharField(label="属性VALUE名称", required=True)


class AttributeTagSLZ(serializers.Serializer):
    tag = serializers.CharField(label="标签")
    id = serializers.CharField(label="属性KEY", required=True)
    name = serializers.CharField(label="属性KEY名称", required=True)
    values = serializers.ListField(label="属性VALUE", child=ValueTagSLZ(label="值"), required=True, allow_empty=False)


class ConditionTagSLZ(serializers.Serializer):
    tag = serializers.CharField(label="标签")
    id = serializers.CharField(label="条件id", allow_blank=True)
    instances = serializers.ListField(label="拓扑选择", required=True, child=InstanceTagSLZ(label="拓扑实例"))
    attributes = serializers.ListField(label="属性选择", required=True, child=AttributeTagSLZ(label="属性"))


class ApplicationListSLZ(serializers.ModelSerializer):
    extra_info = serializers.SerializerMethodField(
        label="申请单额外信息", help_text="type=1:{'system_name'}, type=2:{'group_count'}, type=3:{'template_count'}"
    )

    class Meta:
        model = Application
        fields = ("id", "sn", "type", "applicant", "status", "created_time", "reason", "extra_info")

    def get_extra_info(self, obj):
        """额外信息：每种申请都可以需要给前端不同信息，便于提示"""
        extra_info = {}
        # 自定义需要返回system_name、system_name_en
        if obj.type in [ApplicationTypeEnum.GRANT_ACTION.value, ApplicationTypeEnum.RENEW_ACTION.value]:
            system = obj.data["system"]
            extra_info["system_name"] = system.get("name")
            extra_info["system_name_en"] = system.get("name_en")
        elif obj.type in [ApplicationTypeEnum.JOIN_GROUP.value, ApplicationTypeEnum.RENEW_GROUP.value]:
            extra_info["group_count"] = len(obj.data["groups"])
        return extra_info


class ApplicationDetailSLZ(serializers.ModelSerializer):
    data = serializers.SerializerMethodField(label="申请单数据")
    organizations = serializers.SerializerMethodField(label="组织")
    ticket_url = serializers.SerializerMethodField(label="单据链接")

    class Meta:
        model = Application
        fields = (
            "id",
            "sn",
            "type",
            "applicant",
            "organizations",
            "status",
            "data",
            "created_time",
            "reason",
            "ticket_url",
        )

    def get_organizations(self, obj):
        # 查询申请人的组织
        try:
            qs = User.objects.get(username=obj.applicant).departments
        except User.DoesNotExist:
            return []
        data = [{"id": i.id, "name": i.name, "full_name": i.full_name} for i in qs]
        return data

    def get_data(self, obj):
        """
        详细申请单信息, 补充过期时间显示
        """
        data = obj.data
        # 对于自定义权限申请
        if obj.type in [ApplicationTypeEnum.GRANT_ACTION.value, ApplicationTypeEnum.RENEW_ACTION.value]:
            # 兼容老数据，老数据只有expired_at，而没有expired_display
            for p in data["actions"]:
                if not p.get("expired_display"):
                    p["expired_display"] = expired_at_display(p["expired_at"], obj.created_timestamp)

        # 对于加入用户组权限申请
        if obj.type == ApplicationTypeEnum.JOIN_GROUP.value:
            # 兼容老数据，老数据只有expired_at，而没有expired_display
            if not data.get("expired_display"):
                data["expired_display"] = expired_at_display(data["expired_at"], obj.created_timestamp)

        # 对于申请创建分级管理员
        if obj.type in [
            ApplicationTypeEnum.CREATE_RATING_MANAGER.value,
            ApplicationTypeEnum.UPDATE_RATING_MANAGER.value,
        ]:
            # 兼容老数据，老数据只有system_id，而不是完整的system
            # 授权范围处理
            auth_scopes = data["authorization_scopes"]
            # 填充system name
            system_dict = {s.id: s for s in SystemBiz().list()}
            for scope in auth_scopes:
                if "system_id" in scope:
                    system_id = scope.pop("system_id")
                    sys = system_dict.get(system_id)
                    scope["system"] = {
                        "id": system_id,
                        "name": sys.name if sys else "",
                        "name_en": sys.name_en if sys else "",
                    }
            # 授权人员范围处理
            subjects = SubjectInfoList(parse_obj_as(List[Subject], data["subject_scopes"])).subjects
            data["subject_scopes"] = [one.dict() for one in subjects]

        return data

    def get_ticket_url(self, obj):
        """获取申请单的审批链接"""
        return ApplicationBiz().get_approval_url(obj)


class ApplicationDetailSchemaSLZ(ApplicationDetailSLZ):
    data = ApplicationSLZ(label="申请")


class ApplicationGroupInfoSLZ(serializers.Serializer):
    id = serializers.IntegerField(label="用户组ID")


class GroupApplicationSLZ(ExpiredAtSLZ, ReasonSLZ):
    groups = serializers.ListField(label="加入的用户组", child=ApplicationGroupInfoSLZ(label="用户组"), allow_empty=False)


class GradeManagerCreatedApplicationSLZ(RatingMangerCreateSLZ, ReasonSLZ):
    pass


class GradeManagerUpdateApplicationSLZ(GradeManagerCreatedApplicationSLZ):
    id = serializers.IntegerField(label="分级管理员ID")


class ApplicationGroupExpiredAtSLZ(ApplicationGroupInfoSLZ, ExpiredAtSLZ):
    pass


class RenewGroupApplicationSLZ(ReasonSLZ):
    groups = serializers.ListField(label="加入的用户组", child=ApplicationGroupExpiredAtSLZ(label="用户组"), allow_empty=False)


class IDExpiredAtSLZ(ExpiredAtSLZ):
    id = serializers.IntegerField(label="ID")


class RenewPolicyApplicationSLZ(ReasonSLZ):
    policies = serializers.ListField(label="策略", child=IDExpiredAtSLZ(label="ID"), allow_empty=False)
