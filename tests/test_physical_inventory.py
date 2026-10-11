import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import smart_risk
from deploy.sync_physical_inventory import parse_lsblk


class PhysicalInventoryTests(unittest.TestCase):
    def test_inventory_collector_excludes_virtual_and_network_disks(self):
        rows = '\n'.join([
            'NAME="sda" SERIAL="drive-scsi0" MODEL="QEMU HARDDISK" TYPE="disk"',
            'NAME="sdb" SERIAL="iscsi-uuid" MODEL="TrueNAS iSCSI Disk" TYPE="disk"',
            'NAME="sdc" SERIAL="REAL123" MODEL="ST2000NM0043" TYPE="disk"',
        ])
        self.assertEqual(set(parse_lsblk(rows)), {"REAL123"})

    def test_fresh_inventory_is_exact_serial_allowlist(self):
        now = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "storage-01.json").write_text(json.dumps({
                "host": "storage-01",
                "updated": "2026-10-11T17:59:00Z",
                "disks": {"TEST-DISK-001": {"device": "/dev/sdz"}},
            }))
            with patch.object(smart_risk, "INVENTORY_DIR", root), patch.object(
                smart_risk, "INVENTORY_MAX_AGE_SECONDS", 900
            ), patch.object(smart_risk, "REQUIRED_INVENTORY_SYSTEMS", {"storage-01"}):
                allow, status = smart_risk.load_physical_inventories({"id1": "storage-01"}, now)
        self.assertEqual(allow, {"id1": {"TEST-DISK-001"}})
        self.assertEqual(status["storage-01"]["state"], "fresh")

    def test_stale_or_missing_inventory_fails_closed_for_required_host(self):
        now = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "storage-01.json").write_text(json.dumps({
                "host": "storage-01",
                "updated": "2026-10-11T17:00:00Z",
                "disks": {"OLD-SERIAL": {}},
            }))
            with patch.object(smart_risk, "INVENTORY_DIR", root), patch.object(
                smart_risk, "INVENTORY_MAX_AGE_SECONDS", 900
            ), patch.object(smart_risk, "REQUIRED_INVENTORY_SYSTEMS", {"storage-01", "storage-02"}):
                allow, status = smart_risk.load_physical_inventories(
                    {"id1": "storage-01", "id2": "storage-02"}, now
                )
        self.assertEqual(allow, {"id1": set(), "id2": set()})
        self.assertEqual(status["storage-01"]["state"], "stale")
        self.assertEqual(status["storage-02"]["state"], "missing")

    def test_future_dated_inventory_fails_closed(self):
        now = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "storage-01.json").write_text(json.dumps({
                "host": "storage-01",
                "updated": "2026-10-12T18:00:00Z",
                "disks": {"SERIAL1": {}},
            }))
            with patch.object(smart_risk, "INVENTORY_DIR", root), patch.object(
                smart_risk, "INVENTORY_MAX_AGE_SECONDS", 900
            ), patch.object(smart_risk, "REQUIRED_INVENTORY_SYSTEMS", {"storage-01"}):
                allow, status = smart_risk.load_physical_inventories({"id1": "storage-01"}, now)
        self.assertEqual(allow, {"id1": set()})
        self.assertEqual(status["storage-01"]["state"], "stale")


if __name__ == "__main__":
    unittest.main()
