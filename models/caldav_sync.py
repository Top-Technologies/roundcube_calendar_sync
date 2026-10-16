# -*- coding: utf-8 -*-
"""Two-way CalDAV / cPanel Calendar Synchronization for Odoo 19.

Maintains bidirectional synchronization between Odoo Calendar events and remote CalDAV / cPanel calendars.
Features:
- Full bidirectional synchronization (Odoo <-> CalDAV).
- Tracks remote resource URL (href) and ETag / version for every event.
- Safe conflict detection comparing modification timestamps.
- Automatic background sync on event creation/updates via postcommit + periodic cron.
- Remote deletion detection safely archiving deleted meetings in Odoo.
- Safe deletion propagation from Odoo to remote CalDAV.
"""
import base64
import html
import logging
import re
import uuid
import zoneinfo
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree as ET
from urllib.parse import urlparse, urljoin, quote

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.modules.registry import Registry
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


def _unescape_ical(value):
    return (value or "").replace(r"\n", "\n").replace(r"\N", "\n").replace(r"\,", ",").replace(r"\;", ";").replace(r"\\", "\\")


def _escape_ical(value):
    return (value or "").replace("\\", "\\\\").replace("\r\n", "\\n").replace("\n", "\\n").replace(",", r"\,").replace(";", r"\;")


