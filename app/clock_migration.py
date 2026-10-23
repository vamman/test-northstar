"""Explicit, audited clock cutover. Run only with every application writer stopped.

Historical tables and transport envelopes are outside the allowlist. Active
operational schedules are rebased by their remaining duration, with original
values retained in northstar_clock_audit. No elapsed work/pay is fabricated.
"""
import argparse
import copy
import json
from datetime import datetime, timezone, timedelta
from sqlalchemy import Table, MetaData, Column, String, JSON, DateTime, select, insert, update, text
from app.northstar_clock import timestamp, iso

TERMINAL = {'closed','completed','complete','cancelled','canceled','discharged','resolved','published','rejected','withdrawn','archived','released','graduated','denied','delivered','recovered','final','done'}
# These subtrees contain historical or upstream evidence, never local schedules.
KEEP = {'history','daily_trend','trend','closure_history','stage_history','chain','welfare_checks','transcript',
        'notes','pathway','feedback','last_feedback','environment','weather_response_outcomes','peers','observations',
        'commute_history','commute_leg_history','commute_contacts','commute_workplaces','source','source_clock',
        'source_sim_time','remote_simulated_at','source_simulated_at','remote_sim_time','observed_at',
        'recorded_at','worker_at','last_tick_at','heartbeat_at','worker_heartbeat_at','worker_last_seen_at',
        'last_advance_at','received_wall_at','created_wall_at','installed_at','introduced_at','upgrades',
        'commute_registration','external_request','request','response','payload','body','raw_payload',
        'published_at','date_of_birth','birth_date','tracking_started_at'}


def shift_json(value, offset, key=''):
    if key in KEEP or key.startswith(('remote_', 'source_', 'observed_', 'received_wall', 'recorded_')):
        return copy.deepcopy(value)
    if isinstance(value, dict):
        if (isinstance(value.get('status'), str) and value['status'] in TERMINAL) or any(value.get(k) for k in ('completed_at','closed_at','resolved_at')):
            return copy.deepcopy(value)
        return {k: shift_json(v, offset, k) for k,v in value.items()}
    if isinstance(value, list):
        return [shift_json(v, offset, key) for v in value]
    if isinstance(value, str) and len(value) >= 19 and value[4:5] == '-' and value[10:11] == 'T':
        try:
            parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is not None:
                return (parsed + timedelta(seconds=offset)).isoformat()
        except ValueError:
            pass
    if isinstance(value, (int,float)) and not isinstance(value,bool) and (key == 't' or key.endswith('_t') or key in {'created_at','depart_at','arrive_at','ends_at','started_at','until'}) and 946684800 < value < 4102444800:
        return value + offset
    return copy.deepcopy(value)


def shift_transit_numeric_schedules(value, offset, key=''):
    """Transit heaps encode [release, worker ID, shift start, shift end, direction]."""
    if key in KEEP or key.startswith(('remote_', 'source_')):
        return copy.deepcopy(value)
    if key in ('commute_events', 'commute_pending'):
        return [shift_transit_numeric_schedules(event, offset, 'commute_event') for event in value]
    if key == 'commute_event':
        result = list(value)
        for index in (0, 2, 3):
            result[index] += offset
        return result
    if isinstance(value, dict):
        return {k: shift_transit_numeric_schedules(v, offset, k) for k,v in value.items()}
    if isinstance(value, list):
        return [shift_transit_numeric_schedules(v, offset, key) for v in value]
    if isinstance(value, (int,float)) and not isinstance(value,bool) and key in {
            'queue_entered_at','hold_until','timestamp','last_load_update','shift_end','collision_times'}:
        return value + offset
    return copy.deepcopy(value)


