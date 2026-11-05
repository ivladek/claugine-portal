from django.contrib import admin

from .models import Observation

admin.site.register(Observation, list_display=("group_name", "platform_key", "group_id", "collected_at"))
