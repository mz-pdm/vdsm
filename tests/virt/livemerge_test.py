#
# Copyright 2020 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#
import json
import libvirt
import logging
import os
import threading
import time
import yaml

import pytest

from vdsm.common import response
from vdsm.common import exception
from vdsm.common import xmlutils

from vdsm.virt import metadata
from vdsm.virt.domain_descriptor import DomainDescriptor, XmlSource
from vdsm.virt.livemerge import (
    JobNotReadyError,
    JobUnrecoverableError,
    DriveMerger,
    CleanupThread,
    Job,
)
from vdsm.virt.vm import Vm
from vdsm.virt.vmdevices import storage

from testlib import recorded, read_data, read_files

from . import vmfakelib as fake

TIMEOUT = 10

log = logging.getLogger("test")


class FakeTime(object):

    def __init__(self, value=0):
        self.time = value

    def __call__(self):
        return self.time


@pytest.fixture
def fake_time(monkeypatch):
    fake_time = FakeTime()
    monkeypatch.setattr(time, "monotonic", fake_time)
    return fake_time


class FakeDrive:

    def __init__(self):
        self.volumeChain = []


class FakeDriveMonitor:

    def __init__(self):
        # driver_monitor is always enabled when calling cleanup.
        self.enabled = True

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False


class FakeVM:

    def __init__(self):
        self.drive_monitor = FakeDriveMonitor()
        self.log = logging.getLogger()

    @recorded
    def sync_volume_chain(self, drive):
        pass


class FakeCleanupThread(CleanupThread):
    """
    TODO: use VM/Storage methods instead of these so we can
    test for changes in the real code.
    """
    @recorded
    def tryPivot(self):
        pass

    @recorded
    def update_base_size(self):
        pass

    @recorded
    def teardown_top_volume(self):
        pass


def test_cleanup_initial():
    job = fake_job()
    v = FakeVM()
    t = FakeCleanupThread(
        vm=v, job=job, drive=FakeDrive(), doPivot=True)

    assert t.state == CleanupThread.TRYING
    assert v.drive_monitor.enabled


def test_cleanup_done():
    job = fake_job()
    v = FakeVM()
    drive = FakeDrive()
    t = FakeCleanupThread(vm=v, job=job, drive=drive, doPivot=True)
    t.start()
    t.join()

    assert t.state == CleanupThread.DONE
    assert v.drive_monitor.enabled
    assert v.__calls__ == [('sync_volume_chain', (drive,), {})]
    assert t.__calls__ == [
        ('update_base_size', (), {}),
        ('tryPivot', (), {}),
        ('teardown_top_volume', (), {})
    ]


def test_cleanup_retry(monkeypatch):
    def recoverable_error(arg):
        raise JobNotReadyError("fake-job-id")

    monkeypatch.setattr(
        FakeCleanupThread, "tryPivot", recoverable_error)

    job = fake_job()
    v = FakeVM()
    t = FakeCleanupThread(
        vm=v, job=job, drive=FakeDrive(), doPivot=True)
    t.start()
    t.join()

    assert t.state == CleanupThread.RETRY
    assert v.drive_monitor.enabled
    assert t.__calls__ == [('update_base_size', (), {})]


def test_cleanup_abort(monkeypatch):
    def unrecoverable_error(arg):
        raise JobUnrecoverableError("fake-job-id", "error")

    monkeypatch.setattr(
        FakeCleanupThread, "tryPivot", unrecoverable_error)

    job = fake_job()
    v = FakeVM()
    t = FakeCleanupThread(
        vm=v, job=job, drive=FakeDrive(), doPivot=True)
    t.start()
    t.join()

    assert t.state == CleanupThread.ABORT
    assert v.drive_monitor.enabled
    assert t.__calls__ == [('update_base_size', (), {})]


