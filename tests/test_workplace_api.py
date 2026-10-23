"""Workplace registration against the existing disposable API fixture."""
import copy
import json

import pytest
from sqlalchemy import func, select

from test_api import client, CONTROL, REPORT
from app import commutes, db, workplaces


def body(**changes):
    return {'schema_version':'1.0','synthetic':True,'source':'northstar_office','external_id':'roster-aurora-v1-1',
        'source_sim_time':'2026-10-01T09:00:00-03:00',
        'workplace':{'id':'aurora','name':'Aurora Technology Centre','location':{'crs':'northstar-local-m-v1','units':'m','x':2700,'y':9000,'district_id':'central'},
            'floor_count':18,'worker_count':3200,'business_count':1,'tenancy_type':'single_tenant',
            'businesses':[{'id':'aurora-tech','name':'Aurora Technology','sector':'technology','worker_count':3200,'floors':[1,2,3]}]},
        'workers':[{'worker_id':'aurora-worker-1','home_place_id':'cedar_terrace','business_id':'aurora-tech',
            'work_mode':'office','commute_mode':'car','shift_start':'08:00','shift_end':'17:00'}], **changes}


def submit(client,payload=None,headers=REPORT):
    return client.post('/api/integrations/workplaces',json=payload or body(),headers=headers)


def test_report_scope_registration_preserves_existing_people_and_clock(client):
    before=client.get('/api/dashboard').json()
    assert submit(client,headers={}).status_code==401
    assert submit(client,headers=CONTROL).status_code==401
    response=submit(client);assert response.status_code==201,response.text
    after=client.get('/api/dashboard').json()
    assert before['people']==after['people'] and before['clock']==after['clock']
    assert response.json()['registered_workers']==1
    assert response.json()['source_sim_time'].startswith('2026-10-01')
    assert response.json()['transit_sim_time'].startswith('2026-07-18')
    assert client.get('/api/workplaces').json()['items'][0]['businesses'][0]['name']=='Aurora Technology'


def test_replay_is_immutable_and_new_chunks_are_additive(client):
    first=submit(client);duplicate=submit(client)
    assert first.status_code==201 and duplicate.status_code==200 and duplicate.json()['duplicate']
    changed=body();changed['workers'][0]['shift_start']='09:00'
    assert submit(client,changed).status_code==409
    changed['external_id']='roster-aurora-v1-2';changed['workers'][0]['worker_id']='aurora-worker-2'
    assert submit(client,changed).status_code==201
    assert client.get('/api/workplaces').json()['items'][0]['registered_workers']==2
    with db.Session() as session:
        assert session.scalar(select(func.count()).select_from(db.OfficeWorker))==2
        report=session.scalar(select(db.Report).where(db.Report.external_id=='roster-aurora-v1-1'))
        assert 'workers' not in report.payload # public receipt stays small


def test_new_revision_updates_roster_without_recreating_workers(client):
    submit(client)
    changed=body(external_id='v2');changed['workers'][0]['shift_start']='09:00'
    assert submit(client,changed).status_code==201
    with db.Session() as session:
        worker=session.get(db.OfficeWorker,'aurora-worker-1')
        assert worker.payload['shift_start']=='09:00'
        assert session.scalar(select(func.count()).select_from(db.OfficeWorker))==1


@pytest.mark.parametrize('change',[
    {'synthetic':False},{'synthetic':1},{'source':'somewhere_else'},{'schema_version':'2.0'},
    {'source_sim_time':'2026-10-01T09:00:00'},
    {'workers':[{'worker_id':'w','home_place_id':'unknown'}]},
    {'workers':[{'worker_id':'w','home_place_id':'cedar_terrace','shift_start':'25:00'}]},
    {'workers':[{'worker_id':'w','home_place_id':'cedar_terrace','shift_days':[True]}]},
    {'workers':[{'worker_id':'w','home_place_id':'cedar_terrace','roster_rotation':'random'}]},
    {'workers':[{'worker_id':'w','home_place_id':'cedar_terrace','commute_mode':'teleport'}]},
])
def test_invalid_registration_is_rejected_atomically(client,change):
    assert submit(client,body(**change)).status_code==422
    assert client.get('/api/workplaces').json()['total']==0


def test_chunk_limit_duplicate_ids_and_bad_canonical_location(client):
    too_many=body(workers=[{'worker_id':f'w{i}','home_place_id':'cedar_terrace'} for i in range(501)])
    assert submit(client,too_many).status_code==422
    payload=body();payload['workers']*=2
    assert submit(client,payload).status_code==422
    payload=body();payload['workplace']['location']['district_id']='rural_north'
    assert submit(client,payload).status_code==422
    assert client.get('/api/workplaces').json()['total']==0


def test_location_and_worker_identity_cannot_silently_move(client):
    submit(client)
    payload=body(external_id='relocated');payload['workplace']['location']['x']=2800
    assert submit(client,payload).status_code==409
    payload=body(external_id='new-building');payload['workplace']['id']='another'
    assert submit(client,payload).status_code==409
    assert client.get('/api/workplaces').json()['total']==1


def test_date_jump_archives_trips_and_retains_registered_definitions(client):
    submit(client)
    with db.state_transaction() as (session,row,state):
        definitions=workplaces.registry(session,state)
        commutes.prepare(state,definitions)
        assert state['commute_workplaces']
        oldrun=row.run_id
    response=client.post('/api/scenarios/jump',json={'date':'2026-11-02'},headers=CONTROL)
    assert response.status_code==200
    with db.Session() as session:
        row=session.get(db.Simulation,1)
        definitions=workplaces.registry(session,row.payload)
        assert 'aurora-worker-1' in definitions['workers']
        assert session.get(db.ArchivedRun,oldrun)
        assert row.payload['workplace_revision']>0
    assert client.get('/api/workplaces').json()['items'][0]['source_sim_time'].startswith('2026-10-01')


def test_home_catalog_rural_destinations_and_read_only_observations(client):
    catalog=client.get('/api/commutes/catalog').json()
    assert any(p['district_id']=='rural_north' for p in catalog['home_places'])
    payload=body();payload['workplace']['location'].update(x=-15000,y=42000,district_id='rural_north')
    assert submit(client,payload).status_code==201
    city=client.get('/api/city').json()
    destination=next(p for p in city['places'] if p['id']=='office:aurora')
    assert destination['y']==42000
    assert city['bounds']['max_y']>42000
    assert len(city['routes'])==8
    with db.Session() as session:
        before=copy.deepcopy(session.get(db.Simulation,1).payload)
    snapshot=client.get('/api/commutes?workplace_id=aurora').json()
    assert snapshot['summary']['office_occupancy_policy']=='independent_source_schedule'
    with db.Session() as session:
        assert session.get(db.Simulation,1).payload==before
