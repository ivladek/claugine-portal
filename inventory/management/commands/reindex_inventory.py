from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Old name of load_inventory --once (the portal's records are the source after the first load)."

    def handle(self, *args, **opts):
        call_command("load_inventory", once=True)