class Config:

    def __init__(self, confdir):
        self._confdir = confdir
        self.config = yaml.safe_load(self._load('config.yml'))
        self.xmls = self._load_xmls()

    def _load(self, filename):
        return read_data(os.path.join(self._confdir, filename))

    def _load_xmls(self):
        return read_files(os.path.join(self._confdir, '*.xml'))


class RunningVM(Vm):

    def __init__(self, config):
        self.log = logging.getLogger()
        self.cif = fake.ClientIF()
        self._domain = DomainDescriptor(
            config.xmls["00-before"], xml_source=XmlSource.INITIAL)
        self.id = self._domain.id
        self._md_desc = metadata.Descriptor.from_xml(config.xmls["00-before"])
        self._devices = {
            "disk": [
                storage.Drive(
                    **config.config["drive"],
                    volumeChain=xml_chain(config.xmls["00-before"]),
                    log=self.log)
            ]
        }
        self.conf = self._conf_devices(config)
        self.conf.update({
            "vmId": config.config["vm-id"],
            "xml": config.xmls["00-before"]
        })
        self._external = False  # Used when syncing metadata.
        self._dom = FakeDomain(config)
        self.drive_monitor = FakeDriveMonitor()
        self._confLock = threading.Lock()
        self._drive_merger = DriveMerger(self)

    def _conf_devices(self, config):
        drive = dict(config.config["drive"])
        drive["volumeChain"] = xml_chain(config.xmls["00-before"])
        return {"devices": [drive]}

    def cont(self):
        return response.success()


class FakeDomain:

    def __init__(self, config):
        self.log = logging.getLogger()
        self._id = config.config["vm-id"]
        self.xml = config.xmls["00-before"]
        self.block_jobs = {}
        self._config = config
        self._metadata = ""
        self.aborted = threading.Event()

    def UUIDString(self):
        return self._id

    def setMetadata(self, type, xml, prefix, uri, flags):
        # libvirt's setMetadata will add namespace uri to metadata tags in
        # the domain xml, here we care for volume chain sync after a
        # successful pivot.
        self.metadata = xml

    def XMLDesc(self, flags):
        return self.xml

    def blockCommit(self, drive, base_target, top_target, bandwidth, flags):
        self.block_jobs[drive] = {
            'bandwidth': 0,
            'cur': 0,
            'end': 1024**3,
            'type': libvirt.VIR_DOMAIN_BLOCK_JOB_TYPE_ACTIVE_COMMIT
        }
        # The test should simulate commit-ready once the active commit
        # has done mirroring the volume.
        self.xml = self._config.xmls["01-commit"]

    def blockJobInfo(self, drive, flags=0):
        return self.block_jobs.get(drive, {})

    def blockJobAbort(self, drive, flags):
        if flags == libvirt.VIR_DOMAIN_BLOCK_JOB_ABORT_PIVOT:
            # The test should simulate abort-ready such that the cleanup
            # thread would stop waiting for libvirt's domain xml updated
            # volumes chain after pivot is done.
            self.xml = self._config.xmls["03-abort"]
        else:
            # Aborting without pivot attempt will revert to original dom xml.
            self.xml = self._config.xmls["00-before"]

        self.aborted.set()
        del self.block_jobs[drive]

    def blockInfo(self, drive_name, flags):
        return (1024, 0, 0)


def test_merger_dump_jobs(fake_time):
    config = Config('active-merge')
    vm = RunningVM(config)
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }

    # No jobs yet.

    assert vm._drive_merger.dump_jobs() == {}

    merge_params = config.config["merge_params"]
    job_id = merge_params["jobUUID"]
    vm.merge(**merge_params)

    # Merge was started, new jobs should be in the dump.

    assert vm._drive_merger.dump_jobs() == {
        job_id : {
            "bandwidth": merge_params["bandwidth"],
            "base": merge_params["baseVolUUID"],
            "disk": merge_params["driveSpec"],
            "drive": "sda",
            "state": Job.EXTEND,
            'extend_started': fake_time(),
            "id": job_id,
            "top": merge_params["topVolUUID"],
        }
    }


