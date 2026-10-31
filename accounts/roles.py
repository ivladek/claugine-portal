"""Staff roles: who holds which role, kept complete.

Every role (super admin, resource manager, support engineer, sales manager, key account manager) always has
at least one activated holder. Only activated people (can sign in, password set) hold roles that count; with
a single activated person, that person (the super admin) holds all of them. Several super admins can split
the roles between them. A user holding several roles approves once for all of them (tenants.views).
"""
from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

ROLE_COLUMNS = [
    ("is_super_admin", _l("Super admin")),
    ("is_resource_manager", _l("Resource manager")),
    ("is_support", _l("Support engineer")),
    ("is_sales", _l("Sales manager")),
    ("is_kam", _l("Key account manager")),
]
FIELDS = [f for f, _l2 in ROLE_COLUMNS]


def activated(u):
    return bool(u.is_active and u.login_enabled and u.has_usable_password())


def staff():
    from .models import Kind, User
    return list(User.objects.filter(kind=Kind.STAFF).order_by("-is_super_admin", "full_name", "email"))


def matrix():
    """Rows for the Users → Roles table: {u, active, cells: [(field, on, locked)]}, and whether all is forced."""
    people = staff()
    active = [u for u in people if activated(u)]
    single = len(active) == 1
    rows = []
    for u in people:
        a = activated(u)
        rows.append({"u": u, "active": a, "cells": [(f, (True if single and a else getattr(u, f)), not a or single)
                                                    for f in FIELDS]})
    return rows, single


def missing(people=None):
    """Roles without an activated holder."""
    people = people if people is not None else staff()
    return [f for f in FIELDS if not any(activated(u) and getattr(u, f) for u in people)]


def ensure_complete(actor=None):
    """A single activated person holds every role. With several, nothing is given automatically (correction 16: no
    binding to the first user) — the edits keep every role with an active user; a role left without one (should not
    happen) goes to the activated super admins."""
    from .models import audit
    people = staff()
    active = [u for u in people if activated(u)]
    if not active:
        return []
    changed = []
    with transaction.atomic():
        if len(active) == 1:
            u = active[0]
            todo = [f for f in FIELDS if not getattr(u, f)]
            for f in todo:
                setattr(u, f, True)
            if todo:
                u.save(update_fields=todo)
                changed.append((u, todo))
        else:
            supers = [u for u in active if u.is_super_admin] or active[:1]
            for f in missing(people):
                for u in supers:
                    setattr(u, f, True)
                    u.save(update_fields=[f])
                    changed.append((u, [f]))
    for u, fields in changed:
        audit(actor, "staff_roles_completed", u, roles=",".join(fields))
    return changed


def save(actor, posted):
    """Apply {user id: set of fields} for the activated people. Returns a list of errors (nothing saved then)."""
    from .models import audit
    people = staff()
    active = [u for u in people if activated(u)]
    if len(active) == 1:
        ensure_complete(actor)
        return []
    new = {}
    for u in people:
        new[u.pk] = {f: (f in posted.get(u.pk, set())) if activated(u) else getattr(u, f) for f in FIELDS}
    errors = []
    for f, label in ROLE_COLUMNS:
        if not any(new[u.pk][f] for u in active):
            errors.append(_("%(r)s: at least one active user must hold it.") % {"r": label})
    if not new[actor.pk]["is_super_admin"]:
        errors.append(_("You cannot take the super admin role from yourself."))
    if errors:
        return errors
    with transaction.atomic():
        for u in active:
            changed = [f for f in FIELDS if getattr(u, f) != new[u.pk][f]]
            if not changed:
                continue
            for f in changed:
                setattr(u, f, new[u.pk][f])
            if "is_super_admin" in changed:
                u.is_staff = u.is_superuser = u.is_super_admin
                changed += ["is_staff", "is_superuser"]
            u.save(update_fields=changed)
            audit(actor, "staff_roles_changed", u, roles=",".join(u.staff_role_labels) or "-")
    return []
