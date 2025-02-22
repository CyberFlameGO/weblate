# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Generated by Django 4.1.3 on 2022-12-12 10:40

from django.db import migrations

from weblate.utils.db import PG_DROP, PG_TRGM

FIELDS = (
    ("unit", "source", ", translation_id"),
    ("unit", "target", ", translation_id"),
    ("unit", "context", ", translation_id"),
    ("unit", "note", ", translation_id"),
    ("unit", "location", ", translation_id"),
    ("unit", "explanation", ", translation_id"),
    ("suggestion", "target", ", unit_id"),
    ("comment", "comment", ", unit_id"),
)


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    # Install btree_gin for gin btree search and index
    cur = schema_editor.connection.cursor()
    cur.execute("SELECT * FROM pg_extension WHERE extname = 'btree_gin'")
    if not cur.fetchone():
        schema_editor.execute("CREATE EXTENSION IF NOT EXISTS btree_gin")

    for table, field, extra in FIELDS:
        schema_editor.execute(PG_DROP.format(table, field))
        schema_editor.execute(PG_TRGM.format(table, field, extra))


class Migration(migrations.Migration):
    dependencies = [
        ("trans", "0162_alter_component_language_code_style"),
    ]

    operations = [
        migrations.RunPython(
            create_index, migrations.RunPython.noop, elidable=False, atomic=False
        )
    ]
