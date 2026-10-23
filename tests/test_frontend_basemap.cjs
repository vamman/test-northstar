// Exercise production basemap composition without a browser or cloned implementation.
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8');
const start=source.indexOf('  function composeMapCatalog('),end=source.indexOf('  function applyCityCatalog(',start);
assert(start>=0&&end>start);
const context={};vm.createContext(context);vm.runInContext(source.slice(start,end)+'\nthis.compose=composeMapCatalog;',context);
const base=JSON.parse(fs.readFileSync(path.join(__dirname,'../app/northstar-basemap.json'),'utf8'));
const duplicateRoad={...base.roads[0],geometry:{type:'LineString',coordinates:[[1,2],[3,4]]}};
const duplicateBuilding={...base.buildings[0],district_id:'WRONG'};
const office={id:'office:fixture',name:'Fixture office',kind:'office',x:2700,y:9000,district_id:'central'};
const access={id:'office:fixture:access',geometry:{type:'LineString',coordinates:[[2700,9000],[1800,9100]]}};
const building={...office,width:90,height:70};
const city={world_version:base.world_version,bounds:base.views.city.bounds,places:[base.places[0],office],
  roads:[duplicateRoad,access],buildings:[duplicateBuilding,building],routes:[{id:'still-operational'}],events:[{id:'unchanged'}]};
const beforeBase=JSON.stringify(base),beforeCity=JSON.stringify(city),result=context.compose(base,city);
assert.equal(result.bounds.min_y,-22000);assert.equal(result.bounds.max_y,48000);
assert.equal(result.roads.length,base.roads.length+1);
assert.equal(result.buildings.length,base.buildings.length+1);
assert.equal(result.places.length,base.places.length+1);
assert.equal(result.roads[0],base.roads[0]);assert.equal(result.buildings[0].district_id,base.buildings[0].district_id);
assert.equal(result.routes,city.routes);assert.equal(result.events,city.events);
assert(result.places.some(p=>p.id==='clinic_geriatrics'));
assert(result.places.some(p=>p.id==='clinic_msk_rehab'));
assert(result.places.some(p=>p.id==='office:fixture'));
assert(result.water.some(w=>w.geometry.type==='LineString'));
assert.equal(JSON.stringify(base),beforeBase);assert.equal(JSON.stringify(city),beforeCity);
assert.throws(()=>context.compose(base,{...city,world_version:'wrong'}),/versions differ/);
// A subsequent registry refresh must not discard the immutable base or mutate it.
const refreshed=context.compose(base,{...city,places:[...city.places,{...office,id:'office:second'}]});
assert.equal(refreshed.places.length,base.places.length+2);assert.equal(JSON.stringify(base),beforeBase);
console.log('Shared basemap composition preserves canonical geometry, rural coverage and operational overlays.');
