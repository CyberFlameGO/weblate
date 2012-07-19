from django.core.management.base import BaseCommand
from weblate.trans.models import Suggestion, Check, Unit, Project
from weblate.lang.models import Language

class Command(BaseCommand):
    help = 'clenups orphaned checks and suggestions'

    def handle(self, *args, **options):
        '''
        Perfoms cleanup of Weblate database.
        '''
        for lang in Language.objects.all():
            for prj in Project.objects.all():

                # Remove checks referring to deleted or not translated units
                translatedunits = Unit.objects.filter(translation__language = lang, translated = True, translation__subproject__project = prj).values('checksum').distinct()
                Check.objects.filter(language = lang, project = prj).exclude(checksum__in = translatedunits).delete()

                # Remove suggestions referring to deleted units
                units = Unit.objects.filter(translation__language = lang, translation__subproject__project = prj).values('checksum').distinct()
                Suggestion.objects.filter(language = lang, project = prj).exclude(checksum__in = units).delete()

                for sug in Suggestion.objects.filter(language = lang, project = prj).iterator():
                    # Remove suggestions with same text as real translation
                    units = Unit.objects.filter(checksum = sug.checksum, translation__language = lang, translation__subproject__project = prj, target = sug.target)
                    if units.exists():
                        sug.delete()
                    # Remove duplicate suggestions
                    sugs = Suggestion.objects.filter(checksum = sug.checksum, language = lang, project = prj, target = sug.target).exclude(id = sug.id)
                    if sugs.exists():
                        sugs.delete()