def test_merger_load_jobs(fake_time):
    config = Config('active-merge')
    vm = RunningVM(config)
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }

    assert vm._drive_merger.dump_jobs() == {}

    # Load jobs, simulating recovery flow.

    merge_params = config.config["merge_params"]
    job_id = merge_params["jobUUID"]

    dumped_jobs = {
        job_id : {
            "bandwidth": merge_params["bandwidth"],
            "base": merge_params["baseVolUUID"],
            "disk": merge_params["driveSpec"],
            "drive": "sda",
            "state": Job.EXTEND,
            "extend_started": fake_time(),
            "id": job_id,
            "top": merge_params["topVolUUID"],
        }
    }

    vm._drive_merger.load_jobs(dumped_jobs)
    assert vm._drive_merger.dump_jobs() == dumped_jobs


def test_active_merge(monkeypatch):
    monkeypatch.setattr(CleanupThread, "WAIT_INTERVAL", 0.01)

    config = Config('active-merge')
    vm = RunningVM(config)
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }
    merge_params = config.config["merge_params"]
    image_id = merge_params["driveSpec"]["imageID"]
    job_id = merge_params["jobUUID"]

    # No active block jobs before calling merge.
    assert vm.query_jobs() == {}

    vm.merge(**merge_params)

    # Merge persists the job with EXTEND state.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.EXTEND

    # Libvit block job was not started yet.
    assert "sda" not in vm._dom.block_jobs

    # Because the libvirt job was not started, report default live info.
    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": "0",
            "drive": "sda",
            "end": "0",
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    # query_jobs() keeps job in EXTEND state.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.EXTEND

    # Merge invokes the volume extend API
    assert len(vm.cif.irs.extend_requests) == 1
    _, vol_info, new_size, extend_callback = vm.cif.irs.extend_requests[0]

    # Simulate base volume extension and invoke the verifying callback.
    base_volume = vm.cif.irs.prepared_volumes[(sd_id, vol_info['volumeID'])]
    base_volume['apparentsize'] = new_size
    extend_callback(vol_info)

    # Extend callback started a commit and persisted the job.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.COMMIT
    assert persisted_job["extend_started"] is None

    # And start a libvit block commit job.
    job = vm._dom.block_jobs["sda"]

    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": str(job["cur"]),
            "drive": "sda",
            "end": str(job["end"]),
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    # query_jobs() keeps job in COMMIT state.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.COMMIT

    # Check block job status while in progress.
    job["cur"] = job["end"] // 2
    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": str(job["cur"]),
            "drive": "sda",
            "end": str(job["end"]),
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    # Check job status when job finished, but before libvirt
    # updated the xml.
    job["cur"] = job["end"]
    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": str(job["cur"]),
            "drive": "sda",
            "end": str(job["end"]),
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    # Simulate completion of backup job - libvirt updates the xml.
    vm._dom.xml = config.xmls["02-commit-ready"]

    # Trigger cleanup and pivot attempt.
    vm.query_jobs()

    # query_jobs() switched job to CLEANUP state.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.CLEANUP

    # Wait for cleanup to abort the block job as part of the pivot attempt.
    aborted = vm._dom.aborted.wait(TIMEOUT)
    assert aborted, "Timeout waiting for blockJobAbort() call"

    # Since the job switched to CLEANUP state, we don't query libvirt live info
    # again, and the job reports the last live info.
    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": str(job["cur"]),
            "drive": "sda",
            "end": str(job["end"]),
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    # Set the abort-ready state after cleanup has called active commit abort.
    vm._dom.xml = config.xmls["04-abort-ready"]

    # Check for cleanup completion.
    wait_for_cleanup(vm)

    # When cleanup finished, job was untracked and jobs were persisted.
    persisted_jobs = parse_jobs(vm)
    assert persisted_jobs == {}

    # The fake domain mocks the setMetadata method and store the input as is,
    # domain xml is not manipulated by the test as xml due to namespacing
    # issues, so we only compare the resulting volume chain both between
    # updated metadata and the expected xml.
    expected_volumes_chain = xml_chain(config.xmls["05-after"])
    assert metadata_chain(vm._dom.metadata) == expected_volumes_chain

    # Top volume gets torn down.
    top_id = merge_params["topVolUUID"]
    assert (sd_id, top_id) not in vm.cif.irs.prepared_volumes

    # Drive volume chain is updated and monitoring is back to enabled.
    drive = vm.getDiskDevices()[0]
    assert drive.volumeChain == expected_volumes_chain
    assert vm.drive_monitor.enabled