# Only operational tables can be changed. Every remaining table is untouched.
ALLOW = {
 'clinics': {'encounters','lab_orders','referrals','clinic_appointments','infection_episodes','spatial_shocks',
             'household_exposures','facility_states','patients'},
 'hospital': {'encounters','department_visits','orders','care_assignments','specialist_referrals','weather_deferred_visits',
              'ambulance_units','ems_incidents','staff_assignments','staff_absences','emergency_codes','code_dispatches','facility_state',
              'clinicians','staff_members','diagnoses'},
 'police': {'units','incidents','cases','evidence','custody','handoffs'},
 'fire': {'apparatus','incidents','responses','investigations','tasks'},
 '911': {'calls','incidents','tow_units'},
 'office': {'office_entities','office_illness_episodes'},
 'transit': set(),
 'university': {'entities'},
 'city': {'city_incidents','city_work_orders','city_civic_records'},
 'bank': set(),
 'environment': {'environment_hazards'},
}
STATE = {'office': ('office_simulation_state','clock','data','building_id'),
         'university': ('simulation_state','clock','data','id'),
         'transit': ('simulation',None,'payload','id'),
         'city': ('city_state',None,'payload','id'),
         'bank': ('bank_state',None,'payload','id'),
         'environment': ('environment_state',None,'payload','id')}


def active(row, name, old):
    if row.get('status') in TERMINAL:
        return False
    if name == 'responses' and row.get('status') == 'available':
        return False
    for key in ['completed_at','closed_at','discharged_at','resolved_at','left_at','ended_at','released_at','returned_at']:
        if row.get(key) is not None:
            return False
    if name == 'spatial_shocks' and timestamp(row['ends_at']) <= old:
        return False
    if name == 'entities' and row['kind'] in {'publications','events_calendar','sections','courses','applications','students','programs','enrollments'}:
        return False
    return True


def serial(value):
    return json.loads(json.dumps(value, default=lambda x: x.isoformat()))


