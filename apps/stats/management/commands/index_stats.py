import logging
from datetime import date, timedelta
from optparse import make_option

from django.core.management.base import BaseCommand
from django.db.models import Max, Min

from celery.task.sets import TaskSet

from amo.utils import chunked
from stats.models import UpdateCount, DownloadCount
from stats.tasks import index_update_counts, index_download_counts

log = logging.getLogger('z.stats')

# Number of days of stats to process in one chunk if we're indexing everything.
STEP = 5
HELP = """\
Start tasks to index stats. Without constraints, everything will be
processed.


To limit the add-ons:

    `--addons=1865,2848,..,1843`

To limit the  date range:

    `--date=2011-08-15` or `--date=2011-08-15:2011-08-22`
"""


class Command(BaseCommand):
    option_list = BaseCommand.option_list + (
        make_option('--addons',
                    help='Add-on ids to process. Use commas to separate '
                         'multiple ids.'),
        make_option('--date',
                    help='The date or date range to process. Use the format '
                         'YYYY-MM-DD for a single date or '
                         'YYYY-MM-DD:YYYY-MM-DD to index a range of dates '
                         '(inclusive).'),
        make_option('--fixup', action='store_true',
                    help='Find and index rows we missed.'),
    )
    help = HELP

    def handle(self, *args, **kw):
        if kw.get('fixup'):
            fixup()

        addons, dates = kw['addons'], kw['date']

        queries = [(UpdateCount.objects, index_update_counts),
                   (DownloadCount.objects, index_download_counts)]

        for qs, task in queries:
            qs = qs.order_by('-date').values_list('id', flat=True)
            if addons:
                pks = [int(a.strip()) for a in addons.split(',')]
                qs = qs.filter(addon__in=pks)

            if dates:
                if ':' in dates:
                    qs = qs.filter(date__range=dates.split(':'))
                else:
                    qs = qs.filter(date=dates)

            if not (dates or addons):
                # We're loading the whole world. Do it in stages so we get most
                # recent stats first and don't do huge queries.
                limits = (qs.model.objects.filter(date__isnull=False)
                          .extra(where=['date <> "0000-00-00"'])
                          .aggregate(min=Min('date'), max=Max('date')))
                num_days = (limits['max'] - limits['min']).days
                today = date.today()
                for start in range(0, num_days, STEP):
                    stop = start + STEP
                    date_range = (today - timedelta(days=stop),
                                  today - timedelta(days=start))
                    create_tasks(task, list(qs.filter(date__range=date_range)))
            else:
                create_tasks(task, list(qs))


def create_tasks(task, qs):
    ts = [task.subtask(args=[chunk]) for chunk in chunked(qs, 50)]
    TaskSet(ts).apply_async()


def fixup():
    queries = [(UpdateCount, index_update_counts),
               (DownloadCount, index_download_counts)]

    for model, task in queries:
        all_addons = model.objects.distinct().values_list('addon', flat=True)
        for addon in all_addons:
            qs = model.objects.filter(addon=addon)
            search = model.search().filter(addon=addon)
            if qs.count() != search.count():
                all_ids = list(qs.values_list('id', flat=True))
                search_ids = list(search.values()[:5000])
                ids = set(all_ids) - set(search_ids)
                log.info('Missing %s rows for %s.' % (len(ids), addon))
                create_tasks(task, list(ids))
