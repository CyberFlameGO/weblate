# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import time
from datetime import date, datetime, timedelta
from glob import glob
from typing import List, Optional

from celery import current_task
from celery.schedules import crontab
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext, override

from weblate.addons.models import Addon
from weblate.auth.models import User, get_anonymous
from weblate.lang.models import Language
from weblate.trans.autotranslate import AutoTranslate
from weblate.trans.exceptions import FileParseError
from weblate.trans.models import (
    Change,
    Comment,
    Component,
    Project,
    Suggestion,
    Translation,
)
from weblate.utils.celery import app
from weblate.utils.data import data_dir
from weblate.utils.errors import report_error
from weblate.utils.files import remove_tree
from weblate.utils.lock import WeblateLockTimeout
from weblate.utils.stats import prefetch_stats
from weblate.vcs.base import RepositoryException


@app.task(
    trail=False,
    autoretry_for=(WeblateLockTimeout,),
    retry_backoff=600,
    retry_backoff_max=3600,
)
def perform_update(cls, pk, auto=False, obj=None):
    try:
        if obj is None:
            if cls == "Project":
                obj = Project.objects.get(pk=pk)
            else:
                obj = Component.objects.get(pk=pk)
        if settings.AUTO_UPDATE in ("full", True) or not auto:
            obj.do_update()
        else:
            obj.update_remote_branch()
    except FileParseError:
        # This is stored as alert, so we can silently ignore here
        return


@app.task(
    trail=False,
    autoretry_for=(WeblateLockTimeout,),
    retry_backoff=600,
    retry_backoff_max=3600,
)
def perform_load(
    pk: int,
    force: bool = False,
    langs: Optional[List[str]] = None,
    changed_template: bool = False,
    from_link: bool = False,
):
    component = Component.objects.get(pk=pk)
    component.create_translations(
        force=force, langs=langs, changed_template=changed_template, from_link=from_link
    )


@app.task(
    trail=False,
    autoretry_for=(WeblateLockTimeout,),
    retry_backoff=600,
    retry_backoff_max=3600,
)
def perform_commit(pk, *args):
    component = Component.objects.get(pk=pk)
    component.commit_pending(*args)


@app.task(
    trail=False,
    autoretry_for=(WeblateLockTimeout,),
    retry_backoff=600,
    retry_backoff_max=3600,
)
def perform_push(pk, *args, **kwargs):
    component = Component.objects.get(pk=pk)
    component.do_push(*args, **kwargs)


@app.task(trail=False)
def update_component_stats(pk):
    component = Component.objects.get(pk=pk)
    component.stats.ensure_basic()
    project_stats = component.project.stats
    # Update language stats
    for language in Language.objects.filter(
        translation__component=component
    ).iterator():
        stats = project_stats.get_single_language_stats(language)
        stats.ensure_basic()


@app.task(
    trail=False,
    autoretry_for=(WeblateLockTimeout,),
    retry_backoff=600,
    retry_backoff_max=3600,
)
def commit_pending(hours=None, pks=None, logger=None):
    if pks is None:
        components = Component.objects.all()
    else:
        components = Component.objects.filter(translation__pk__in=pks).distinct()

    for component in prefetch_stats(components.prefetch()):
        if hours is None:
            age = timezone.now() - timedelta(hours=component.commit_pending_age)
        else:
            age = timezone.now() - timedelta(hours=hours)

        last_change = component.stats.last_changed
        if not last_change:
            continue
        if last_change > age:
            continue

        if not component.needs_commit():
            continue

        if logger:
            logger(f"Committing {component}")

        perform_commit.delay(component.pk, "commit_pending", None)


@app.task(trail=False)
def cleanup_project(pk):
    """
    Perform cleanup of project models.

    - Remove stale source Unit objects.
    - Update variants.
    """
    try:
        project = Project.objects.get(pk=pk)
    except Project.DoesNotExist:
        return

    for component in project.component_set.filter(template="").iterator():
        # Remove stale variants
        with transaction.atomic():
            component.update_variants()

        translation = component.source_translation
        # Skip translations with a filename (eg. when POT file is present)
        if translation.filename:
            continue
        with transaction.atomic():
            # Remove all units where there is just one referenced unit (self)
            deleted, details = (
                translation.unit_set.annotate(Count("unit"))
                .filter(unit__count__lte=1)
                .delete()
            )
            if deleted:
                translation.log_info("removed leaf units: %s", details)


