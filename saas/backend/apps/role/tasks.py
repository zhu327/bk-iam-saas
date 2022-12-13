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
import time
from typing import Set
from urllib.parse import urlencode

from blue_krill.web.std_error import APIError
from celery import Task, current_app
from celery import shared_task as task
from django.conf import settings
from django.template.loader import render_to_string

from backend.apps.group.models import Group
from backend.apps.organization.models import User
from backend.apps.role.models import Role, RoleRelatedObject, RoleUser
from backend.biz.action import ActionBiz
from backend.biz.group import GroupBiz, GroupTemplateGrantBean
from backend.biz.policy import PolicyBean, PolicyBeanList
from backend.biz.resource import ResourceBiz
from backend.biz.role import RoleBiz, RoleCheckBiz, RoleInfoBean
from backend.biz.system import SystemBiz
from backend.common.lock import gen_init_grade_manager_lock
from backend.common.time import DAY_SECONDS, get_soon_expire_ts
from backend.component import esb
from backend.component.cmdb import list_biz
from backend.component.sops import list_project
from backend.service.constants import ADMIN_USER, RoleRelatedObjectType, RoleType
from backend.service.models.policy import ResourceGroupList
from backend.service.models.subject import Subject
from backend.service.role import AuthScopeAction, AuthScopeSystem
from backend.util.url import url_join
from backend.util.uuid import gen_uuid

from .constants import ManagementCommonActionNameEnum, ManagementGroupNameSuffixEnum

logger = logging.getLogger("celery")


@task(ignore_result=True)
def sync_system_manager():
    """
    创建系统管理员
    """
    # 查询后端所有的系统信息
    biz = SystemBiz()
    systems = {system.id: system for system in biz.list()}

    # 查询已创建的系统管理员的系统id
    exists_system_ids = Role.objects.filter(type=RoleType.SYSTEM_MANAGER.value).values_list("code", flat=True)

    # 遍历创建还未创建的系统管理员
    for system_id in set(systems.keys()) - set(exists_system_ids):
        system = systems[system_id]
        logger.info("create system_manager for system_id: %s", system_id)

        # 查询系统管理员配置
        members = biz.list_system_manger(system_id)

        data = {
            "type": RoleType.SYSTEM_MANAGER.value,
            "code": system_id,
            "name": f"{system.name}",
            "name_en": f"{system.name_en}",
            "description": "",
            "members": [{"username": username} for username in members],
            "authorization_scopes": [{"system_id": system_id, "actions": [{"id": "*", "related_resource_types": []}]}],
            "subject_scopes": [{"type": "*", "id": "*"}],
        }
        RoleBiz().create_grade_manager(RoleInfoBean.parse_obj(data), "admin")


class SendRoleGroupExpireRemindMailTask(Task):
    name = "backend.apps.role.tasks.SendRoleGroupExpireRemindMailTask"

    group_biz = GroupBiz()

    base_url = url_join(settings.APP_URL, "/group-perm-renewal")

    def run(self, role_id: int, expired_at: int):
        role = Role.objects.get(id=role_id)
        group_ids = list(
            RoleRelatedObject.objects.filter(
                role_id=role.id, object_type=RoleRelatedObjectType.GROUP.value
            ).values_list("object_id", flat=True)
        )
        if not group_ids:
            return

        exist_group_ids = self.group_biz.list_exist_groups_before_expired_at(group_ids, expired_at)
        if not exist_group_ids:
            return

        groups = Group.objects.filter(id__in=exist_group_ids)

        params = {"source": "email", "current_role_id": role.id, "role_type": role.type}
        url = self.base_url + "?" + urlencode(params)

        mail_content = render_to_string(
            "group_expired_mail.html", {"groups": groups, "role": role, "url": url, "index_url": settings.APP_URL}
        )

        usernames = RoleUser.objects.filter(role_id=role.id).values_list("username", flat=True)
        try:
            esb.send_mail(",".join(usernames), "蓝鲸权限中心用户组续期提醒", mail_content)
        except Exception:  # pylint: disable=broad-except
            logger.exception("send role_group_expire_remind email fail, usernames=%s", usernames)


