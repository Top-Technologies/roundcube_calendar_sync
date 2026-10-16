# -*- coding: utf-8 -*-
{
    "name": "Roundcube / cPanel CalDAV Calendar Sync",
    "version": "19.0.1.1.0",
    "category": "Productivity/Calendar",
    "summary": "Synchronize Odoo calendar events with a remote CalDAV calendar",
    "description": '''
Adds a per-user cPanel/CalDAV connection entry under Odoo's existing Calendar app.
Each Odoo user can configure their own connection; administrators can access all connections.
This is a production-oriented integration candidate and requires target-environment validation.
''',
    "author": "Custom",
    "license": "LGPL-3",
    "depends": ["base", "calendar"],
    "data": [
        "security/ir.model.access.csv",
        "security/caldav_sync_rules.xml",
        "views/caldav_sync_views.xml",
        "data/ir_cron.xml",
    ],
    "installable": True,
    "application": False,
}
