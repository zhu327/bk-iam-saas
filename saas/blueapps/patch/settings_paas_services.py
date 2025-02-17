# coding=utf-8
"""
TencentBlueKing is pleased to support the open source community by making 蓝鲸智云-权限中心(BlueKing-IAM) available.
Copyright (C) 2017-2021 THL A29 Limited, a Tencent company. All rights reserved.
Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
specific language governing permissions and limitations under the License.
"""
import os

from config.default import *  # noqa

# sentry support

SENTRY_DSN = os.environ.get("SENTRY_DSN")
if SENTRY_DSN:
    INSTALLED_APPS += ('raven.contrib.django.raven_compat', )
    MIDDLEWARE += ("raven.contrib.django.raven_compat.middleware.Sentry404CatchMiddleware",)
    RAVEN_CONFIG = {
        'dsn': SENTRY_DSN,
    }

# apm support
APM_ID = os.environ.get("APM_ID")
APM_TOKEN = os.environ.get("APM_TOKEN")
if APM_ID and APM_TOKEN:
    INSTALLED_APPS += ('ddtrace.contrib.django', )
    DATADOG_TRACE = {
        'TAGS': {
            'env': os.getenv('BKPAAS_ENVIRONMENT', 'dev'),
            'apm_id': APM_ID,
            'apm_token': APM_TOKEN,
        },
    }
    # requests for APIGateway/ESB
    # remove pymysql while Django Defaultdb has been traced already
    try:
        import requests  # noqa
        from ddtrace import patch
        patch(requests=True, pymysql=False)
    except Exception as e:  # pylint: disable=broad-except
        print("patch fail for requests and pymysql: %s" % e)