def _parse_ical_datetime(raw, params=""):
    """Parse UTC, local, and date-only iCalendar date-time forms."""
    raw = (raw or "").strip()
    if not raw:
        return None
    # All-day or date-only: YYYYMMDD
    if "VALUE=DATE" in (params or "").upper() or (len(raw) == 8 and raw.isdigit()):
        try:
            return datetime.strptime(raw[:8], "%Y%m%d")
        except ValueError:
            return None
    clean = raw.replace("-", "").replace(":", "")
    tz = timezone.utc
    if params:
        tz_match = re.search(r"TZID=([^;:]+)", params, re.IGNORECASE)
        if tz_match:
            tz_name = tz_match.group(1).strip("\"'")
            try:
                tz = zoneinfo.ZoneInfo(tz_name)
            except Exception:
                tz = timezone.utc
    try:
        if clean.endswith("Z"):
            dt = datetime.strptime(clean, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        elif "T" in clean:
            dt = datetime.strptime(clean[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz)
        else:
            dt = datetime.strptime(clean[:8], "%Y%m%d").replace(tzinfo=tz)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_vevents(ical_text):
    """Extract VEVENT properties from unfolded iCalendar text."""
    unfolded = re.sub(r"\r?\n[ \t]", "", ical_text or "")
    events = []
    current = None
    for line in unfolded.splitlines():
        line = line.strip()
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        left, value = line.split(":", 1)
        key_parts = left.split(";")
        key = key_parts[0].upper()
        params = ";".join(key_parts[1:])
        if key in ("UID", "SUMMARY", "DESCRIPTION", "DTSTART", "DTEND", "DURATION",
                   "LOCATION", "LAST-MODIFIED", "DTSTAMP", "SEQUENCE", "STATUS"):
            current[key] = {"value": _unescape_ical(value), "params": params}
    return events


def _make_ical(event, event_uid, sequence=0):
    def fmt(dt):
        return dt.replace(tzinfo=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    now_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Odoo 19 CalDAV Sync//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{event_uid}",
        f"DTSTAMP:{now_str}",
        f"LAST-MODIFIED:{now_str}",
    ]
    if sequence:
        lines.append(f"SEQUENCE:{sequence}")
    if event.allday:
        lines.append(f"DTSTART;VALUE=DATE:{event.start.strftime('%Y%m%d')}")
        stop_dt = event.stop if (event.stop and event.stop > event.start) else (event.start + timedelta(days=1))
        lines.append(f"DTEND;VALUE=DATE:{stop_dt.strftime('%Y%m%d')}")
    else:
        lines.append(f"DTSTART:{fmt(event.start)}")
        lines.append(f"DTEND:{fmt(event.stop)}")
    lines.append(f"SUMMARY:{_escape_ical(event.name or '')}")
    if event.description:
        desc = html2plaintext(event.description).strip() if ("<" in event.description and ">" in event.description) else event.description.strip()
        if desc:
            lines.append(f"DESCRIPTION:{_escape_ical(desc)}")
    if event.location:
        lines.append(f"LOCATION:{_escape_ical(event.location)}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines).encode("utf-8")


class CalDAVSyncConfig(models.Model):
    _name = "caldav.sync.config"
    _description = "CalDAV Calendar Sync Configuration"
    _rec_name = "name"

    name = fields.Char(required=True, default="My cPanel Calendar")
    user_id = fields.Many2one(
        "res.users", string="Odoo User", required=True, default=lambda self: self.env.user,
        index=True, ondelete="cascade", help="The Odoo user who owns this cPanel connection.",
    )
    active = fields.Boolean(default=True)
    server_url = fields.Char(
        string="CalDAV Server URL", required=True,
        help="Server endpoint provided by cPanel, e.g. https://mail.example.com:2080",
    )
    calendar_url = fields.Char(
        string="Direct Calendar URL", required=True,
        help="Exact calendar collection URL provided by cPanel/Webmail.",
    )
    username = fields.Char(required=True)
    password = fields.Char(required=True, groups="base.group_user")
    sync_interval_minutes = fields.Integer(default=2, required=True)
    last_sync = fields.Datetime(readonly=True)
    last_status = fields.Selection(
        [("never", "Never run"), ("success", "Success"), ("error", "Error")],
        default="never", readonly=True,
    )
    last_message = fields.Text(readonly=True)

    @api.constrains("user_id")
    def _check_one_connection_per_user(self):
        for rec in self:
            duplicate = self.search([("id", "!=", rec.id), ("user_id", "=", rec.user_id.id)], limit=1)
            if duplicate:
                raise ValidationError(_("Each Odoo user can have only one active CalDAV connection in this version."))

    @api.constrains("server_url", "calendar_url")
    def _check_urls(self):
        for rec in self:
            for label, value in (("Server URL", rec.server_url), ("Calendar URL", rec.calendar_url)):
                if value and not value.startswith("https://"):
                    raise ValidationError(_("%s must use HTTPS.") % label)

    @api.constrains("sync_interval_minutes")
    def _check_interval(self):
        for rec in self:
            if rec.sync_interval_minutes < 1:
                raise ValidationError(_("Sync interval must be at least 1 minute."))

    def _request(self, method, url, headers=None, data=None):
        self.ensure_one()
        # Prevent credentials from being sent to an arbitrary host through a crafted URL.
        server_host = (urlparse(self.server_url).hostname or "").lower()
        target = urlparse(url)
        if target.scheme != "https" or not target.hostname or target.hostname.lower() != server_host:
            raise UserError(_("The request URL must use HTTPS and the same host as the configured CalDAV server."))
        request_headers = {"User-Agent": "Odoo-CalDAV-Sync/19.0"}
        request_headers.update(headers or {})
        token = base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        request_headers["Authorization"] = f"Basic {token}"
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UserError(_("Could not reach the CalDAV server: %s") % str(exc)) from exc

    def _write_status(self, status, message):
        self.sudo().write({
            "last_sync": fields.Datetime.now(),
            "last_status": status,
            "last_message": (message or "")[:4000],
        })

    def action_test_connection(self):
        self.ensure_one()
        try:
            status, _headers, _body = self._request(
                "PROPFIND", self.calendar_url,
                headers={"Depth": "0"},
            )
            if status not in (200, 207):
                raise UserError(
                    _("CalDAV endpoint returned HTTP %s. Check URL, credentials, and permissions.") % status
                )
            self._write_status("success", _("Connection test succeeded (HTTP %s).") % status)
        except Exception as exc:
            self._write_status("error", str(exc))
            if isinstance(exc, UserError):
                raise
            raise UserError(_("Connection test failed: %s") % str(exc)) from exc
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("CalDAV Connection"), "message": _("Connection test succeeded."),
                       "type": "success", "sticky": False},
        }

    def action_sync_now(self):
        self.ensure_one()
        self._sync()
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("CalDAV Sync"), "message": self.last_message or _("Sync completed."),
                       "type": "success" if self.last_status == "success" else "warning", "sticky": True},
        }

    def _sync(self):
        self.ensure_one()
        try:
            # 1. Propagate any local deletions/archivals to remote
            self._sync_odoo_deletions_to_remote()
            # 2. Pull remote changes and remote deletions into Odoo
            self._sync_remote_to_odoo()
            # 3. Push new or locally modified Odoo events to remote
            self._sync_odoo_to_remote()
            self._write_status("success", _("Synchronization completed successfully."))
        except Exception as exc:
            _logger.exception("CalDAV sync failed for config %s", self.id)
            try:
                self.env.cr.rollback()
            except Exception:
                pass
            self._write_status("error", str(exc))
            raise UserError(_("CalDAV synchronization failed: %s") % str(exc)) from exc

    def _sync_remote_to_odoo(self):
        query = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><d:getetag/><c:calendar-data/></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR">'
            '<c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>'
            '</c:calendar-query>'
        ).encode("utf-8")
        status, _headers, body = self._request(
            "REPORT", self.calendar_url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            data=query,
        )
        if status not in (200, 207):
            raise UserError(_("Calendar REPORT returned HTTP %s.") % status)
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise UserError(_("The CalDAV server returned invalid XML.")) from exc

        remote_uids = set()
        responses = root.findall(".//{DAV:}response")
        if not responses:
            responses = [el for el in root.iter() if el.tag.endswith("response")]

        for resp in responses:
            href_el = None
            for child in resp:
                if child.tag.endswith("href"):
                    href_el = child
                    break
            href_text = (href_el.text or "").strip() if href_el is not None else ""
            if not href_text:
                continue
            full_url = urljoin(self.calendar_url, href_text)

            etag = ""
            for el in resp.iter():
                if el.tag.endswith("getetag") and el.text:
                    etag = el.text.strip().strip('"')
                    break

            cal_data = ""
            for el in resp.iter():
                if el.tag.endswith("calendar-data") and el.text:
                    cal_data = el.text
                    break
            if not cal_data:
                continue

            for item in _parse_vevents(cal_data):
                event_uid = item.get("UID", {}).get("value", "").strip()
                if event_uid:
                    remote_uids.add(event_uid)
                    self._import_or_update_event(item, full_url, etag)

        # Reflect remote deletions safely in Odoo
        self._handle_remote_deletions(remote_uids)

    def _import_or_update_event(self, item, href, remote_etag):
        event_uid = item.get("UID", {}).get("value", "").strip()
        if not event_uid:
            return

        start_prop = item.get("DTSTART", {})
        start = _parse_ical_datetime(start_prop.get("value"), start_prop.get("params"))
        if start is None:
            return
        end_prop = item.get("DTEND", {})
        stop = _parse_ical_datetime(end_prop.get("value"), end_prop.get("params"))
        if stop is None or stop < start:
            stop = start

        # Check if all-day event
        is_allday = "VALUE=DATE" in (start_prop.get("params") or "").upper() or (len(start_prop.get("value", "").strip()) == 8 and start_prop.get("value", "").strip().isdigit())

        # Parse remote edit timestamp
        remote_dt = None
        for k in ("LAST-MODIFIED", "DTSTAMP"):
            if k in item and item[k].get("value"):
                remote_dt = _parse_ical_datetime(item[k]["value"], item[k].get("params"))
                if remote_dt:
                    break

        summary = (item.get("SUMMARY", {}).get("value") or "(No title)")[:256]
        raw_description = item.get("DESCRIPTION", {}).get("value", "")
        if raw_description and not ("<p" in raw_description.lower() or "<br" in raw_description.lower() or "<div>" in raw_description.lower()):
            description = html.escape(raw_description).replace("\n", "<br/>")
        else:
            description = raw_description
        location = item.get("LOCATION", {}).get("value", "")

        Event = self.env["calendar.event"].sudo()
        existing = Event.search([
            ("caldav_config_id", "=", self.id),
            "|", ("caldav_uid", "=", event_uid), ("caldav_href", "=", href),
        ], limit=1)

        now = fields.Datetime.now()
        vals = {
            "name": summary,
            "start": start,
            "stop": stop,
            "allday": is_allday,
            "description": description,
            "location": location,
            "caldav_config_id": self.id,
            "caldav_uid": event_uid,
            "caldav_href": href,
            "caldav_etag": remote_etag,
            "caldav_need_sync": False,
            "caldav_sync_origin": "synced",
            "caldav_last_synced": now,
            "active": True,
        }

        if not existing:
            # Check if this event was created in Odoo by the same user with the same UID
            existing = Event.search([
                ("user_id", "=", self.user_id.id),
                ("caldav_uid", "=", event_uid),
            ], limit=1)

        if not existing:
            # Brand new remote event
            vals["user_id"] = self.user_id.id
            vals["partner_ids"] = [(6, 0, [self.user_id.partner_id.id])]
            vals["privacy"] = "private"
            Event.with_context(caldav_import=True, no_calendar_sync=True, dont_notify=True).create(vals)
            return

        # Event already exists in Odoo.
        etag_changed = bool(remote_etag and existing.caldav_etag and existing.caldav_etag != remote_etag)
        content_changed = bool(
            existing.name != summary
            or existing.start != start
            or existing.stop != stop
            or (location and existing.location != location)
            or (not existing.caldav_etag and remote_etag)
        )

        if not etag_changed and not content_changed and existing.caldav_etag:
            # Remote version has not changed.
            return

        # Remote has been modified on cPanel.
        # Detect conflict: did Odoo also have pending un-synced edits?
        if existing.caldav_need_sync:
            odoo_modified = existing.write_date
            if remote_dt and odoo_modified and remote_dt < odoo_modified:
                # Odoo local edit is newer. Preserve Odoo changes, but update etag so upcoming PUT uses If-Match.
                existing.with_context(caldav_import=True, no_calendar_sync=True, dont_notify=True).write({
                    "caldav_etag": remote_etag,
                    "caldav_href": href,
                })
                _logger.info("CalDAV sync conflict on event %s: Odoo edit is newer (%s > %s). Preserving Odoo.", existing.id, odoo_modified, remote_dt)
                return
            else:
                _logger.info("CalDAV sync conflict on event %s: Remote edit is newer or equal (%s >= %s). Updating Odoo.", existing.id, remote_dt, odoo_modified)

        # Update existing Odoo event with remote changes
        existing.with_context(caldav_import=True, no_calendar_sync=True, dont_notify=True).write(vals)

    def _handle_remote_deletions(self, remote_uids):
        """Archive events in Odoo that were deleted on the remote CalDAV server."""
        Event = self.env["calendar.event"].sudo()
        synced_events = Event.search([
            ("caldav_config_id", "=", self.id),
            ("caldav_uid", "!=", False),
            ("active", "=", True),
            "|", ("caldav_etag", "!=", False), ("caldav_last_synced", "!=", False),
        ])
        to_archive = synced_events.filtered(lambda e: e.caldav_uid not in remote_uids)
        if to_archive:
            _logger.info("Archiving %d events in Odoo deleted from CalDAV: %s", len(to_archive), to_archive.mapped("name"))
            to_archive.with_context(caldav_import=True, no_calendar_sync=True, dont_notify=True).write({
                "active": False,
                "caldav_need_sync": False,
            })

    def _sync_odoo_deletions_to_remote(self):
        """Send HTTP DELETE to CalDAV server for events archived or marked deleted in Odoo."""
        Event = self.env["calendar.event"].sudo()
        archived_events = Event.search([
            ("caldav_config_id", "=", self.id),
            ("caldav_href", "!=", False),
            ("active", "=", False),
            ("caldav_need_sync", "=", True),
        ])
        for event in archived_events:
            try:
                headers = {}
                if event.caldav_etag:
                    headers["If-Match"] = f'"{event.caldav_etag}"'
                status, _headers, _body = self._request("DELETE", event.caldav_href, headers=headers)
                if status in (200, 204, 404):
                    event.with_context(caldav_import=True, no_calendar_sync=True).write({
                        "caldav_need_sync": False,
                        "caldav_etag": False,
                    })
            except Exception:
                _logger.exception("Failed to delete event %s on CalDAV server", event.id)

    def _sync_odoo_to_remote(self):
        """Upload this user's Odoo-created or locally modified events to the CalDAV server."""
        Event = self.env["calendar.event"].sudo()

        # Select only active events for this user that need syncing.
        # Limit initial upload of unlinked legacy events to recent/future events (last 90 days)
        # and batch to 100 to avoid request timeouts in production with large databases.
        cutoff = fields.Datetime.now() - timedelta(days=90)
        domain = [
            ("user_id", "=", self.user_id.id),
            ("active", "=", True),
            ("caldav_config_id", "in", [False, self.id]),
            "|",
            ("caldav_need_sync", "=", True),
            "&", ("caldav_href", "=", False), ("stop", ">=", cutoff),
        ]
        events = Event.search(domain, limit=100)

        now = fields.Datetime.now()
        upload_errors = []
        for event in events:
            try:
                event_uid = event.caldav_uid or str(uuid.uuid4())
                url = event.caldav_href or (self.calendar_url.rstrip("/") + "/" + quote(event_uid, safe="") + ".ics")
                headers = {"Content-Type": "text/calendar; charset=utf-8"}

                is_new = not bool(event.caldav_href)
                if is_new:
                    # Brand new event: create only if not existing
                    headers["If-None-Match"] = "*"
                elif event.caldav_etag:
                    # Existing event: use If-Match to prevent overwriting concurrent remote edits
                    headers["If-Match"] = f'"{event.caldav_etag}"'

                ical_data = _make_ical(event, event_uid)
                status, resp_headers, _body = self._request("PUT", url, headers=headers, data=ical_data)

                if status in (200, 201, 204):
                    new_etag = resp_headers.get("ETag") or resp_headers.get("etag") or ""
                    new_etag = new_etag.strip().strip('"')
                    event.with_context(caldav_import=True, no_calendar_sync=True, dont_notify=True).write({
                        "caldav_config_id": self.id,
                        "caldav_uid": event_uid,
                        "caldav_href": url,
                        "caldav_etag": new_etag or event.caldav_etag,
                        "caldav_need_sync": False,
                        "caldav_sync_origin": "synced",
                        "caldav_last_synced": now,
                    })
                elif status == 412:
                    # Precondition Failed: remote event was modified since last fetch or resource already exists.
                    # Keep caldav_need_sync=True so it will be resolved by the remote pull step.
                    _logger.warning("HTTP 412 Precondition Failed uploading event %s (%s). Preserving remote version.", event.id, event.name)
                else:
                    _logger.warning("Uploading event '%s' returned HTTP %s", event.name, status)
                    upload_errors.append(f"'{event.name}' (HTTP {status})")
            except Exception as exc:
                _logger.warning("Failed uploading event %s: %s", event.id, exc)
                upload_errors.append(f"'{event.name}' ({str(exc)})")

        if upload_errors:
            _logger.warning("CalDAV sync completed with upload issues: %s", ", ".join(upload_errors))

    @api.model
    def _cron_sync_active(self):
        """Run periodically to synchronize all active connections."""
        now = fields.Datetime.now()
        configs = self.search([("active", "=", True)]) if not self else self.filtered("active")
        for config in configs:
            if config.last_sync and config.sync_interval_minutes:
                elapsed = (now - config.last_sync).total_seconds() / 60.0
                if elapsed < config.sync_interval_minutes:
                    continue
            try:
                config.with_user(config.user_id).with_context(caldav_sync_job=True)._sync()
            except Exception:
                _logger.exception("Scheduled CalDAV sync failed for config %s", config.id)