@app.task(trail=False)
def cleanup_suggestions():
    # Process suggestions
    anonymous_user = get_anonymous()
    suggestions = Suggestion.objects.prefetch_related("unit")
    for suggestion in suggestions:
        with transaction.atomic():
            # Remove suggestions with same text as real translation
            if (
                suggestion.unit.target == suggestion.target
                and suggestion.unit.translated
            ):
                suggestion.delete_log(
                    anonymous_user, change=Change.ACTION_SUGGESTION_CLEANUP
                )
                continue

            # Remove duplicate suggestions
            sugs = Suggestion.objects.filter(
                unit=suggestion.unit, target=suggestion.target
            ).exclude(id=suggestion.id)
            # Do not rely on the SQL as MySQL compares strings case insensitive
            for other in sugs:
                if other.target == suggestion.target:
                    suggestion.delete_log(
                        anonymous_user, change=Change.ACTION_SUGGESTION_CLEANUP
                    )
                    break


@app.task(trail=False)
def update_remotes():
    """Update all remote branches (without attempt to merge)."""
    if settings.AUTO_UPDATE not in ("full", "remote", True, False):
        return

    for component in Component.objects.with_repo().iterator():
        perform_update("Component", -1, auto=True, obj=component)


@app.task(trail=False)
def cleanup_stale_repos():
    prefix = data_dir("vcs")
    vcs_mask = os.path.join(prefix, "*", "*")

    yesterday = time.monotonic() - 86400

    for path in glob(vcs_mask):
        if not os.path.isdir(path):
            continue

        # Skip recently modified paths
        if os.path.getmtime(path) > yesterday:
            continue

        # Parse path
        project, component = os.path.split(path[len(prefix) + 1 :])

        # Find matching components
        objects = Component.objects.with_repo().filter(
            slug=component, project__slug=project
        )

        # Remove stale dirs
        if not objects.exists():
            remove_tree(path)


@app.task(trail=False)
def cleanup_old_suggestions():
    if not settings.SUGGESTION_CLEANUP_DAYS:
        return
    cutoff = timezone.now() - timedelta(days=settings.SUGGESTION_CLEANUP_DAYS)
    Suggestion.objects.filter(timestamp__lt=cutoff).delete()


@app.task(trail=False)
def cleanup_old_comments():
    if not settings.COMMENT_CLEANUP_DAYS:
        return
    cutoff = timezone.now() - timedelta(days=settings.COMMENT_CLEANUP_DAYS)
    Comment.objects.filter(timestamp__lt=cutoff).delete()


@app.task(trail=False)
def repository_alerts(threshold=settings.REPOSITORY_ALERT_THRESHOLD):
    non_linked = Component.objects.with_repo()
    for component in non_linked.iterator():
        try:
            if component.repository.count_missing() > threshold:
                component.add_alert("RepositoryOutdated")
            else:
                component.delete_alert("RepositoryOutdated")
            if component.repository.count_outgoing() > threshold:
                component.add_alert("RepositoryChanges")
            else:
                component.delete_alert("RepositoryChanges")
        except RepositoryException as error:
            report_error(
                cause="Could not check repository status", project=component.project
            )
            component.add_alert("MergeFailure", error=component.error_text(error))


@app.task(trail=False)
def component_alerts(component_ids=None):
    if component_ids:
        components = Component.objects.filter(pk__in=component_ids)
    else:
        components = Component.objects.all()
    for component in components.prefetch():
        component.update_alerts()


