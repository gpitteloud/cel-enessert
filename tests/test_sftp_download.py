"""Tests for sftp_download_and_process - the download half of the daily job.

Written BEFORE the download path is fixed, so these pin today's behaviour,
including the two defects that fix has to address:

  * the window is `file_date > last_archived_date()`, so once a day is archived
    every later-arriving file of that same day is excluded forever;
  * a failed RETR leaves a partial file at its final name.

Both are marked `..._today` and are expected to be rewritten.
"""
from datetime import date

import pytest

from scripts import sftp_download_and_process as sftp
from scripts.sftp_download_and_process import (download_sdat_files,
                                               last_archived_date)


class FakeFTP:
    """Enough of ftplib.FTP_TLS for the download loop.

    `listing` maps a remote name to its bytes; `entries` may add directories.
    Names in `fail_on` raise on RETR, which is how a dropped transfer looks.
    """

    def __init__(self, listing, dirs=(), fail_on=()):
        self.listing = listing
        self.dirs = list(dirs)
        self.fail_on = set(fail_on)
        self.retrieved = []
        self.quit_called = False

    def connect(self, host, port, timeout=None):
        self.host = host

    def login(self, user=None, passwd=None):
        self.user = user

    def prot_p(self):
        pass

    def mlsd(self):
        for name in self.dirs:
            yield name, {'type': 'dir'}
        for name in self.listing:
            yield name, {'type': 'file'}

    def retrbinary(self, command, callback):
        name = command[len('RETR '):]
        self.retrieved.append(name)
        if name in self.fail_on:
            callback(b'trunc')          # a partial transfer, then the failure
            raise OSError('connection reset mid-transfer')
        callback(self.listing[name])

    def quit(self):
        self.quit_called = True


@pytest.fixture
def ftp(monkeypatch):
    """Install a FakeFTP and the credentials the download needs."""
    monkeypatch.setenv('SFTP_HOST', 'ftp.example.test')
    monkeypatch.setenv('SFTP_PORT', '21')
    monkeypatch.setattr(sftp, 'read_secret', lambda name: f'fake-{name}')

    holder = {}

    def install(listing, **kwargs):
        holder['ftp'] = FakeFTP(listing, **kwargs)
        monkeypatch.setattr(sftp, 'FTP_TLS', lambda: holder['ftp'])
        return holder['ftp']

    return install


def name(delivery, tag='a', doc='E66'):
    return f'{delivery}_094500_12X-0000001536-1_{doc}_12X-00000020FW-5_{tag}.xml'


# --------------------------------------------------------------------------
# Where the window starts
# --------------------------------------------------------------------------

def test_last_archived_date_is_the_newest_zip(tmp_path):
    for day in ('20260605', '20260807', '20260618'):
        (tmp_path / f'{day}.zip').write_bytes(b'')
    assert last_archived_date(tmp_path) == date(2026, 8, 7)


def test_last_archived_date_without_archives_is_the_epoch(tmp_path):
    """The bootstrap value; it bounds the first ever download."""
    assert last_archived_date(tmp_path) == date(2026, 1, 1)


# --------------------------------------------------------------------------
# Selecting what to download
# --------------------------------------------------------------------------

def test_only_files_after_the_cutoff_are_downloaded(tmp_path, ftp):
    fake = ftp({name('20260605'): b'old', name('20260607'): b'new'})
    count = download_sdat_files(tmp_path, date(2026, 6, 6))

    assert count == 1
    assert [p.name for p in tmp_path.glob('*.xml')] == [name('20260607')]
    assert fake.quit_called


def test_a_file_from_the_archived_day_is_never_downloaded_today(tmp_path, ftp):
    """The defect. Delivery 20260807 arrives in four waves; once the 09:45 wave
    is archived, `> 20260807` excludes the 10:45 wave permanently -- 117 files
    including the monthly E31 aggregates, lost with no error."""
    late = f'20260807_104500_12X-0000001536-1_E31_12X-00000020FW-5_z.xml'
    ftp({late: b'late wave'})

    assert download_sdat_files(tmp_path, date(2026, 8, 7)) == 0
    assert list(tmp_path.glob('*.xml')) == []


def test_names_without_a_date_prefix_are_skipped(tmp_path, ftp):
    ftp({'README.txt': b'x', 'index.html': b'x', name('20260607'): b'ok'})
    assert download_sdat_files(tmp_path, date(2026, 6, 1)) == 1


