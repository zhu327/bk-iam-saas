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
from abc import ABC, abstractmethod
from typing import Dict, Optional, Set, Tuple, Type
from urllib.parse import urlencode

from blue_krill.web.std_error import APIError
from celery import Task, current_app, shared_task
from django.conf import settings
from django.template.loader import render_to_string
from pydantic.main import BaseModel

from backend.apps.group.models import Group
from backend.apps.organization.models import User
from backend.apps.role.models import Role, RoleRelatedObject, RoleUser
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
from backend.service.action import ActionService
from backend.service.constants import ADMIN_USER, RoleRelatedObjectType, RoleType
from backend.service.models import Action, PathResourceType
from backend.service.models.policy import ResourceGroupList
from backend.service.models.subject import Subject
from backend.service.role import AuthScopeAction, AuthScopeSystem, RoleMember
from backend.util.url import url_join
from backend.util.uuid import gen_uuid

from .constants import ManagementCommonActionNameEnum, ManagementGroupNameSuffixEnum

logger = logging.getLogger("celery")


@shared_task(ignore_result=True)
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


@shared_task(ignore_result=True)
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


class ResourceInstance(BaseModel):
    system_id: str
    type: str
    id: str
    name: str


class BaseAuthScopeActionHandler(ABC):
    @abstractmethod
    def handle(self, system_id: str, action: Action, instance: ResourceInstance) -> Optional[AuthScopeAction]:
        pass