def migrate(engine, peer, config, dry_run=False):
    md = MetaData()
    audit = Table('northstar_clock_audit', md,
        Column('id',String(250),primary_key=True), Column('epoch',String(80),nullable=False),
        Column('table_name',String(100),nullable=False), Column('record_key',JSON,nullable=False),
        Column('before_values',JSON,nullable=False), Column('after_values',JSON,nullable=False),
        Column('recorded_at',String(50),nullable=False))
    with engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            conn.execute(text('SELECT pg_advisory_xact_lock(88758875)'))
        audit.create(conn, checkfirst=True)
        reflected = MetaData()
        reflected.reflect(conn)
        table_name,column,meta,key = STATE.get(peer, ('simulation_state','simulated_at','settings','id'))
        states = reflected.tables[table_name]
        state_rows = conn.execute(select(states).with_for_update()).mappings().all()
        offsets = {}
        dest = timestamp(config['cutover_at'])
        changed = {}

        def save(table, row, changes):
            if not changes:
                return
            pk = {c.name:row[c.name] for c in table.primary_key}
            if not pk:
                raise ValueError('A migration target has no primary key')
            identity = json.dumps(serial(pk),sort_keys=True,separators=(',',':'))
            aid = peer + ':' + table.name + ':' + identity
            before = {k:row[k] for k in changes}
            conn.execute(insert(audit).values(id=aid,epoch=config['epoch'],table_name=table.name,record_key=serial(pk),
                before_values=serial(before),after_values=serial(changes),recorded_at=datetime.now(timezone.utc).isoformat()))
            conn.execute(update(table).where(*(table.c[k]==v for k,v in pk.items())).values(**changes))
            changed[table.name] = changed.get(table.name,0)+1

        for r in state_rows:
            data = r[meta]
            if data.get('shared_clock'):
                if data['shared_clock']['epoch'] != config['epoch']:
                    raise ValueError('Existing different epoch; manual recovery required')
                continue
            old = timestamp(r[column] if column else data['t'] if peer=='transit' else data['simulated_at'])
            if dest < old:
                raise ValueError('Cutover would move a simulator backward')
            offsets[str(r[key])] = (old,dest-old)
        if not offsets:
            return {'peer':peer,'already_migrated':True}
        if len(offsets) != len(state_rows):
            raise ValueError('Partially enrolled state; investigate before retrying')
        missing = ALLOW[peer] - reflected.tables.keys()
        if missing:
            raise ValueError('Missing migration tables: '+str(sorted(missing)))
        for name in sorted(ALLOW[peer]):
            table=reflected.tables[name]
            for r in conn.execute(select(table)).mappings():
                old,offset=offsets[str(r['building_id']) if peer=='office' else '1']
                if not active(r,name,old):
                    continue
                changes={}
                for c in table.columns:
                    if c.primary_key or (c.name in KEEP and c.name != 'payload' and not (name=='diagnoses' and c.name=='recorded_at')):
                        continue
                    value=r[c.name]
                    if value is None:
                        continue
                    if isinstance(c.type,DateTime):
                        # Creation is historical except operational appointment/queue creation.
                        if c.name=='created_at' and name=='patients':
                            continue
                        new=value+timedelta(seconds=offset)
                    elif isinstance(c.type,JSON):
                        if name=='patients' and c.name!='profile':
                            continue
                        if name=='handoffs' and c.name=='payload':
                            # Immutable transport payload; local simulated milestones only.
                            new=copy.deepcopy(value)
                            for k in ('simulated_ack_at','simulated_complete_at'):
                                if k in new:new[k]=shift_json(new[k],offset,k)
                        else:
                            new=shift_json(value,offset)
                    elif name=='office_illness_episodes' and c.name=='exposed_at':
                        new=iso(timestamp(value)+offset)
                    else:
                        continue
                    if new!=value:changes[c.name]=new
                save(table,r,changes)
        for r in state_rows:
            old,offset=offsets[str(r[key])]
            data=shift_json(r[meta],offset)
            data['shared_clock']={'epoch':config['epoch'],'cutover_at':config['cutover_at'],
                'previous_simulated_at':iso(old),'offset_seconds':offset,
                'history_policy':'Original historical records and transport envelopes retain their source timeline. Active schedule changes are recorded in northstar_clock_audit.'}
            if column:
                at=datetime.fromtimestamp(dest,timezone.utc) if isinstance(states.c[column].type,DateTime) else iso(dest)
                changes={column:at,meta:data}
            else:
                data['t' if peer=='transit' else 'simulated_at']=dest if peer=='transit' else iso(dest)
                changes={meta:data}
            if 'paused' in states.c:changes['paused']=False
            if 'running' in states.c:changes['running']=True
            if not column:data['paused']=False
            if peer=='transit':
                data=shift_transit_numeric_schedules(data,offset)
                data['shared_clock']['transit_numeric_schedules_version']=1
                changes[meta]=data
                infra=data.get('infrastructure',{})
                prior=r[meta].get('infrastructure',{})
                if 'observed_t' in prior:infra['observed_t']=prior['observed_t']+offset
                if prior.get('observed_at'):infra['observed_at']=iso(timestamp(prior['observed_at'])+offset)
            if peer=='office':
                data['legacy_pending_simulated_seconds']=r[meta].get('pending_simulated_seconds',0)
                data['pending_simulated_seconds']=0
                # The cutover is not a worked day; keep totals until the next modeled day boundary.
                data['day']=datetime.fromtimestamp(dest,timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('America/Moncton')).date().isoformat()
            if peer=='environment':
                data['legacy_pending_seconds']=r[meta].get('pending_seconds',0)
                data['pending_seconds']=0
                data['last_auto_day']=datetime.fromtimestamp(dest,timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('America/Moncton')).date().isoformat()
            if peer=='university':
                local=datetime.fromtimestamp(dest,timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('America/Moncton'))
                data['last_daily_date']=local.date().isoformat()
                data['faculty_life_planned_on']=None
            save(states,r,changes)
        result={'peer':peer,'changed_rows':changed,'members':len(offsets),'target_at':config['cutover_at']}
        if dry_run:
            conn.rollback()
        return result

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('peer',choices=ALLOW)
    parser.add_argument('config')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    from app.db import engine
    print(json.dumps(migrate(engine,args.peer,json.load(open(args.config)),args.dry_run)))
