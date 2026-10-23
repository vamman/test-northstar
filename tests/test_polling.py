"""Persisted wall-time schedules: fairness, evidence, leases, recovery and retirement."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import os
from pathlib import Path
import sys
import tempfile
import threading
from uuid import uuid4

if 'app.db' not in sys.modules:
    _DATABASE_DIR=tempfile.TemporaryDirectory(prefix='northstar-transit-polling-tests-')
    os.environ['DATABASE_URL']='sqlite:///'+str(Path(_DATABASE_DIR.name)/'polling.sqlite')

import pytest
from sqlalchemy import func,select

from app import db,polling,services

NOW=datetime(2026,9,28,15,0,tzinfo=timezone.utc)
REMOTE='11111111-1111-4111-8111-111111111111'
EMS_A='22222222-2222-4222-8222-222222222222'
EMS_B='33333333-3333-4333-8333-333333333333'


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    path=Path(db.engine.url.database)
    assert db.engine.dialect.name=='sqlite' and path.parent.name.startswith('northstar-transit-') and 'tests-' in path.parent.name
    db.Base.metadata.drop_all(db.engine);db.init_db()
    monkeypatch.setattr(db,'now',lambda:NOW)


def job(ident='job-1',age=0):
    with db.Session.begin() as session:
        session.add(db.Outbox(id=ident,run_id='test-run',payload={'identity':ident},body_hash='a'*64,
                             status='sent',remote_incident_id=REMOTE,created_at=NOW-timedelta(seconds=age),
                             sent_at=NOW-timedelta(seconds=age)))
    return ident


def reports(ident='job-1',status='on_scene',ems=(),paused=False,errors=None,known=True):
    assignments=[{'assignment_id':f'assignment-{index}','agency':'ems','remote_id':remote,
                  'status':'completed' if completed else 'assigned','remote_status':'completed' if completed else 'transporting'}
                 for index,(remote,completed) in enumerate(ems)]
    detail={'scope':'command_incident','remote_status':status,'command_status':status,
            'assignments_known':known,'assignments':assignments,'command_paused':paused}
    if errors:detail['observation_errors']=errors
    result=[{'source':'911','external_id':str(uuid4()),'incident_id':ident,'status':status,'details':detail}]
    for remote,completed in ems:
        result.append({'source':'hospital','external_id':str(uuid4()),'incident_id':ident,
                       'status':'resolved' if completed else 'transporting',
                       'details':{'scope':'ems_journey','remote_incident_id':remote,
                                  'remote_status':'completed' if completed else 'transporting',
                                  'patient_id':remote+':patient','completed_at':'2026-09-28T12:00:00Z' if completed else None}})
    return result


def poll(ident='job-1'):
    with db.Session() as session:return session.get(db.FeedbackPoll,ident)


def at(seconds):return NOW+timedelta(seconds=seconds)
def seconds_from_now(value):return (polling._utc(value)-NOW).total_seconds()


def test_claim_is_bounded_and_active_observations_do_not_repeat_before_due():
    for index in range(10):job(f'job-{index}')
    claims=polling.claim_feedback(now=NOW)
    assert len(claims)==4
    first=claims[0]
    polling.complete_feedback(first,reports=reports(first['id']),now=NOW)
    assert poll(first['id']).details['interval_seconds']==30
    assert all(c['id']!=first['id'] for c in polling.claim_feedback(now=at(29)))
    due=polling.claim_feedback(now=at(30))
    assert first['id'] in [c['id'] for c in due]
    assert len(due)<=4


def test_unchanged_and_paused_observations_back_off_without_source_clock_dependence():
    job();observation=reports()
    for current,interval in [(0,30),(30,60),(90,90),(180,120)]:
        claim=polling.claim_feedback(now=at(current))[0]
        polling.complete_feedback(claim,reports=observation,now=at(current))
        assert poll().details['interval_seconds']==interval
    claim=polling.claim_feedback(now=at(300))[0]
    changed=reports(status='transporting',paused=True)
    polling.complete_feedback(claim,reports=changed,now=at(300))
    assert poll().details['interval_seconds']==120
    assert poll().state=='active'


def test_errors_use_persisted_exponential_backoff_and_success_resets_it():
    job();elapsed=0
    for failures,interval in enumerate((30,60,120,240,480,900,900),1):
        claim=polling.claim_feedback(now=at(elapsed))[0]
        assert polling.complete_feedback(claim,error='HTTP unavailable',now=at(elapsed))
        row=poll()
        assert row.state=='backoff' and row.failures==failures and row.details['interval_seconds']==interval
        assert polling.claim_feedback(now=at(elapsed+interval-1))==[]
        elapsed+=interval
    claim=polling.claim_feedback(now=at(elapsed))[0]
    polling.complete_feedback(claim,reports=reports(),now=at(elapsed))
    assert poll().failures==0 and poll().last_error is None and poll().state=='active'


def test_terminal_grace_retires_and_stops_polling_forever_until_woken():
    job();terminal=reports(status='resolved',ems=((EMS_A,True),))
    claim=polling.claim_feedback(now=NOW)[0];polling.complete_feedback(claim,reports=terminal,now=NOW)
    assert poll().state=='grace' and poll().details['interval_seconds']==120
    for instant in (120,240,360,480,600,720,840,900):
        claim=polling.claim_feedback(now=at(instant))[0]
        polling.complete_feedback(claim,reports=terminal,now=at(instant))
    row=poll();assert row.state=='retired' and seconds_from_now(row.terminal_since)==0
    assert polling.claim_feedback(now=at(86400))==[]
    with db.Session() as session:
        outbox=session.get(db.Outbox,'job-1')
        assert outbox.status=='sent' and outbox.payload=={'identity':'job-1'} and outbox.body_hash=='a'*64


def test_late_ems_resets_grace_and_waits_for_positive_completion():
    job();initial=reports(status='resolved',ems=((EMS_A,True),))
    claim=polling.claim_feedback(now=NOW)[0];polling.complete_feedback(claim,reports=initial,now=NOW)
    claim=polling.claim_feedback(now=at(120))[0]
    pending=reports(status='resolved',ems=((EMS_A,True),(EMS_B,False)))
    polling.complete_feedback(claim,reports=pending,now=at(120))
    assert poll().state=='active' and poll().terminal_since is None
    assert poll().details['pending_ems_count']==1
    claim=polling.claim_feedback(now=at(150))[0]
    complete=reports(status='resolved',ems=((EMS_A,True),(EMS_B,True)))
    polling.complete_feedback(claim,reports=complete,now=at(150))
    assert poll().state=='grace' and seconds_from_now(poll().terminal_since)==150
    # A newly discovered already-completed assignment is still a meaningful change.
    third=reports(status='resolved',ems=((EMS_A,True),(EMS_B,True),(REMOTE,True)))
    claim=polling.claim_feedback(now=at(270))[0];polling.complete_feedback(claim,reports=third,now=at(270))
    assert seconds_from_now(poll().terminal_since)==270


def test_incomplete_callback_or_error_evidence_cannot_retire_a_poll():
    callback=[{'source':'911','status':'resolved','details':{}}]
    assert not polling.observation_evidence(callback)['terminal']
    malformed=reports(status='resolved');malformed[0]['details']['assignments']=[{}]
    assert not polling.observation_evidence(malformed)['terminal']
    assert not polling.observation_evidence(reports(status='resolved',known=False))['terminal']
    assert polling.observation_evidence(reports(status='resolved',ems=()))['terminal']
    missing=reports(status='resolved');missing[0]['details']['assignments']=[{'agency':'ems','remote_id':None}]
    assert not polling.observation_evidence(missing)['terminal']
    pending=reports(status='resolved',ems=((EMS_A,False),))
    assert not polling.observation_evidence(pending)['terminal']
    terminal=reports(status='resolved',ems=((EMS_A,True),),errors=[{'error':'Health unreachable'}])
    job();claim=polling.claim_feedback(now=NOW)[0]
    polling.complete_feedback(claim,reports=terminal,now=NOW)
    assert poll().state=='backoff' and poll().terminal_since is None


def test_concurrent_claims_are_disjoint_and_expired_leases_recover():
    for i in range(8):job(f'job-{i}')
    barrier=threading.Barrier(2)
    def claim():
        barrier.wait()
        return polling.claim_feedback(now=NOW)
    with ThreadPoolExecutor(max_workers=2) as executor:
        a,b=list(executor.map(lambda _:claim(),range(2)))
    assert len(a)==len(b)==4
    assert not {c['id'] for c in a}&{c['id'] for c in b}
    assert polling.claim_feedback(now=at(119))==[]
    recovered=polling.claim_feedback(now=at(121))
    assert len(recovered)==4
    stale=next(c for c in a+b if c['id']==recovered[0]['id'])
    assert polling.complete_feedback(stale,reports=reports(stale['id']),now=at(122)) is False
    assert polling.renew_feedback(stale,now=at(122)) is False
    assert polling.renew_feedback(recovered[0],now=at(122)) is True


def test_racing_callback_preserves_due_wake_and_bypasses_terminal_cache():
    job();claim=polling.claim_feedback(now=NOW)[0]
    with db.Session.begin() as session:services.wake_feedback(session,'job-1')
    polling.complete_feedback(claim,reports=reports(status='resolved'),now=at(5))
    assert poll().state=='active' and poll().terminal_since is None
    assert poll().details['interval_seconds']==0
    new=polling.claim_feedback(now=at(5))[0]
    assert new['force_refresh'] is True
    polling.complete_feedback(new,reports=reports(),now=at(5))
    next_claim=polling.claim_feedback(now=at(35))[0]
    assert next_claim['force_refresh'] is False


def test_fresh_work_and_old_backlog_both_receive_bounded_claims():
    for i in range(20):job(f'old-{i}',age=86400+i)
    for i in range(10):job(f'new-{i}',age=i)
    claims=polling.claim_feedback(now=NOW)
    assert len(claims)==4
    assert sum(c['id'].startswith('new-') for c in claims)==3
    assert sum(c['id'].startswith('old-') for c in claims)==1
    with db.Session() as session:
        assert session.scalar(select(func.count()).select_from(db.FeedbackPoll))==polling.BOOTSTRAP_BATCH
    for claim in claims:polling.complete_feedback(claim,reports=reports(claim['id'],status='resolved'),now=NOW)
    second=polling.claim_feedback(now=at(5))
    assert len(second)==4 and any(c['id'].startswith('old-') for c in second)
    assert not {c['id'] for c in claims}&{c['id'] for c in second}


def test_legacy_positive_history_retires_after_one_fresh_matching_catchup():
    job(age=86400)
    legacy=reports(status='resolved',ems=((EMS_A,True),))
    legacy[0]['details'].pop('assignments_known');legacy[0]['details'].pop('command_status')
    with db.Session.begin() as session:
        for report in legacy:
            session.add(db.Report(id=str(uuid4()),source=report['source'],external_id=report['external_id'],
                         incident_id='job-1',body_hash='b'*64,payload=report,observed_at=NOW-timedelta(hours=2)))
    claim=polling.claim_feedback(now=NOW)[0]
    assert poll().details['bootstrap_from_history'] is True
    assert poll().state=='grace' and seconds_from_now(poll().terminal_since)==-7200
    assert polling.claim_feedback(now=at(1))==[]
    current=reports(status='resolved',ems=((EMS_A,True),))
    polling.complete_feedback(claim,reports=current,now=at(1))
    assert poll().state=='retired'
    assert polling.claim_feedback(now=at(86400))==[]


def test_history_without_valid_assignments_requires_new_full_grace():
    job(age=86400)
    report=reports(status='resolved')[0];report['details'].pop('assignments')
    with db.Session.begin() as session:
        session.add(db.Report(id=str(uuid4()),source='911',external_id=report['external_id'],incident_id='job-1',
                             body_hash='b'*64,payload=report,observed_at=NOW-timedelta(hours=2)))
    claim=polling.claim_feedback(now=NOW)[0]
    assert poll().state=='active' and poll().terminal_since is None
    polling.complete_feedback(claim,reports=reports(status='resolved'),now=NOW)
    assert poll().state=='grace' and seconds_from_now(poll().terminal_since)==0
