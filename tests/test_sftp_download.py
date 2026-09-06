"""Tests for sftp_download_and_process - the download half of the daily job.

Selection is by file name over a bounded window. The two behaviours these
replace both lost data: a date cutoff dropped every wave that arrived after its
delivery was archived, and `sys.exit(3)` on a non-empty incoming folder meant
one bad file stopped the job for good.
"""
import zipfile
from datetime import date, timedelta

import pytest

from scripts import sftp_download_and_process as sftp
from scripts.sftp_download_and_process import (archived_names,
                                              download_sdat_files,
                                              last_archived_date,
                                              quarantine_leftovers)


class FakeFTP:
    """Enough of ftplib.FTP_TLS for the download loop.

    `listing` maps a remote name to its bytes; `dirs` adds directory entries.
    Names in `fail_on` write a few bytes and then raise, which is what a
    connection dropped mid-transfer looks like.
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
            callback(b'trunc')
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

    def install(listing, **kwargs):
        fake = FakeFTP(listing, **kwargs)
        monkeypatch.setattr(sftp, 'FTP_TLS', lambda: fake)
        return fake

    return install


@pytest.fixture
def dirs(tmp_path):
    incoming = tmp_path / 'incoming'
    archive = tmp_path / 'archive'
    incoming.mkdir()
    archive.mkdir()
    return incoming, archive


def name(delivery, time='094500', doc='E66', tag='a'):
    return f'{delivery}_{time}_12X-0000001536-1_{doc}_12X-00000020FW-5_{tag}.xml'


def archive_holding(archive_dir, delivery, *names):
    """Write <delivery>.zip containing the given file names."""
    with zipfile.ZipFile(archive_dir / f'{delivery}.zip', 'w') as zf:
        for n in names:
            zf.writestr(n, 'x')


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
# What we already have
# --------------------------------------------------------------------------

def test_archived_names_reads_the_zips_in_the_window(dirs):
    _, archive = dirs
    archive_holding(archive, '20260807', name('20260807'), name('20260807', tag='b'))
    assert archived_names(archive, date(2026, 7, 31)) == {
        name('20260807'), name('20260807', tag='b')}


def test_archived_names_ignores_zips_before_the_floor(dirs):
    """A zip is named after its files' own prefix, so one older than the floor
    cannot hold a candidate -- and not opening it is what keeps the scan flat."""
    _, archive = dirs
    archive_holding(archive, '20260605', name('20260605'))
    assert archived_names(archive, date(2026, 7, 31)) == set()


def test_archived_names_ignores_zips_without_a_date_name(dirs):
    """A hand-made archive.zip could otherwise suppress real downloads."""
    _, archive = dirs
    archive_holding(archive, 'archive', name('20260807'))
    assert archived_names(archive, date(2026, 1, 1)) == set()


def test_an_unreadable_archive_does_not_abort_the_scan(dirs):
    _, archive = dirs
    (archive / '20260806.zip').write_bytes(b'not a zip')
    archive_holding(archive, '20260807', name('20260807'))
    assert archived_names(archive, date(2026, 7, 31)) == {name('20260807')}


def test_the_archive_scan_stays_bounded_as_history_grows(dirs, monkeypatch):
    """The property that matters operationally: one zip lands per day forever,
    so the cost of a run must not grow with the archive. Asserted on the zips
    actually opened, not on elapsed time."""
    _, archive = dirs
    newest = date(2026, 8, 7)
    for i in range(400):
        day = (newest - timedelta(days=i)).strftime('%Y%m%d')
        archive_holding(archive, day, name(day))

    opened = []
    real = zipfile.ZipFile
    monkeypatch.setattr(zipfile, 'ZipFile',
                        lambda p, *a, **k: (opened.append(p), real(p, *a, **k))[1])

    archived_names(archive, newest - timedelta(days=7))

    assert len(opened) == 8, f'opened {len(opened)} zips out of 400'


# --------------------------------------------------------------------------
# Selecting what to download
# --------------------------------------------------------------------------

def test_a_file_already_in_an_archive_is_not_downloaded(dirs, ftp):
    incoming, archive = dirs
    archive_holding(archive, '20260807', name('20260807'))
    fake = ftp({name('20260807'): b'x'})

    assert download_sdat_files(incoming, archive) == 0
    assert fake.retrieved == []
    assert fake.quit_called


def test_a_late_wave_of_an_archived_day_is_downloaded(dirs, ftp):
    """The regression this commit exists for. Delivery 20260807 arrives in four
    waves; the 09:45 wave is archived, then the 10:45 wave lands. The old date
    cutoff (`> 20260807`) excluded it forever -- 117 files, silently."""
    incoming, archive = dirs
    archive_holding(archive, '20260807', name('20260807', time='094500'))
    late = name('20260807', time='104500', doc='E31', tag='z')
    ftp({name('20260807', time='094500'): b'old', late: b'late wave'})

    assert download_sdat_files(incoming, archive) == 1
    assert [p.name for p in incoming.glob('*.xml')] == [late]


def test_a_quarantined_file_is_downloaded_again(dirs, ftp):
    """failed/ is deliberately not "known": re-fetching is what heals a
    truncated download. A genuinely corrupt file is re-fetched daily, which is a
    warning line rather than a blockage."""
    incoming, archive = dirs
    archive_holding(archive, '20260807', name('20260807'))
    broken = name('20260807', tag='broken')
    (incoming / 'failed').mkdir()
    (incoming / 'failed' / broken).write_text('truncated')
    ftp({broken: b'a good copy this time'})

    assert download_sdat_files(incoming, archive) == 1
    assert (incoming / broken).read_bytes() == b'a good copy this time'


def test_a_file_already_in_incoming_is_not_downloaded(dirs, ftp):
    incoming, archive = dirs
    already = name('20260807')
    (incoming / already).write_text('x')
    fake = ftp({already: b'y'})

    assert download_sdat_files(incoming, archive) == 0
    assert fake.retrieved == []


def test_files_older_than_the_window_are_not_downloaded(dirs, ftp):
    """Without a floor, adopting name-based selection would pull back everything
    the provider still keeps that we never archived."""
    incoming, archive = dirs
    archive_holding(archive, '20260807', name('20260807'))
    ftp({name('20260601'): b'ancient'})

    assert download_sdat_files(incoming, archive, window_days=7) == 0


def test_the_window_length_is_configurable(dirs, ftp):
    incoming, archive = dirs
    archive_holding(archive, '20260807', name('20260807'))
    ftp({name('20260601'): b'old'})

    assert download_sdat_files(incoming, archive, window_days=90) == 1


def test_names_without_a_date_prefix_are_skipped(dirs, ftp):
    incoming, archive = dirs
    ftp({'README.txt': b'x', 'index.html': b'x', name('20260807'): b'ok'})
    assert download_sdat_files(incoming, archive, window_days=365) == 1


def test_directories_are_skipped(dirs, ftp):
    incoming, archive = dirs
    fake = ftp({name('20260807'): b'ok'}, dirs=['20260807_subdir'])
    assert download_sdat_files(incoming, archive, window_days=365) == 1
    assert fake.retrieved == [name('20260807')]


# --------------------------------------------------------------------------
# Transferring
# --------------------------------------------------------------------------

def test_a_failed_transfer_leaves_nothing_behind(dirs, ftp):
    """The download writes to <name>.part and renames, so an interrupted
    transfer cannot leave a truncated XML under its final name. Such a file
    would parse to nothing, be archived anyway, and then mask the good copy
    permanently, because the archive is what "known" is built from."""
    incoming, archive = dirs
    bad = name('20260807', tag='bad')
    ftp({bad: b'the whole file'}, fail_on=[bad])

    assert download_sdat_files(incoming, archive, window_days=365) == 0
    assert list(incoming.iterdir()) == []


def test_one_failed_transfer_does_not_abort_the_rest(dirs, ftp):
    """A single bad file must not cost the delivery: the others still land."""
    incoming, archive = dirs
    bad, good = name('20260807', tag='bad'), name('20260807', tag='good')
    fake = ftp({bad: b'x', good: b'y'}, fail_on=[bad])

    count = download_sdat_files(incoming, archive, window_days=365)

    assert count == 1
    assert (incoming / good).read_bytes() == b'y'
    assert set(fake.retrieved) == {bad, good}


def test_a_connection_failure_is_reported_not_raised(dirs, monkeypatch):
    """The job logs and returns 0 rather than crashing, so the next run retries."""
    incoming, archive = dirs
    monkeypatch.setenv('SFTP_HOST', 'ftp.example.test')
    monkeypatch.setenv('SFTP_PORT', '21')
    monkeypatch.setattr(sftp, 'read_secret', lambda n: 'x')

    class Unreachable(FakeFTP):
        def __init__(self):
            super().__init__({})

        def connect(self, *a, **k):
            raise OSError('no route to host')

    monkeypatch.setattr(sftp, 'FTP_TLS', Unreachable)
    assert download_sdat_files(incoming, archive) == 0


def test_a_missing_credential_is_fatal(dirs, monkeypatch):
    incoming, archive = dirs
    monkeypatch.delenv('SFTP_HOST', raising=False)
    with pytest.raises(ValueError, match='SFTP_HOST'):
        download_sdat_files(incoming, archive)


# --------------------------------------------------------------------------
# Leftovers from the previous run
# --------------------------------------------------------------------------

def test_leftovers_are_moved_to_failed(dirs):
    incoming, _ = dirs
    leftover = incoming / name('20260807')
    leftover.write_text('unparseable')

    assert quarantine_leftovers(incoming) == 1
    assert not leftover.exists()
    assert (incoming / 'failed' / leftover.name).read_text() == 'unparseable'


def test_quarantining_the_same_name_twice_overwrites(dirs):
    """The same file failing again must not raise: it is the same file."""
    incoming, _ = dirs
    (incoming / name('20260807')).write_text('first')
    quarantine_leftovers(incoming)
    (incoming / name('20260807')).write_text('second')
    quarantine_leftovers(incoming)

    assert (incoming / 'failed' / name('20260807')).read_text() == 'second'


def test_an_empty_incoming_creates_no_failed_directory(dirs):
    incoming, _ = dirs
    assert quarantine_leftovers(incoming) == 0
    assert not (incoming / 'failed').exists()


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------

@pytest.fixture
def job(dirs, monkeypatch):
    """Point main() at temp folders instead of /data and /app/config."""
    incoming, archive = dirs
    config = {'processing': {'incoming_path': str(incoming),
                             'archive_path': str(archive),
                             'download_window_days': 7}}
    monkeypatch.setattr(sftp, 'load_config', lambda path: config)
    return incoming, archive


def test_main_quarantines_leftovers_and_keeps_going(job, monkeypatch):
    """What replaces sys.exit(3). Aborting meant one unparseable file stopped
    every later delivery: nothing downloaded, so the file kept "for retry" had
    no next delivery to be retried with."""
    incoming, _ = job
    leftover = incoming / name('20260807')
    leftover.write_text('unparseable')
    seen = {}

    def fake_download(*args, **kwargs):
        seen['downloaded'] = True
        return 0

    monkeypatch.setattr(sftp, 'download_sdat_files', fake_download)
    sftp.main()

    assert seen.get('downloaded'), 'the download must run despite the leftover'
    assert (incoming / 'failed' / leftover.name).exists()


def test_main_does_not_process_when_nothing_was_downloaded(job, monkeypatch):
    monkeypatch.setattr(sftp, 'download_sdat_files', lambda *a, **k: 0)
    monkeypatch.setattr(sftp.SDATProcessor, 'from_config',
                        classmethod(lambda cls, cfg: pytest.fail(
                            'must not process an empty download')))
    sftp.main()


def test_main_processes_what_it_downloaded(job, monkeypatch):
    incoming, archive = job
    seen = {}

    def fake_download(target_dir, archive_dir, window_days):
        seen.update(target=target_dir, archive=archive_dir, window=window_days)
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

    assert (seen['target'], seen['archive'], seen['window']) == (incoming, archive, 7)
    assert seen['processed']