current_app.tasks.register(SendRoleGroupExpireRemindMailTask())


@task(ignore_result=True)
def role_group_expire_remind():
    """
    角色管理的用户组过期提醒
    """
    group_biz = GroupBiz()
    expired_at = get_soon_expire_ts()

    group_id_set, role_id_set = set(), set()  # 去重用

    # 查询有过期成员的用户组关系
    group_subjects = group_biz.list_group_subject_before_expired_at(expired_at)
    for gs in group_subjects:
        group_id = gs.group.id
        if group_id in group_id_set:
            continue

        group_id_set.add(group_id)

        # 查询用户组对应的分级管理员
        relation = RoleRelatedObject.objects.filter(
            object_type=RoleRelatedObjectType.GROUP.value, object_id=int(group_id)
        ).first()

        if not relation:
            continue

        role_id = relation.role_id
        if role_id in role_id_set:
            continue

        role_id_set.add(role_id)
        SendRoleGroupExpireRemindMailTask().delay(role_id, expired_at)


class InitBizGradeManagerTask(Task):
    name = "backend.apps.role.tasks.InitBizGradeManagerTask"

    biz = RoleBiz()
    role_check_biz = RoleCheckBiz()
    group_biz = GroupBiz()
    action_biz = ActionBiz()
    resource_biz = ResourceBiz()

    _exist_names: Set[str] = set()

    def run(self):
        if not settings.ENABLE_INIT_GRADE_MANAGER:
            return

        with gen_init_grade_manager_lock():
            biz_info = list_biz()
            biz_dict = {one["bk_biz_id"]: one for one in biz_info["info"]}

            projects = list_project()
            for project in projects:
                if project["bk_biz_id"] in biz_dict:
                    biz = biz_dict[project["bk_biz_id"]]

                    maintainers = (biz.get("bk_biz_maintainer") or "").split(",")  # 业务的负责人
                    viewers = list(
                        set(
                            (biz.get("bk_biz_developer") or "").split(",")
                            + (biz.get("bk_biz_productor") or "").split(",")
                            + (biz.get("bk_biz_tester") or "").split(",")
                        )
                    )  # 业务的查看人

                    self._create_grade_manager(project, maintainers, viewers)
                else:
                    logger.debug(
                        "init grade manager: bk_sops project [%s] biz_id [%d] not exists in bk_cmdb",
                        project["name"],
                        project["bk_biz_id"],
                    )

    def _create_grade_manager(self, project, maintainers, viewers):
        biz_name = project["name"]
        if biz_name in self._exist_names:
            return

        try:
            self.role_check_biz.check_grade_manager_unique_name(biz_name)
        except APIError:
            # 缓存结果
            self._exist_names.add(biz_name)
            return

        role_info = self._init_role_info(project, maintainers)

        role = self.biz.create_grade_manager(role_info, ADMIN_USER)

        # 创建用户组并授权
        expired_at = int(time.time()) + 6 * 30 * DAY_SECONDS  # 过期时间半年

        authorization_scopes = role_info.dict()["authorization_scopes"]
        for name_suffix in [ManagementGroupNameSuffixEnum.OPS.value, ManagementGroupNameSuffixEnum.READ.value]:
            description = "{}业务运维人员的权限".format(biz_name)
            if name_suffix == ManagementGroupNameSuffixEnum.READ.value:
                description = "仅包含{}各系统的查看权限".format(biz_name)

            members = maintainers if name_suffix == ManagementGroupNameSuffixEnum.OPS.value else viewers
            users = User.objects.filter(username__in=members)  # 筛选出已同步存在的用户
            group = self.group_biz.create_and_add_members(
                role.id,
                biz_name + name_suffix,
                description=description,
                creator=ADMIN_USER,
                subjects=[Subject.from_username(u.username) for u in users],
                expired_at=expired_at,  # 过期时间半年
            )

            templates = self._init_group_auth_info(authorization_scopes, name_suffix)
            self.group_biz.grant(role, group, templates)

        self._exist_names.add(biz_name)

    def _init_role_info(self, data, maintainers):
        """
        创建初始化分级管理员数据

        1. 遍历各个需要初始化的系统
        2. 查询系统的常用操作与系统的操作信息, 拼装出授权范围
        3. 返回role info
        """
        role_info = RoleInfoBean(
            name=data["name"],
            description="管理员可授予他人{}业务的权限".format(data["name"]),
            members=maintainers or [ADMIN_USER],
            subject_scopes=[Subject(type="*", id="*")],
            authorization_scopes=[],
        )

        # 默认需要初始化的系统列表
        systems = settings.INIT_GRADE_MANAGER_SYSTEM_LIST
        bk_sops_system = "bk_sops"
        bk_cmdb_system = "bk_cmdb"
        for system_id in systems:
            resource_type = "biz"
            if system_id == bk_sops_system:
                resource_type = "project"
            # NOTE: 日志平台, 监控平台迁移完成后再处理
            # elif system_id in ["bk_log_search", "bk_monitorv3"]:
            #     resource_type = "space"

            resource_system = bk_cmdb_system
            if system_id == bk_sops_system:
                resource_system = system_id
            # if system_id in [bk_sops_system, "bk_log_search", "bk_monitorv3"]:
            #     resource_system = system_id

            resource_id = data["bk_biz_id"] if system_id != bk_sops_system else data["project_id"]
            resource_name = data["name"]
            # if system_id in ["bk_log_search", "bk_monitorv3"]:
            #     resource_name = "[业务] " + resource_name

            auth_scope = AuthScopeSystem(system_id=system_id, actions=[])

            # 1. 查询常用操作
            common_action = self.biz.get_common_action_by_name(system_id, ManagementCommonActionNameEnum.OPS.value)
            if not common_action:
                logger.debug(
                    "init grade manager: system [%s] is not configured common action [%s]",
                    system_id,
                    ManagementCommonActionNameEnum.OPS.value,
                )
                continue

            # 2. 查询操作信息
            action_list = self.action_biz.list(system_id)

            # 3. 生成授权范围
            for action_id in common_action.action_ids:
                action = action_list.get(action_id)
                if not action:
                    logger.debug(
                        "init grade manager: system [%s] action [%s] not exists in common action [%s]",
                        system_id,
                        action_id,
                        ManagementCommonActionNameEnum.OPS.value,
                    )
                    continue

                # 不关联资源类型的操作
                if len(action.related_resource_types) == 0:
                    auth_scope_action = AuthScopeAction(id=action.id, resource_groups=ResourceGroupList(__root__=[]))

                elif system_id == bk_cmdb_system and action_id == "unassign_biz_host":
                    # 配置管理 -- 主机归还主机池 主机池默认为空闲机
                    policy_data = self._gen_cmdb_unassign_biz_host_policy(
                        action, resource_type, resource_system, resource_id, resource_name
                    )
                    auth_scope_action = AuthScopeAction.parse_obj(policy_data)

                elif system_id == "bk_nodeman" and action_id == "cloud_view":
                    # 节点管理 -- 云区域查看 默认为任意权限
                    policy_data = self._gen_nodeman_cloud_view_policy(action)
                    auth_scope_action = AuthScopeAction.parse_obj(policy_data)

                else:
                    policy_data = self._action_policy(
                        action, resource_type, resource_system, resource_id, resource_name
                    )
                    auth_scope_action = AuthScopeAction.parse_obj(policy_data)

                auth_scope.actions.append(auth_scope_action)

            # 4. 组合授权范围
            if auth_scope.actions:
                role_info.authorization_scopes.append(auth_scope)

        return role_info

    def _action_policy(self, action, resource_type, resource_system, resource_id, resource_name):
        return {
            "id": action.id,
            "resource_groups": [
                {
                    "related_resource_types": [
                        {
                            "system_id": rrt.system_id,
                            "type": rrt.id,
                            "condition": [
                                {
                                    "id": gen_uuid(),
                                    "instances": [
                                        {
                                            "type": resource_type,
                                            "path": [
                                                [
                                                    {
                                                        "id": resource_id,
                                                        "name": resource_name,
                                                        "system_id": resource_system,
                                                        "type": resource_type,
                                                    }
                                                ]
                                            ],
                                        }
                                    ],
                                    "attributes": [],
                                }
                            ],
                        }
                        for rrt in action.related_resource_types
                    ]
                }
            ],
        }

    def _gen_nodeman_cloud_view_policy(self, action):
        return {
            "id": action.id,
            "resource_groups": [
                {
                    "related_resource_types": [
                        {
                            "system_id": rrt.system_id,
                            "type": rrt.id,
                            "condition": [],
                        }
                        for rrt in action.related_resource_types
                    ]
                }
            ],
        }

    def _gen_cmdb_unassign_biz_host_policy(self, action, resource_type, resource_system, resource_id, resource_name):
        return {
            "id": action.id,
            "resource_groups": [
                {
                    "related_resource_types": [
                        {
                            "system_id": resource_system,
                            "type": resource_type,
                            "condition": [
                                {
                                    "id": gen_uuid(),
                                    "instances": [
                                        {
                                            "type": resource_type,
                                            "path": [
                                                [
                                                    {
                                                        "id": resource_id,
                                                        "name": resource_name,
                                                        "system_id": resource_system,
                                                        "type": resource_type,
                                                    }
                                                ]
                                            ],
                                        }
                                    ],
                                    "attributes": [],
                                }
                            ],
                        },
                        {
                            "system_id": resource_system,
                            "type": "sys_resource_pool_directory",
                            "condition": [
                                {
                                    "id": gen_uuid(),
                                    "instances": [
                                        {
                                            "type": "sys_resource_pool_directory",
                                            "path": [
                                                [
                                                    {
                                                        "id": self._query_cmdb_sys_resource_pool_directory_id("空闲机"),
                                                        "name": "空闲机",
                                                        "system_id": resource_system,
                                                        "type": "sys_resource_pool_directory",
                                                    }
                                                ]
                                            ],
                                        }
                                    ],
                                    "attributes": [],
                                }
                            ],
                        },
                    ]
                }
            ],
        }

    def _query_cmdb_sys_resource_pool_directory_id(self, name: str) -> str:
        # 查询cmdb主机池id
        _, resources = self.resource_biz.search_instance_for_topology("bk_cmdb", "sys_resource_pool_directory", name)
        for r in resources:
            if r.display_name == name:
                return r.id

        return "*"  # NOTE: 不应该出现的场景

    def _init_group_auth_info(self, authorization_scopes, name_suffix: str):
        templates = []
        for auth_scope in authorization_scopes:
            system_id = auth_scope["system_id"]
            actions = auth_scope["actions"]
            if name_suffix == ManagementGroupNameSuffixEnum.READ.value:
                common_action = self.biz.get_common_action_by_name(
                    system_id, ManagementCommonActionNameEnum.READ.value
                )
                if not common_action:
                    logger.debug(
                        "init grade manager: system [%s] is not configured common action [%s]",
                        system_id,
                        ManagementCommonActionNameEnum.READ.value,
                    )
                    continue

                actions = [a for a in actions if a["id"] in common_action.action_ids]

            policies = [PolicyBean.parse_obj(action) for action in actions]
            policy_list = PolicyBeanList(
                system_id=system_id,
                policies=policies,
                need_fill_empty_fields=True,  # 填充相关字段
            )

            template = GroupTemplateGrantBean(
                system_id=system_id,
                template_id=0,  # 自定义权限template_id为0
                policies=policy_list.policies,
            )

            templates.append(template)

        return templates