@app.task(trail=False, autoretry_for=(Component.DoesNotExist,), retry_backoff=60)
def component_after_save(
    pk: int,
    changed_git: bool,
    changed_setup: bool,
    changed_template: bool,
    changed_variant: bool,
    skip_push: bool,
    create: bool,
):
    component = Component.objects.get(pk=pk)
    component.after_save(
        changed_git=changed_git,
        changed_setup=changed_setup,
        changed_template=changed_template,
        changed_variant=changed_variant,
        skip_push=skip_push,
        create=create,
    )
    return {"component": pk}


@app.task(trail=False)
def component_removal(pk, uid):
    user = User.objects.get(pk=uid)
    try:
        component = Component.objects.get(pk=pk)
        component.acting_user = user
        Change.objects.create(
            project=component.project,
            action=Change.ACTION_REMOVE_COMPONENT,
            target=component.slug,
            user=user,
            author=user,
        )
        component.delete()
        if component.allow_translation_propagation:
            components = component.project.component_set.filter(
                allow_translation_propagation=True
            ).exclude(pk=component.pk)
            for component_id in components.values_list("id", flat=True):
                update_checks.delay(component_id)
    except Component.DoesNotExist:
        return


@app.task(trail=False)
def project_removal(pk: int, uid: Optional[int]):
    if uid is None:
        user = get_anonymous()
    else:
        user = User.objects.get(pk=uid)
    try:
        project = Project.objects.get(pk=pk)
        create_project_backup(pk)
        Change.objects.create(
            action=Change.ACTION_REMOVE_PROJECT,
            target=project.slug,
            user=user,
            author=user,
        )
        project.stats.invalidate()
        project.delete()
    except Project.DoesNotExist:
        return


@app.task(
    trail=False,
    autoretry_for=(WeblateLockTimeout,),
    retry_backoff=600,
    retry_backoff_max=3600,
)
def auto_translate(
    user_id: int,
    translation_id: int,
    mode: str,
    filter_type: str,
    auto_source: str,
    component: Optional[int],
    engines: List[str],
    threshold: int,
    translation: Optional[Translation] = None,
    component_wide: bool = False,
):
    if translation is None:
        translation = Translation.objects.get(pk=translation_id)
    if user_id:
        user = User.objects.get(pk=user_id)
    else:
        user = None
    translation.log_info(
        "starting automatic translation %s: %s: %s",
        current_task.request.id,
        auto_source,
        ", ".join(engines) if engines else component,
    )
    with translation.component.lock, override(user.profile.language if user else "en"):
        auto = AutoTranslate(
            user, translation, filter_type, mode, component_wide=component_wide
        )
        if auto_source == "mt":
            auto.process_mt(engines, threshold)
        else:
            auto.process_others(component)
        translation.log_info("completed automatic translation")

        if auto.updated == 0:
            message = _("Automatic translation completed, no strings were updated.")
        else:
            message = (
                ngettext(
                    "Automatic translation completed, %d string was updated.",
                    "Automatic translation completed, %d strings were updated.",
                    auto.updated,
                )
                % auto.updated
            )
        return {"translation": translation_id, "message": message}


@app.task(
    trail=False,
    autoretry_for=(WeblateLockTimeout,),
    retry_backoff=600,
    retry_backoff_max=3600,
)
def auto_translate_component(
    component_id: int,
    mode: str,
    filter_type: str,
    auto_source: str,
    engines: List[str],
    threshold: int,
    component: Optional[int],
):
    component_obj = Component.objects.get(pk=component_id)

    for translation in component_obj.translation_set.iterator():
        if translation.is_source:
            continue

        auto_translate(
            None,
            translation.pk,
            mode,
            filter_type,
            auto_source,
            component,
            engines,
            threshold,
            translation=translation,
            component_wide=True,
        )
    component_obj.update_source_checks()
    component_obj.run_batched_checks()
    return {"component": component_obj.id}


@app.task(trail=False)
def create_component(addons_from=None, in_task=False, **kwargs):
    kwargs["project"] = Project.objects.get(pk=kwargs["project"])
    kwargs["source_language"] = Language.objects.get(pk=kwargs["source_language"])
    component = Component.objects.create(**kwargs)
    Change.objects.create(action=Change.ACTION_CREATE_COMPONENT, component=component)
    if addons_from:
        addons = Addon.objects.filter(
            component__pk=addons_from, project_scope=False, repo_scope=False
        )
        for addon in addons:
            # Avoid installing duplicate addons
            if component.addon_set.filter(name=addon.name).exists():
                continue
            if not addon.addon.can_install(component, None):
                continue
            addon.addon.create(component, configuration=addon.configuration)
    if in_task:
        return {"component": component.id}
    return component


