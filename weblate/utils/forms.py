# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

from crispy_forms.layout import Div, Field
from crispy_forms.utils import TEMPLATE_PACK
from django import forms
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

from weblate.trans.defines import EMAIL_LENGTH, USERNAME_LENGTH
from weblate.trans.filter import FILTERS
from weblate.trans.util import sort_unicode
from weblate.utils.validators import validate_email, validate_username


class UsernameField(forms.CharField):
    default_validators = [validate_username]

    def __init__(self, *args, **kwargs):
        params = {
            "max_length": USERNAME_LENGTH,
            "help_text": _(
                "Username may only contain letters, "
                "numbers or the following characters: @ . + - _"
            ),
            "label": _("Username"),
            "required": True,
        }
        params.update(kwargs)
        self.valid = None

        super().__init__(*args, **params)


class EmailField(forms.EmailField):
    """Slightly restricted EmailField.

    We blacklist some additional local parts and customize error messages.
    """

    default_validators = [validate_email]

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", EMAIL_LENGTH)
        super().__init__(*args, **kwargs)


class SortedSelectMixin:
    """Mixin for Select widgets to sort choices alphabetically."""

    def optgroups(self, name, value, attrs=None):
        groups = super().optgroups(name, value, attrs)
        return sort_unicode(groups, lambda val: str(val[1][0]["label"]))


class ColorWidget(forms.RadioSelect):
    def __init__(self, attrs=None, choices=()):
        attrs = {**(attrs or {}), "class": "color_edit"}
        super().__init__(attrs, choices)


class SortedSelectMultiple(SortedSelectMixin, forms.SelectMultiple):
    """Wrapper class to sort choices alphabetically."""


class SortedSelect(SortedSelectMixin, forms.Select):
    """Wrapper class to sort choices alphabetically."""


class ContextDiv(Div):
    def __init__(self, *fields, **kwargs):
        self.context = kwargs.pop("context", {})
        super().__init__(*fields, **kwargs)

    def render(self, form, context, template_pack=TEMPLATE_PACK, **kwargs):
        template = self.get_template_name(template_pack)
        return render_to_string(template, self.context)


class SearchField(Field):
    def __init__(self, *args, **kwargs):
        kwargs["template"] = "snippets/query-field.html"
        super().__init__(*args, **kwargs)

    def render(self, form, context, template_pack=TEMPLATE_PACK, **kwargs):
        extra_context = {"custom_filter_list": self.get_search_query_choices()}
        return super().render(form, context, template_pack, extra_context, **kwargs)

    def get_search_query_choices(self):
        """Return all filtering choices for query field."""
        filter_keys = [
            "nottranslated",
            "todo",
            "translated",
            "fuzzy",
            "suggestions",
            "variants",
            "screenshots",
            "labels",
            "context",
            "nosuggestions",
            "comments",
            "allchecks",
            "approved",
            "unapproved",
        ]
        result = [
            (key, FILTERS.get_filter_name(key), FILTERS.get_filter_query(key))
            for key in filter_keys
        ]
        return result


class FilterForm(forms.Form):
    project = forms.SlugField(required=False)
    component = forms.SlugField(required=False)
    lang = forms.SlugField(required=False)
    user = UsernameField(required=False)