def test_internal_merge():
    config = Config('internal-merge')
    vm = RunningVM(config)
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }

    assert vm.query_jobs() == {}

    merge_params = config.config["merge_params"]
    vm.merge(**merge_params)

    # Merge invokes the volume extend API
    assert len(vm.cif.irs.extend_requests) == 1
    _, vol_info, new_size, extend_callback = vm.cif.irs.extend_requests[0]

    # Simulate base volume extension and invoke the verifying callback.
    base_volume = vm.cif.irs.prepared_volumes[(sd_id, vol_info['volumeID'])]
    base_volume['apparentsize'] = new_size
    extend_callback(vol_info)

    # Active jobs after calling merge.
    job_id = merge_params["jobUUID"]
    image_id = merge_params["driveSpec"]["imageID"]
    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": "0",
            "drive": "sda",
            "end": "1073741824",
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    job = vm._dom.blockJobInfo("sda")

    # Check block job status while in progress.
    job["cur"] = job["end"] // 2
    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": str(job["cur"]),
            "drive": "sda",
            "end": "1073741824",
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    # Check job status when job finished, but before libvirt
    # updated the xml.
    job["cur"] = job["end"]
    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": str(job["cur"]),
            "drive": "sda",
            "end": "1073741824",
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    # Simulate job completion:
    # 1. libvirt removes the job.
    # 2. libvirt changes the xml.
    del vm._dom.block_jobs["sda"]
    vm._dom.xml = config.xmls["02-after"]

    # Querying the job when the job has gone should switch the job state to
    # CLEANUP and start a cleanup thread.
    info = vm.query_jobs()

    # Query reports the default status entry before cleanup is done.
    assert info == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": "0",
            "drive": "sda",
            "end": "0",
            "id": job_id,
            "imgUUID": image_id,
            "jobType": "block"
        }
    }

    # Jobs persisted now in COMMIT state.
    assert vm._drive_merger.dump_jobs() == {
        job_id : {
            "bandwidth": merge_params["bandwidth"],
            "base": merge_params["baseVolUUID"],
            "disk": merge_params["driveSpec"],
            "drive": "sda",
            "state": Job.CLEANUP,
            "extend_started": None,
            "id": job_id,
            "top": merge_params["topVolUUID"],
        }
    }

    # Check for cleanup completion.
    wait_for_cleanup(vm)

    # Volumes chain is updated in domain metadata without top volume.
    expected_volumes_chain = xml_chain(config.xmls["02-after"])
    assert metadata_chain(vm._dom.metadata) == expected_volumes_chain

    # Top snapshot is merged into removed snapshot and its volume is torn down.
    top_id = merge_params["topVolUUID"]
    assert (sd_id, top_id) not in vm.cif.irs.prepared_volumes

    drive = vm.getDiskDevices()[0]
    assert drive.volumeChain == expected_volumes_chain
    assert vm.drive_monitor.enabled