def test_directories_are_skipped(tmp_path, ftp):
    fake = ftp({name('20260607'): b'ok'}, dirs=['20260607_subdir'])
    assert download_sdat_files(tmp_path, date(2026, 6, 1)) == 1
    assert fake.retrieved == [name('20260607')]


def test_one_failed_transfer_does_not_abort_the_rest(tmp_path, ftp):
    """A single bad file must not cost the delivery: the others still land."""
    bad, good = name('20260607', 'bad'), name('20260607', 'good')
    fake = ftp({bad: b'x', good: b'y'}, fail_on=[bad])

    count = download_sdat_files(tmp_path, date(2026, 6, 1))

    assert count == 1
    assert (tmp_path / good).read_bytes() == b'y'
    assert set(fake.retrieved) == {bad, good}


def test_a_failed_transfer_leaves_a_partial_file_today(tmp_path, ftp):
    """The download writes straight to the final name, so a dropped connection
    leaves a truncated XML that later parses to nothing. Two 0-byte files in the
    corpus look exactly like this. The fix downloads to `.part` and renames."""
    bad = name('20260607', 'bad')
    ftp({bad: b'the whole file'}, fail_on=[bad])

    download_sdat_files(tmp_path, date(2026, 6, 1))

    assert (tmp_path / bad).read_bytes() == b'trunc'


def test_a_connection_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """The job logs and returns 0 rather than crashing, so the scheduler sees a
    clean run and the next one retries."""
    monkeypatch.setenv('SFTP_HOST', 'ftp.example.test')
    monkeypatch.setenv('SFTP_PORT', '21')
    monkeypatch.setattr(sftp, 'read_secret', lambda n: 'x')

    class Unreachable(FakeFTP):
        def __init__(self):
            super().__init__({})

        def connect(self, *a, **k):
            raise OSError('no route to host')

    monkeypatch.setattr(sftp, 'FTP_TLS', Unreachable)
    assert download_sdat_files(tmp_path, date(2026, 6, 1)) == 0


def test_a_missing_credential_is_fatal(tmp_path, monkeypatch):
    monkeypatch.delenv('SFTP_HOST', raising=False)
    with pytest.raises(ValueError, match='SFTP_HOST'):
        download_sdat_files(tmp_path, date(2026, 6, 1))


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------

@pytest.fixture
def job_dirs(tmp_path, monkeypatch):
    """Point main() at temp folders instead of /data and /app/config."""
    incoming = tmp_path / 'incoming'
    archive = tmp_path / 'archive'
    incoming.mkdir()
    archive.mkdir()
    config = {'processing': {'incoming_path': str(incoming),
                             'archive_path': str(archive)}}
    monkeypatch.setattr(sftp, 'load_config', lambda path: config)
    return incoming, archive


def test_main_aborts_when_incoming_is_not_empty_today(job_dirs, monkeypatch):
    """The defect that wedges the job: one unparseable file left behind means
    the next day downloads nothing and processes nothing, forever. The fix moves
    leftovers to failed/ and carries on."""
    incoming, _ = job_dirs
    (incoming / name('20260607')).write_text('leftover')
    monkeypatch.setattr(sftp, 'download_sdat_files',
                        lambda *a: pytest.fail('must not download'))

    with pytest.raises(SystemExit) as exit_info:
        sftp.main()
    assert exit_info.value.code == 3


def test_main_does_not_process_when_nothing_was_downloaded(job_dirs, monkeypatch):
    monkeypatch.setattr(sftp, 'download_sdat_files', lambda *a: 0)
    monkeypatch.setattr(sftp.SDATProcessor, 'from_config',
                        classmethod(lambda cls, cfg: pytest.fail(
                            'must not process an empty download')))
    sftp.main()


def test_main_processes_what_it_downloaded(job_dirs, monkeypatch):
    incoming, archive = job_dirs
    (archive / '20260605.zip').write_bytes(b'')
    seen = {}

    def fake_download(target_dir, after_date):
        seen['target'] = target_dir
        seen['after'] = after_date
        return 3

    class FakeProcessor:
        @classmethod
        def from_config(cls, config):
            return cls()

        def process_sdat_files(self):
            seen['processed'] = True

    monkeypatch.setattr(sftp, 'download_sdat_files', fake_download)
    monkeypatch.setattr(sftp, 'SDATProcessor', FakeProcessor)

    sftp.main()

    assert seen['target'] == incoming
    assert seen['after'] == date(2026, 6, 5), 'the window starts at the newest archive'
    assert seen['processed']