@app.task(trail=False)
def update_checks(pk: int, update_state: bool = False):
    component = Component.objects.get(pk=pk)
    component.batch_checks = True
    for translation in component.translation_set.exclude(
        pk=component.source_translation.pk
    ).prefetch():
        for unit in translation.unit_set.prefetch():
            if update_state:
                unit.update_state()
            unit.run_checks()
    for unit in component.source_translation.unit_set.prefetch():
        if update_state:
            unit.update_state()
        unit.run_checks()
    component.run_batched_checks()
    component.invalidate_cache()


@app.task(trail=False)
def daily_update_checks():
    components = Component.objects.all()
    today = date.today()
    if settings.BACKGROUND_TASKS == "never":
        return
    if settings.BACKGROUND_TASKS == "monthly":
        components = components.annotate(idmod=F("id") % 30).filter(idmod=today.day)
    elif settings.BACKGROUND_TASKS == "weekly":
        components = components.annotate(idmod=F("id") % 7).filter(
            idmod=today.weekday()
        )
    for component_id in components.values_list("id", flat=True):
        update_checks.delay(component_id)


@app.task(trail=False)
def cleanup_project_backups():
    # This intentionally does not use Project objects to remove stale backups
    # for removed projects as well.
    rootdir = data_dir("projectbackups")
    backup_cutoff = datetime.now() - timedelta(days=settings.PROJECT_BACKUP_KEEP_DAYS)
    for projectdir in glob(os.path.join(rootdir, "*")):
        if not os.path.isdir(projectdir):
            continue
        if projectdir.endswith("import"):
            # Keep imports for shorter time, but more of them
            cutoff = datetime.now() - timedelta(days=1)
            max_count = 30
        else:
            cutoff = backup_cutoff
            max_count = settings.PROJECT_BACKUP_KEEP_COUNT
        backups = sorted(
            (
                (
                    path,
                    datetime.fromtimestamp(int(path.split(".")[0])),
                )
                for path in os.listdir(projectdir)
                if path.endswith(".zip") or path.endswith(".zip.part")
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        while len(backups) > max_count:
            remove = backups.pop()
            os.unlink(os.path.join(projectdir, remove[0]))

        for backup in backups:
            if backup[1] < cutoff:
                os.unlink(os.path.join(projectdir, backup[0]))


@app.task(trail=False)
def create_project_backup(pk):
    from weblate.trans.backups import ProjectBackup

    project = Project.objects.get(pk=pk)
    ProjectBackup().backup_project(project)


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(3600, commit_pending.s(), name="commit-pending")
    sender.add_periodic_task(
        crontab(hour=3, minute=30), update_remotes.s(), name="update-remotes"
    )
    sender.add_periodic_task(
        crontab(hour=0, minute=30), daily_update_checks.s(), name="daily-update-checks"
    )
    sender.add_periodic_task(3600 * 24, repository_alerts.s(), name="repository-alerts")
    sender.add_periodic_task(3600 * 24, component_alerts.s(), name="component-alerts")
    sender.add_periodic_task(
        3600 * 24, cleanup_suggestions.s(), name="suggestions-cleanup"
    )
    sender.add_periodic_task(
        3600 * 24, cleanup_stale_repos.s(), name="cleanup-stale-repos"
    )
    sender.add_periodic_task(
        3600 * 24, cleanup_old_suggestions.s(), name="cleanup-old-suggestions"
    )
    sender.add_periodic_task(
        3600 * 24, cleanup_old_comments.s(), name="cleanup-old-comments"
    )
    sender.add_periodic_task(
        crontab(hour=2, minute=30),
        cleanup_project_backups.s(),
        name="cleanup-project-backups",
    )
