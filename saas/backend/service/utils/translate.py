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
from collections import defaultdict
from typing import Any, Dict, List

from backend.common.error_codes import error_codes
from backend.service.constants import ANY_ID
from backend.util.json import json_dumps


class ResourceExpressionTranslator:
    """
    翻译资源条件到后端表达式
    """

    def translate(self, resources: List[Dict]) -> str:
        """
        resources: [
          {
            "system_id": "string",
            "type": "string",
            "name": "string",
            "condition": [
              {
                "id": "string",
                "instances": [
                  {
                    "type": "string",
                    "name": "string",
                    "path": [
                      [
                        {
                          "type": "string",
                          "type_name": "string",
                          "id": "string",
                          "name": "string"
                        }
                      ]
                    ]
                  }
                ],
                "attributes": [
                  {
                    "id": "string",
                    "name": "string",
                    "values": [
                      {
                        "id": "string",
                        "name": "string"
                      }
                    ]
                  }
                ]
              }
            ]
          }
        ]
        """
        expression = [
            {"system": r["system_id"], "type": r["type"], "expression": self._translate_condition(r)}
            for r in resources
        ]

        return json_dumps(expression)  # 去掉json自动生成的空格

    def _translate_condition(self, resource: Dict) -> Dict:
        """
        表达式转换, 转换SaaS的条件为后端的表达式
        """

        # 条件为空, 表示任意
        if len(resource["condition"]) == 0:
            return {"Any": {"id": []}}

        content = []

        for c in resource["condition"]:  # 多个项之间是OR
            # 转换实例选择, 每个path中的链路之间是OR
            instance_content = []
            for i in c["instances"]:
                instance_content.append(self._translate_instance(resource["type"], i))

            if len(instance_content) == 0:
                instance = {}
            elif len(instance_content) == 1:
                instance = instance_content[0]
            else:
                instance = {"OR": {"content": instance_content}}

            # 转换属性选择, 每个属性之间是AND
            attribute_content = []
            for a in c["attributes"]:
                attribute_content.append(self._translate_attribute(a))

            if len(attribute_content) == 0:
                attribute = {}
            elif len(attribute_content) == 1:
                attribute = attribute_content[0]
            else:
                attribute = {"AND": {"content": attribute_content}}

            # instance 与 attribute 之间 AND
            if instance and attribute:
                content.append({"AND": {"content": [instance, attribute]}})
                continue

            if instance and not attribute:
                content.append(instance)
                continue

            if not instance and attribute:
                content.append(attribute)
                continue

            raise error_codes.INVALID_ARGS.format("instance and attribute must not be both empty")

        if len(content) == 1:
            return content[0]

        # 多组condition之间是OR
        return {"OR": {"content": content}}

    def _translate_attribute(self, attribute: Dict) -> Dict:
        """
        转换单个attribute
        """
        values = [one["id"] for one in attribute["values"]]

        if len(values) == 0:
            raise error_codes.INVALID_ARGS.format("values must not empty")

        if isinstance(values[0], bool):
            # bool属性值只能有一个
            if len(values) != 1:
                raise error_codes.INVALID_ARGS.format("bool value must has one")
            return {"Bool": {attribute["id"]: values}}

        if isinstance(values[0], (int, float)):
            return {"NumericEquals": {attribute["id"]: values}}

        if isinstance(values[0], str):
            return {"StringEquals": {attribute["id"]: values}}

        raise error_codes.INVALID_ARGS.format("values only support (bool, int, float, str)")

    def _translate_instance(self, _type: str, instance: Dict) -> Dict[str, Any]:
        """
        转换单个instance
        """
        content: List[Dict[str, Any]] = []

        ids = []  # 合并只有id的条件
        paths = []  # 合并最后一级为*的path
        path_ids = defaultdict(list)  # 合并path相同的id

        for p in instance["path"]:
            # 最后一个节点是叶子节点
            if p[-1]["type"] == _type:
                # 如果路径上只有一个节点, 且为叶子节点, 直接使用StringEquals
                if len(p) == 1:
                    ids.append(p[0]["id"])
                else:
                    path = translate_path(p[:-1])

                    # 如果叶子节点是任意, 只是用路径StringPrefix
                    if p[-1]["id"] == ANY_ID:
                        paths.append(path)
                        continue

                    # 具有相同路径前缀的叶子节点, 聚合到一个AND的条件中
                    path_ids[path].append(p[-1]["id"])
            else:
                paths.append(translate_path(p))

        if ids:
            content.append({"StringEquals": {"id": ids}})

        if paths:
            content.append({"StringPrefix": {"_bk_iam_path_": paths}})

        for path, ids in path_ids.items():
            content.append(
                {"AND": {"content": [{"StringEquals": {"id": ids}}, {"StringPrefix": {"_bk_iam_path_": [path]}}]}}
            )

        if len(content) == 0:
            raise error_codes.INVALID_ARGS.format("instance path must not be empty")

        if len(content) == 1:
            return content[0]

        return {"OR": {"content": content}}


def translate_path(path_nodes: List[Dict]) -> str:
    """
    转换path层级到字符串表示
    """
    path = ["/"]
    for n in path_nodes:
        path.append("{},{}/".format(n["type"], n["id"]))
    return "".join(path)