class CalendarEvent(models.Model):
    _inherit = "calendar.event"

    caldav_config_id = fields.Many2one(
        "caldav.sync.config", string="CalDAV Configuration", copy=False, index=True, ondelete="set null"
    )
    caldav_uid = fields.Char(string="CalDAV UID", copy=False, index=True)
    caldav_href = fields.Char(string="CalDAV Resource URL", copy=False)
    caldav_etag = fields.Char(string="CalDAV ETag", copy=False)
    caldav_need_sync = fields.Boolean(string="Need CalDAV Sync", default=True, copy=False, index=True)
    caldav_last_synced = fields.Datetime(string="Last Synced with CalDAV", copy=False)
    caldav_sync_origin = fields.Selection(
        [("local", "Local"), ("odoo", "Created in Odoo"), ("remote", "Imported from CalDAV"),
         ("synced", "Synced")],
        default="local", copy=False, index=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get("caldav_import"):
            Config = self.env["caldav.sync.config"].sudo()
            for vals in vals_list:
                event_user_id = vals.get("user_id") or self.env.user.id
                config = Config.search([
                    ("user_id", "=", event_user_id), ("active", "=", True)
                ], limit=1)
                if config:
                    vals["caldav_config_id"] = config.id
                    vals["caldav_sync_origin"] = "odoo"
                    vals["caldav_need_sync"] = True

        records = super().create(vals_list)

        # Trigger automatic background upload after commit
        if not self.env.context.get("caldav_import") and not self.env.context.get("no_calendar_sync"):
            for record in records:
                if record.caldav_config_id and record.caldav_config_id.active:
                    record._trigger_caldav_sync()

        return records

    def write(self, vals):
        synced_fields = {"name", "start", "stop", "allday", "description", "location", "user_id", "active"}
        if not self.env.context.get("caldav_import") and synced_fields.intersection(vals.keys()):
            vals["caldav_need_sync"] = True
            vals["caldav_sync_origin"] = "odoo"

        result = super().write(vals)

        # Trigger automatic background upload after commit
        if not self.env.context.get("caldav_import") and not self.env.context.get("no_calendar_sync"):
            if synced_fields.intersection(vals.keys()):
                for record in self:
                    if record.caldav_config_id and record.caldav_config_id.active:
                        record._trigger_caldav_sync()

        return result

    def unlink(self):
        for event in self:
            if event.caldav_href and event.caldav_config_id and event.caldav_config_id.active:
                try:
                    headers = {}
                    if event.caldav_etag:
                        headers["If-Match"] = f'"{event.caldav_etag}"'
                    event.caldav_config_id._request("DELETE", event.caldav_href, headers=headers)
                except Exception:
                    _logger.warning("Could not delete CalDAV resource on unlink for event %s", event.id)
        return super().unlink()

    def _trigger_caldav_sync(self):
        """Schedule immediate push to CalDAV server after transaction commit."""
        self.ensure_one()
        event_id = self.id
        config_id = self.caldav_config_id.id
        dbname = self.env.cr.dbname
        user_id = self.env.uid
        context = dict(self.env.context)

        @self.env.cr.postcommit.add
        def sync_after_commit():
            try:
                db_registry = Registry(dbname)
                with db_registry.cursor() as cr:
                    env = api.Environment(cr, user_id, context)
                    config = env["caldav.sync.config"].sudo().browse(config_id)
                    event = env["calendar.event"].sudo().browse(event_id)
                    if config.exists() and config.active and event.exists() and event.caldav_need_sync:
                        config.with_user(config.user_id).with_context(no_calendar_sync=True)._sync_odoo_to_remote()
            except Exception as e:
                _logger.warning("Automatic CalDAV upload failed for event %s: %s", event_id, e)
