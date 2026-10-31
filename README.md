# Claugine

**CLoud AUtomation enGINE** — a portal to run an OpenNebula-based cloud for many customers: from an organization's
registration to its projects' networks, gateways and quotas on the platforms, and day-2 operations of the sites
themselves. Project site and documentation: [claugine.com](https://claugine.com) ·
[docs.claugine.com](https://docs.claugine.com).

## What it does

- **Customers:** self-registration of organizations (e-mail confirmation, tax-ID check, checks against automated
  and suspicious registrations), staff approval, organization admins and members, invitations, join requests.
- **Projects:** ordering and approval, deployment on the OpenNebula platforms (group, quotas, users, networks), an
  edge gateway and an internet gateway (VyOS) per project with public subnets, BGP and shaping; changes after
  deployment (speed, subnets, size) as requests for approval; existing projects can be adopted from the platforms.
- **Infrastructure:** sites, platforms, clusters, networks and address registry, secrets (encrypted), router
  configuration templates, core services of a site (name servers, front ends, route reflectors, …), host health
  over IPMI/Redfish, timers that read the platforms and compare routers with their templates.
- **Staff:** roles (super admin, resource manager, support, sales), mandatory two-factor sign-in, audit log of every
  decision, daily reports.
- **The portal itself:** e-mail through its own relay (Postfix + DKIM) or an SMTP server, encrypted backups to a local
  folder or a remote host, two sites with database replication and switch-over, HAProxy with Let's Encrypt in front.

The provider's name and logo are set in Settings → Provider ("Example Cloud" is only the default).

## Install

Claugine runs on Ubuntu 26.04 as two portal VMs (optionally across two sites) behind HAProxy with Let's Encrypt.

- **Licensed clients** get the automated installer and the auto-update module from
  [my.claugine.com](https://my.claugine.com); they are not part of this repository.
- **To evaluate or install by hand**, follow the manual procedure in the documentation at
  [docs.claugine.com](https://docs.claugine.com). Configuration lives in `/etc/claugine-portal/`; management
  commands run as `sudo claugine-manage <command>`.

Production use requires a licence (see **Licence** below).

## Development

```
pip install -r requirements.txt
DEBUG=1 python manage.py migrate
DEBUG=1 python manage.py bootstrap_superadmin   # prints a one-time link
DEBUG=1 python manage.py runserver
```

Without an SMTP server, mail is written to `var/mail/`. Tests:
`DEBUG=1 SECRET_KEY=x PORTAL_ENV_FILE=/nonexistent python manage.py test`.

Interface languages: English, Russian, Kazakh.

## Licence

Claugine is **source-available** under the [Business Source License 1.1](LICENSE):

- You may copy, modify, build and use the code for **non-production** purposes (evaluation, development, testing) at
  no charge, whatever kind of organization you are.
- **Production use requires a commercial licence** from [my.claugine.com](https://my.claugine.com).
- **Each version becomes open source** (Apache License 2.0) four years after it is published.

The automated installer and the auto-update module are delivered only to licensed clients, from my.claugine.com, and
are not part of this repository.

### Contributions

Outside contributions are not accepted at this time.