class DefaultAuthScopeActionHandler(BaseAuthScopeActionHandler):
    def handle(self, system_id: str, action: Action, instance: ResourceInstance) -> Optional[AuthScopeAction]:
        # 校验实例视图, 如果校验不过, 需要跳过, 避免错误数据
        for rrt in action.related_resource_types:
            for selection in rrt.instance_selections:
                if selection.match_path([PathResourceType(system_id=instance.system_id, id=instance.type)]):
                    break
            else:
                return None

        return AuthScopeAction.parse_obj(
            {
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
                                                "type": instance.type,
                                                "path": [
                                                    [
                                                        {
                                                            "id": instance.id,
                                                            "name": instance.name,
                                                            "system_id": instance.system_id,
                                                            "type": instance.type,
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
        )


class AnyAuthScopeActionHandler(BaseAuthScopeActionHandler):
    def handle(self, system_id: str, action: Action, instance: ResourceInstance) -> Optional[AuthScopeAction]:
        return AuthScopeAction.parse_obj(
            {
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
        )


class CmdbUnassignBizHostAuthScopeActionHandler(BaseAuthScopeActionHandler):

    resource_biz = ResourceBiz()

    def handle(self, system_id: str, action: Action, instance: ResourceInstance) -> Optional[AuthScopeAction]:
        return AuthScopeAction.parse_obj(
            {
                "id": action.id,
                "resource_groups": [
                    {
                        "related_resource_types": [
                            {
                                "system_id": instance.system_id,
                                "type": instance.type,
                                "condition": [
                                    {
                                        "id": gen_uuid(),
                                        "instances": [
                                            {
                                                "type": instance.type,
                                                "path": [
                                                    [
                                                        {
                                                            "id": instance.id,
                                                            "name": instance.name,
                                                            "system_id": instance.system_id,
                                                            "type": instance.type,
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
                                "system_id": instance.system_id,
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
                                                            "id": self._query_cmdb_sys_resource_pool_directory_id(
                                                                "空闲机"
                                                            ),
                                                            "name": "空闲机",
                                                            "system_id": instance.system_id,
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
        )

    def _query_cmdb_sys_resource_pool_directory_id(self, name: str) -> str:
        # 查询cmdb主机池id
        _, resources = self.resource_biz.search_instance_for_topology("bk_cmdb", "sys_resource_pool_directory", name)
        for r in resources:
            if r.display_name == name:
                return r.id

        return "*"  # NOTE: 不应该出现的场景


class LogSpaceAuthScopeActionHandler(DefaultAuthScopeActionHandler):
    def handle(self, system_id: str, action: Action, instance: ResourceInstance) -> Optional[AuthScopeAction]:
        auth_scope_action = super().handle(system_id, action, instance)
        if auth_scope_action:
            return auth_scope_action

        space_instance = ResourceInstance(
            system_id="bk_monitorv3", type="space", id=instance.id, name="[业务] " + instance.name
        )
        return super().handle(system_id, action, space_instance)


class JobExecutePublicScriptAuthScopeActionHandler(BaseAuthScopeActionHandler):
    def handle(self, system_id: str, action: Action, instance: ResourceInstance) -> Optional[AuthScopeAction]:
        return AuthScopeAction.parse_obj(
            {
                "id": action.id,
                "resource_groups": [
                    {
                        "related_resource_types": [
                            {
                                "system_id": "bk_job",
                                "type": "public_script",
                                "condition": [],
                            },
                            {
                                "system_id": instance.system_id,
                                "type": "host",
                                "condition": [
                                    {
                                        "id": gen_uuid(),
                                        "instances": [
                                            {
                                                "type": instance.type,
                                                "path": [
                                                    [
                                                        {
                                                            "id": instance.id,
                                                            "name": instance.name,
                                                            "system_id": instance.system_id,
                                                            "type": instance.type,
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
        )


class SopsCommonFlowCreateTaskAuthScopeActionHandler(BaseAuthScopeActionHandler):
    def handle(self, system_id: str, action: Action, instance: ResourceInstance) -> Optional[AuthScopeAction]:
        return AuthScopeAction.parse_obj(
            {
                "id": action.id,
                "resource_groups": [
                    {
                        "related_resource_types": [
                            {
                                "system_id": instance.system_id,
                                "type": "common_flow",
                                "condition": [],
                            },
                            {
                                "system_id": instance.system_id,
                                "type": instance.type,
                                "condition": [
                                    {
                                        "id": gen_uuid(),
                                        "instances": [
                                            {
                                                "type": instance.type,
                                                "path": [
                                                    [
                                                        {
                                                            "id": instance.id,
                                                            "name": instance.name,
                                                            "system_id": instance.system_id,
                                                            "type": instance.type,
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
        )


class ActionWithoutResourceAuthScopeActionHandler(BaseAuthScopeActionHandler):
    def handle(self, system_id: str, action: Action, instance: ResourceInstance) -> Optional[AuthScopeAction]:
        return AuthScopeAction(id=action.id, resource_groups=ResourceGroupList(__root__=[]))


class AuthScopeActionGenerator:

    handler_map: Dict[Tuple[str, str], Type[BaseAuthScopeActionHandler]] = {
        ("bk_nodeman", "cloud_view"): AnyAuthScopeActionHandler,
        ("bk_cmdb", "unassign_biz_host"): CmdbUnassignBizHostAuthScopeActionHandler,
        ("bk_sops", "common_flow_view"): AnyAuthScopeActionHandler,
        ("bk_sops", "common_flow_create_task"): SopsCommonFlowCreateTaskAuthScopeActionHandler,
        ("bk_job", "execute_public_script"): JobExecutePublicScriptAuthScopeActionHandler,
    }

    def __init__(self, system_id: str, action: Action, instance: ResourceInstance) -> None:
        self._system_id = system_id
        self._action = action
        self._instance = instance

    def generate(self) -> Optional[AuthScopeAction]:
        handler = self._get_handler()
        return handler.handle(self._system_id, self._action, self._instance)

    def _get_handler(self) -> BaseAuthScopeActionHandler:
        if len(self._action.related_resource_types) == 0:
            return ActionWithoutResourceAuthScopeActionHandler()
        elif self._system_id in ["bk_log_search", "bk_monitorv3"]:
            return LogSpaceAuthScopeActionHandler()

        return self.handler_map.get((self._system_id, self._action.id), DefaultAuthScopeActionHandler)()


class InitBizGradeManagerTask(Task):
    name = "backend.apps.role.tasks.InitBizGradeManagerTask"

    biz = RoleBiz()
    role_check_biz = RoleCheckBiz()
    group_biz = GroupBiz()
    action_svc = ActionService()

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
                role,
                biz_name + name_suffix,
                description=description,
                creator=ADMIN_USER,
                subjects=[Subject.from_username(u.username) for u in users],
                expired_at=expired_at,  # 过期时间半年
            )

            templates = self._init_group_auth_info(authorization_scopes, name_suffix)
            self.group_biz.grant(role, group, templates, need_check=False)

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
            members=[RoleMember(username=username) for username in maintainers or [ADMIN_USER]],
            subject_scopes=[Subject(type="*", id="*")],
            authorization_scopes=[],
        )

        # 默认需要初始化的系统列表
        systems = settings.INIT_GRADE_MANAGER_SYSTEM_LIST
        bk_sops_system = "bk_sops"
        bk_cmdb_system = "bk_cmdb"
        for system_id in systems:
            if system_id == bk_sops_system:
                instance = ResourceInstance(
                    system_id=bk_sops_system, type="project", id=data["project_id"], name=data["name"]
                )
            else:
                instance = ResourceInstance(
                    system_id=bk_cmdb_system, type="biz", id=data["bk_biz_id"], name=data["name"]
                )

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
            action_list = self.action_svc.new_action_list(system_id)

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

                # 分发者模式
                auth_scope_action = AuthScopeActionGenerator(system_id, action, instance).generate()

                if auth_scope_action:
                    auth_scope.actions.append(auth_scope_action)

            # 4. 组合授权范围
            if auth_scope.actions:
                role_info.authorization_scopes.append(auth_scope)

        return role_info

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


current_app.tasks.register(InitBizGradeManagerTask())


from backend.apps.group.tasks import GroupAuthorizationTask
from backend.biz.role import RoleListQuery

fix_system_id = "bk_log_search"

biz = RoleBiz()
group_biz = GroupBiz()
gt = InitBizGradeManagerTask()

projects = list_project()
for project in projects:
    role = Role.objects.filter(
        name=project["name"],
        type=RoleType.GRADE_MANAGER.value,
    ).first()
    if not role:
        continue
    biz = RoleBiz()
    auth_scopes = biz.list_auth_scope(role.id)
    need_fix = True
    for auth_scope in auth_scopes:
        if auth_scope.system_id == fix_system_id:
            need_fix = False
            break
    if not need_fix:
        continue
    print("fix role:", role.name)
    new_role_info = gt._init_role_info(project, [])
    biz.svc.update_role_auth_scope(role.id, new_role_info.authorization_scopes)
    # 用户组授权
    for name_suffix in [ManagementGroupNameSuffixEnum.OPS.value, ManagementGroupNameSuffixEnum.READ.value]:
        group_name = project["name"] + name_suffix
        group = RoleListQuery(role, None).query_group().filter(name=group_name).first()
        if not group:
            continue
        print("fix group:", group.name)
        templates = gt._init_group_auth_info(new_role_info.dict()["authorization_scopes"], name_suffix)
        for template in templates:
            if template.system_id == fix_system_id:
                group_biz.grant(role, group, [template], need_check=False)
                break
