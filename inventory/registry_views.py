"""Infrastructure → Address registry: delegated subnets and their assignments, one tab per platform."""
import ipaddress

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l
from django.views.decorators.http import require_POST

from accounts.models import Role, audit
from accounts.utils import staff_required
from tenants.models import PrefixAssignment, Source, Tenant

from .models import DelegatedSubnet, Platform, Site



class PrefixForm(forms.Form):
    cidr = forms.CharField(label=_l("Subnet"), max_length=43, help_text=_l("e.g. 10.82.104.0/28 or 198.18.93.4/32"))
    site = forms.ChoiceField(label=_l("Site"))
    kind = forms.ChoiceField(label=_l("Use"), choices=PrefixAssignment.Kind.choices)
    tenant = forms.ChoiceField(label=_l("Project"), required=False, help_text=_l("Empty for a reservation."))
    note = forms.CharField(label=_l("Note"), max_length=200, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = [(s.code, s.code) for s in Site.objects.all()]
        from tenants.models import Lifecycle
        self.fields["tenant"].choices = [("", "—")] + [
            (str(t.pk), f"{t.group_name or t.name} · {t.organization}") for t in
            Tenant.objects.filter(approval="approved").exclude(status__in=[Lifecycle.ARCHIVED, Lifecycle.DELETING])
            .select_related("organization").order_by("name")]

    def clean_cidr(self):
        try:
            return str(ipaddress.ip_network(self.cleaned_data["cidr"].strip(), strict=True))
        except ValueError as exc:
            raise forms.ValidationError(str(exc))

    def clean(self):
        d = super().clean()
        name = d.get("tenant") or ""
        d["tenant_obj"] = Tenant.objects.filter(pk=int(name)).first() if name.isdigit() else None
        if d.get("kind") != PrefixAssignment.Kind.RESERVED and not name:
            self.add_error("tenant", _("A project is required unless the subnet is reserved."))
        if d.get("cidr"):
            net = ipaddress.ip_network(d["cidr"])
            for p in PrefixAssignment.active():
                if ipaddress.ip_network(p.cidr).overlaps(net):
                    self.add_error("cidr", _("Overlaps %(c)s already assigned (%(t)s).") % {
                        "c": p.cidr, "t": p.tenant or p.note or p.kind})
                    break
        return d


def _pool_row(pool, assignments):
    net = ipaddress.ip_network(pool.cidr)
    inside = [a for a in assignments if a.site == pool.site.code
              and ipaddress.ip_network(a.cidr).version == net.version and ipaddress.ip_network(a.cidr).subnet_of(net)]
    used = sum(ipaddress.ip_network(a.cidr).num_addresses for a in inside)
    return {"pool": pool, "assignments": sorted(inside, key=lambda a: ipaddress.ip_network(a.cidr)),
            "used": used, "size": net.num_addresses, "pct": round(100 * used / net.num_addresses), "ids": {a.pk for a in inside}}


def site_addresses(request, site, ctx):
    """The Address registry tab of a site: its delegated subnets grouped by platform, assignments, the form."""
    url = f"{reverse('infra_site', args=[site.code])}?tab=subnets"
    form = PrefixForm(request.POST or None, initial={"site": site.code})
    if request.method == "POST":
        if not (request.user.has_role(Role.SUPPORT) or request.user.is_super_admin):
            messages.error(request, _("Only support engineers assign subnets."))
        elif form.is_valid():
            d = form.cleaned_data
            p = PrefixAssignment.objects.create(cidr=d["cidr"], site=d["site"], kind=d["kind"], tenant=d["tenant_obj"],
                                                note=d["note"], source=Source.MANUAL, created_by=request.user)
            audit(request.user, "prefix_assigned", p, cidr=p.cidr, tenant=str(p.tenant or ""), kind=p.kind)
            messages.success(request, _("%(c)s recorded.") % {"c": p.cidr})
            return redirect(url)
    pools = list(DelegatedSubnet.objects.filter(site=site).select_related("site"))
    assignments = list(PrefixAssignment.active().filter(site=site.code).select_related("tenant"))
    rows = {pool.pk: _pool_row(pool, assignments) for pool in pools}
    groups, claimed = [], set()
    for p in Platform.objects.filter(site=site):
        mine = [rows[d.pk] for d in pools if d.segment in (p.segments or [])]
        if mine:
            groups.append({"label": p.key, "rows": mine, "used": sum(r["used"] for r in mine),
                           "size": sum(r["size"] for r in mine)})
            claimed.update(r["pool"].pk for r in mine)
    loose = [rows[d.pk] for d in pools if d.pk not in claimed]
    if loose:
        groups.append({"label": _("Other"), "rows": loose})
    placed = set().union(*(r["ids"] for r in rows.values())) if rows else set()
    overlaps = []
    nets = [(a, ipaddress.ip_network(a.cidr)) for a in assignments]
    for i, (a, na) in enumerate(nets):
        for b, nb in nets[i + 1:]:
            if na.version == nb.version and na.overlaps(nb):
                overlaps.append((a, b))
    ctx.update(groups=groups, outside=[a for a in assignments if a.pk not in placed], overlaps=overlaps, prefix_form=form)
    return None


@staff_required(Role.RESOURCE_MANAGER, Role.SUPPORT)
def registry_view(request):
    """Old link: the address registry now lives on each site."""
    st = Site.objects.first()
    return redirect(f"{reverse('infra_site', args=[st.code])}?tab=subnets" if st else reverse("inventory"))


@require_POST
@staff_required(Role.SUPPORT)
def prefix_release_view(request, pk):
    p = get_object_or_404(PrefixAssignment, pk=pk, released_at__isnull=True)
    p.released_at = timezone.now()
    p.save(update_fields=["released_at"])
    audit(request.user, "prefix_released", p, cidr=p.cidr)
    nxt = request.POST.get("next") or ""
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = reverse("registry")
    return redirect(nxt)
