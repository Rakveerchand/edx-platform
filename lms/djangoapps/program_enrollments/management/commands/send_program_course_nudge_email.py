"""
Django management command for sending nudge emails to learners after they complete once course in a program, to suggest
to complete possible next course from same program.
"""

import logging
from collections import defaultdict
from datetime import timedelta
from operator import itemgetter
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.core.management import BaseCommand
from django.utils import timezone

from common.djangoapps.track import segment
from lms.djangoapps.grades.models import PersistentCourseGrade
from openedx.core.constants import COURSE_PUBLISHED
from openedx.core.djangoapps.catalog.utils import get_programs
from openedx.core.djangoapps.programs.utils import ProgramProgressMeter
from openedx.features.enterprise_support.api import get_enterprise_learner_data_from_db

User = get_user_model()

LOGGER = logging.getLogger(__name__)


class Command(BaseCommand):
    """
    Django management command for sending nudge emails to learners

    This command sends nudge emails to learners after they complete once course in a program, to suggest to complete
    possible next course from same program.

    Example usage:
           $ ./manage.py lms send_program_course_nudge_email
           $ ./manage.py lms send_program_course_nudge_email  --no-commit
    """

    def get_passed_course_to_users_maps(self):
        """
        Returns mapping between course passed yesterday with passing users.
        """
        passed_course_to_users_maps = defaultdict(list)
        yesterday = timezone.now().date() - timedelta(days=1)
        passed_grades = PersistentCourseGrade.objects.filter(
            passed_timestamp__date=yesterday,
        )

        passing_user_ids = list(passed_grades.values_list('user_id', flat=True).distinct())
        passing_users = User.objects.filter(id__in=passing_user_ids)
        user_id_to_user_map = {user.id: user for user in passing_users}

        for passing_grade in passed_grades:
            user = user_id_to_user_map[passing_grade.user_id]
            passed_course_to_users_maps[str(passing_grade.course_id)].append(user)

        LOGGER.info(
            '[Program Course Nudge Email] Found [%s] passing grades on [%s] date with [%s] distinct users '
            'and [%s] distinct courses',
            passed_grades.count(),
            yesterday,
            len(passing_user_ids),
            len(passed_course_to_users_maps.keys()),
        )

        return passed_course_to_users_maps

    def valid_course_run(self, course_run):
        """
        Check if a course run is in enrollable state.
        """
        return course_run['is_enrollable'] \
            and course_run['is_marketable'] \
            and course_run['marketing_url'] \
            and course_run['image'] \
            and course_run['status'] == COURSE_PUBLISHED

    def get_course_run_to_suggest(self, programs_progress, completed_course_id):
        """
        Finds out enrollable course run from programs Generated by ProgramProgressMeter.

        Returns: Suggested program and course_run dicts
        """
        for program in programs_progress:
            for not_started_course in program['not_started']:
                for course_run in not_started_course['course_runs']:
                    if self.valid_course_run(course_run) and course_run['key'] != completed_course_id:
                        return program, course_run, not_started_course
        return None, None

    def sort_programs(self, programs):
        """
        Sorts programs based on their revenue ranking.
        """
        sort_revenue_order = {
            'MicroMasters': 1,
            'Professional Program': 2,
            'Professional Certificate': 3,
            'XSeries': 4,
            'Masters': 5,
            'MicroBachelors': 6,
        }
        for program in programs:
            program['sort_revenue_order'] = sort_revenue_order.get(program['type'], 7)

        return sorted(programs, key=itemgetter('sort_revenue_order'))

    def get_course_run(self, program, course_run_id):
        """
        get course run from a program.
        """
        for course in program['courses']:
            for course_run in course['course_runs']:
                if course_run['key'] == course_run_id:
                    return course_run

    def get_program(self, programs, program_progress):
        """
        get detailed program.
        """
        for program in programs:
            if program['uuid'] == program_progress['uuid']:
                return program

    def emit_event(self, user, program, suggested_course_run, suggested_course, completed_course_run):
        """
         Emit the Segment event which will be used by Braze to send the email
        """
        learner_data = get_enterprise_learner_data_from_db(user)
        enterprise_customer = learner_data[0]['enterprise_customer'] if learner_data else None
        if enterprise_customer and enterprise_customer['enable_learner_portal']:
            # If user is an enterprise learner then we want to redirect him to B2B course landing on learner portal.
            recommended_course_url = urljoin(
                settings.ENTERPRISE_LEARNER_PORTAL_BASE_URL,
                '/'.join([enterprise_customer['slug'], 'course', suggested_course['key']]),
            )
        else:
            recommended_course_url = urljoin(settings.MKTG_URLS.get('ROOT'), suggested_course_run['marketing_url'])

        event_properties = {
            'COURSE_ONE_NAME': completed_course_run['title'],
            'PROGRAM_TYPE': program['type'],
            'PROGRAM_TITLE': program['title'],
            'COURSE_TWO_NAME': suggested_course_run['title'],
            'COURSE_TWO_SHORT_DESCRIPTION': suggested_course_run['short_description'],
            'COURSE_TWO_LINK': recommended_course_url,
            'COURSE_TWO_IMAGE_LINK': suggested_course_run['image'].get('src'),
        }
        segment.track(user.id, 'edx.bi.program.course-enrollment.nudge', event_properties)

        LOGGER.info(
            '[Program Course Nudge Email] Segment event fired to suggested. '
            'Completed Course: [%s], Program: [%s], Suggested Course: [%s], User: [%s].',
            completed_course_run['key'],
            program['uuid'],
            suggested_course_run['key'],
            user.username,
        )

    def add_arguments(self, parser):
        """
        Entry point to add arguments.
        """
        parser.add_argument(
            '--no-commit',
            action='store_true',
            dest='no_commit',
            default=False,
            help='Dry Run, print log messages without committing anything.',
        )

    def handle(self, *args, **options):
        """
        Command's entry point.
        """
        should_commit = not options['no_commit']

        email_sent_records = []
        site = Site.objects.get_current()
        course_to_users_maps = self.get_passed_course_to_users_maps()

        for completed_course_id, users in course_to_users_maps.items():
            course_linked_programs = get_programs(course=completed_course_id)
            course_linked_programs = self.sort_programs(course_linked_programs)
            if course_linked_programs:
                for user in users:
                    meter = ProgramProgressMeter(site=site, user=user, include_course_entitlements=False)
                    programs_progress = meter.progress(programs=course_linked_programs, count_only=False)
                    suggested_program_progress, suggested_course_run, suggested_course = self.get_course_run_to_suggest(
                        programs_progress, completed_course_id
                    )
                    if suggested_course_run and suggested_course:
                        suggested_program = self.get_program(course_linked_programs, suggested_program_progress)
                        completed_course_run = self.get_course_run(suggested_program, completed_course_id)
                        if should_commit:
                            self.emit_event(
                                user, suggested_program, suggested_course_run, suggested_course, completed_course_run,
                            )
                        email_sent_records.append(
                            f'User: {user.username}, Completed Course: {completed_course_id}, '
                            f'Suggested Course: {suggested_course_run["key"]}'
                        )

        LOGGER.info(
            '[Program Course Nudge Email] %s Emails sent. Records: %s',
            len(email_sent_records),
            email_sent_records,
        )
