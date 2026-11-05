"""owner 2026-11-03 (u11): the orderer picks a base IX subnet per public prefix; "auto" lets the portal choose."""
from django.test import TestCase

from accounts.tests import make_inventory
from tenants.ordering import inet_base_subnets, plan_prefixes


class BaseSubnet(TestCase):
    def setUp(self):
        make_inventory()  # dc1.cloud reaches cloud-ix 203.0.113.0/25 (inet); 203.0.113.0/29 is a service block

    def test_base_subnets_listed_per_platform(self):
        choices, meta = inet_base_subnets()
        self.assertIn(("203.0.113.0/25", "cloud-ix · 203.0.113.0/25"), choices)
        self.assertIn("dc1.cloud", meta["203.0.113.0/25"]["platforms"])
        # the portal-ix pool belongs to an ext-managed front end, not an orderable cluster
        self.assertNotIn("198.51.100.0/26", meta)

    def test_auto_still_allocates(self):
        order = {"connections": ["inet"], "inet_main_prefix": 31, "site": "dc1"}
        plan = plan_prefixes(order, "dc1.cloud")
        self.assertEqual(plan[0][0], "inet_main")
        self.assertTrue(plan[0][1].startswith("203.0.113."))  # from the cloud-ix pool

    def test_chosen_base_subnet_is_used(self):
        order = {"connections": ["inet"], "inet_main_prefix": 30,
                 "inet_main_pool": "203.0.113.0/25",
                 "inet_extra_prefixes": [29], "inet_extra_pools": ["203.0.113.0/25"], "site": "dc1"}
        plan = dict(plan_prefixes(order, "dc1.cloud"))
        import ipaddress
        base = ipaddress.ip_network("203.0.113.0/25")
        self.assertTrue(ipaddress.ip_network(plan["inet_main"]).subnet_of(base))
        self.assertTrue(ipaddress.ip_network(plan["inet_extra"]).subnet_of(base))

    def test_request_pool_choices(self):
        # the customer "+ subnet" dialog offers the tenant's platform INET subnets, with "auto" added in the template
        from tenants.models import Organization, Tenant
        from tenants.network_views import _pool_choices
        org = Organization.objects.create(name="Acme", short_name="acme")
        t = Tenant.objects.create(organization=org, name="lab", title="lab", kind="cloud", approval="approved",
                                  order={"platform": "dc1.cloud", "site": "dc1"})
        self.assertEqual(_pool_choices(t, "inet"), [("203.0.113.0/25", "cloud-ix · 203.0.113.0/25")])

    def test_rm_pool_overrides_main(self):
        # the resource manager's choice at approval (inet_pool) wins over the main prefix's ordered base subnet
        order = {"connections": ["inet"], "inet_main_prefix": 31, "inet_main_pool": "", "site": "dc1"}
        plan = plan_prefixes(order, "dc1.cloud", inet_pool="203.0.113.0/25")
        self.assertTrue(plan[0][1].startswith("203.0.113."))