def test_extend_timeout():
    config = Config('active-merge')
    vm = RunningVM(config)
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }
    merge_params = config.config["merge_params"]
    job_id = merge_params["jobUUID"]

    vm.merge(**merge_params)

    # Job starts with EXTEND state.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.EXTEND

    # Simulate an extend timeout.
    vm._drive_merger.EXTEND_TIMEOUT = 0.0

    # The next query will abort the job.
    assert vm.query_jobs() == {}

    # Simulate slow extend completing after jobs was untracked.
    _, vol_info, new_size, extend_callback = vm.cif.irs.extend_requests[0]
    extend_callback(vol_info)


def test_merge_cancel_commit():
    config = Config('active-merge')
    vm = RunningVM(config)
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }
    merge_params = config.config["merge_params"]
    job_id = merge_params["jobUUID"]

    assert vm.query_jobs() == {}

    vm.merge(**merge_params)

    # Simulate base volume extension completion.
    _, vol_info, new_size, extend_callback = vm.cif.irs.extend_requests[0]
    base_volume = vm.cif.irs.prepared_volumes[(sd_id, vol_info['volumeID'])]
    base_volume['apparentsize'] = new_size
    extend_callback(vol_info)

    # Job switched to COMMIT state.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.COMMIT

    assert vm.query_jobs() == {
        job_id : {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": "0",
            "drive": "sda",
            "end": "1073741824",
            "id": job_id,
            "imgUUID": merge_params["driveSpec"]["imageID"],
            "jobType": "block"
        }
    }

    # Cancel the block job. This simulates a scenario where a user
    # aborts running block job from virsh.
    vm._dom.blockJobAbort("sda", 0)

    vm.query_jobs()

    # Job switched to CLEANUP state.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.CLEANUP

    # Cleanup is done running.
    wait_for_cleanup(vm)

    # Volume chains state should be as it was before merge.
    assert vm._dom.xml == config.xmls["00-before"]
    expected_volumes_chain = xml_chain(config.xmls["00-before"])
    assert metadata_chain(vm._dom.metadata) == expected_volumes_chain

    # Drive chain is unchanged and monitoring is enabled.
    drive = vm.getDiskDevices()[0]
    assert drive.volumeID == config.config["drive"]["volumeID"]
    assert drive.volumeChain == expected_volumes_chain
    assert vm.drive_monitor.enabled


def test_block_job_info_error(monkeypatch):
    config = Config("internal-merge")
    vm = RunningVM(config)
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }
    merge_params = config.config["merge_params"]
    job_id = merge_params["jobUUID"]

    vm.merge(**merge_params)

    # Simulate base volume extension completion.
    _, vol_info, new_size, extend_callback = vm.cif.irs.extend_requests[0]
    base_volume = vm.cif.irs.prepared_volumes[(sd_id, vol_info['volumeID'])]
    base_volume['apparentsize'] = new_size
    extend_callback(vol_info)

    # Job switched to COMMIT state.
    persisted_job = parse_jobs(vm)[job_id]
    assert persisted_job["state"] == Job.COMMIT

    with monkeypatch.context() as mc:

        # Simulate failing blockJobInfo call.
        def blockJobInfo(*args, **kwargs):
            raise fake.libvirt_error(
                [libvirt.VIR_ERR_INTERNAL_ERROR], "Block job info failed")

        mc.setattr(FakeDomain, "blockJobInfo", blockJobInfo)

        # We cannot get live job info, so we return default values.
        assert vm.query_jobs() == {
            job_id: {
                "bandwidth" : 0,
                "blockJobType": "commit",
                "cur": "0",
                "drive": "sda",
                "end": "0",
                "id": job_id,
                "imgUUID": merge_params["driveSpec"]["imageID"],
                "jobType": "block"
            }
        }

    # Libvirt call succeeds so we return live info from libvit.
    assert vm.query_jobs() == {
        job_id: {
            "bandwidth" : 0,
            "blockJobType": "commit",
            "cur": "0",
            "drive": "sda",
            "end": "1073741824",
            "id": job_id,
            "imgUUID": merge_params["driveSpec"]["imageID"],
            "jobType": "block"
        }
    }


