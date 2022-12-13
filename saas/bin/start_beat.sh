#!/bin/bash
celery -A celery_app beat -l info
