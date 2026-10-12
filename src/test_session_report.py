from datetime import datetime
from fits_metadata_extractor import FITSMetadata
from session_report import group_sessions, find_close_pairs


def m(folder, ts, target='M 31', device='SEESTAR_S50', ftype='LIGHT', valid=True):
    return FITSMetadata(file_path=f'/src/2025/{folder}/f_{ts}.fit', file_name='f.fit',
                        device_type=device, frame_type=ftype, object_name=target,
                        observation_date=ts, observation_year=2025, is_valid=valid)


def test_two_nights_two_sessions():
    r = [m('n1', '2025-08-25T22:00:00'), m('n1', '2025-08-26T01:00:00'),
         m('n2', '2025-09-02T22:00:00')]
    s = group_sessions(r)[('2025', 'M 31', 'SEESTAR_S50')]
    assert [x['frames'] for x in s] == [2, 1]


def test_session_crossing_midnight_stays_one():
    r = [m('n1', '2025-08-25T23:50:00'), m('n1', '2025-08-26T00:10:00')]
    assert len(group_sessions(r)[('2025', 'M 31', 'SEESTAR_S50')]) == 1


def test_devices_separate():
    r = [m('a', '2025-08-25T22:00:00'), m('b', '2025-08-25T22:00:00', device='DWARF_3')]
    assert len(group_sessions(r)) == 2


def test_only_lights_and_valid():
    r = [m('a', '2025-08-25T22:00:00', ftype='DARK'),
         m('a', '2025-08-25T22:00:00', valid=False)]
    assert group_sessions(r) == {}


def test_target_filter():
    r = [m('a', '2025-08-25T22:00:00'), m('b', '2025-08-25T22:00:00', target='M 57')]
    assert list(group_sessions(r, 'm 31')) == [('2025', 'M 31', 'SEESTAR_S50')]


def test_same_night_split_across_folders_flagged():
    r = [m('M 31', '2025-08-25T22:00:00'), m('M 31_sub', '2025-08-25T22:05:00')]
    s = group_sessions(r)[('2025', 'M 31', 'SEESTAR_S50')]
    assert find_close_pairs(s) == [(0, 1)]


def test_separate_nights_not_flagged():
    r = [m('n1', '2025-08-25T22:00:00'), m('n2', '2025-09-02T22:00:00')]
    s = group_sessions(r)[('2025', 'M 31', 'SEESTAR_S50')]
    assert find_close_pairs(s) == []
