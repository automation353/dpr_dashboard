"""Run one background Excel export job (launched by dashboard.exporter.start_job)."""
from django.core.management.base import BaseCommand

from dashboard.exporter import run_job


class Command(BaseCommand):
    help = 'Write the All Details Excel file for an export job id.'

    def add_arguments(self, parser):
        parser.add_argument('job_id')

    def handle(self, *args, **opts):
        run_job(opts['job_id'])