def test_merge_commit_error(monkeypatch):
    def commit_error(*args):
        raise fake.libvirt_error(
            [libvirt.VIR_ERR_INTERNAL_ERROR], "Block commit failed")

    monkeypatch.setattr(FakeDomain, "blockCommit", commit_error)

    config = Config("internal-merge")
    vm = RunningVM(config)
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }
    merge_params = config.config["merge_params"]

    vm.merge(**merge_params)

    # Simulate base volume extension completion.
    _, vol_info, new_size, extend_callback = vm.cif.irs.extend_requests[0]
    base_volume = vm.cif.irs.prepared_volumes[(sd_id, vol_info['volumeID'])]
    base_volume['apparentsize'] = new_size

    # Extend completion trigger failed commit.
    with pytest.raises(exception.MergeFailed):
        extend_callback(vol_info)

    # Job was untracked.
    assert vm.query_jobs() == {}


def test_merge_job_already_exists(monkeypatch):
    config = Config("internal-merge")
    vm = RunningVM(config)
    drive = config.config["drive"]
    sd_id = drive["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }

    # Calling merge twice will fail the second call with same block
    # job already tracked from first call.
    merge_params = config.config["merge_params"]
    vm.merge(**merge_params)
    assert len(vm.query_jobs()) == 1

    with pytest.raises(exception.MergeFailed):
        vm.merge(**merge_params)

    # Existing job is kept.
    assert len(vm.query_jobs()) == 1


def test_merge_base_too_small(monkeypatch):
    config = Config("internal-merge")
    vm = RunningVM(config)
    merge_params = config.config["merge_params"]

    # Ensure that base volume is raw and smaller than top,
    # engine is responsible for extending the raw base volume
    # before merge is called.
    base_vol = config.config["volumes"][merge_params["baseVolUUID"]]
    top_vol = config.config["volumes"][merge_params["topVolUUID"]]
    base_vol["capacity"] = top_vol["capacity"] // 2
    base_vol["format"] = "RAW"
    sd_id = config.config["drive"]["domainID"]
    vm.cif.irs.prepared_volumes = {
        (sd_id, k): v for k, v in config.config["volumes"].items()
    }

    with pytest.raises(exception.DestinationVolumeTooSmall):
        vm.merge(**merge_params)

    assert vm.query_jobs() == {}


def wait_for_cleanup(vm):
    log.info("Waiting for cleanup")

    deadline = time.monotonic() + TIMEOUT
    # Block job monitor excutes updateVmJobs method periodically to update
    # on the status of managed block jobs.
    vm.updateVmJobs()
    while vm.hasVmJobs:
        time.sleep(0.01)
        if time.monotonic() > deadline:
            raise RuntimeError("Timeout waiting for cleanup completion")
        log.info("Updating VM jobs...")
        vm.updateVmJobs()

    log.info("No more jobs")


def xml_chain(xml):
    md = metadata.Descriptor.from_xml(xml)
    with md.device(devtype="disk", name="sda") as dev:
        return dev["volumeChain"]


def parse_jobs(vm):
    """
    Parse jobs persisted in vm metadata.
    """
    root = xmlutils.fromstring(vm._dom.metadata)
    jobs = root.find("./jobs").text
    return json.loads(jobs)


def metadata_chain(xml):
    vm = xmlutils.fromstring(xml)
    nodes = vm.findall("./device/volumeChain/volumeChainNode")
    return [
        {
            "domainID": node.find("./domainID").text,
            "imageID": node.find("./imageID").text,
            "leaseOffset": int(node.find("./leaseOffset").text),
            "leasePath": node.find("./leasePath").text,
            "path": node.find("./path").text,
            "volumeID": node.find("./volumeID").text
        } for node in nodes
    ]


def fake_job():
    return Job(
        id="fake-job-id",
        drive=None,
        disk={"volumeID": "fake-vol"},
        top="fake-vol",
        base=None,
        bandwidth=0,
    )
