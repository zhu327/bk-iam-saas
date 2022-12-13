#!/bin/bash
celery -A celery_app worker -l INFO
