from __future__ import annotations
import csv,html,json,uuid,os,re,threading,time,hashlib
from datetime import datetime,timezone
import geopandas as gpd
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs
from sqlalchemy import create_engine, text
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import Response,JSONResponse,FileResponse,HTMLResponse
from pydantic import BaseModel
from shapely import wkt as shapely_wkt
from raster_layers import (
 create_coverage,create_terrain_product,describe_raster,raster_bounds4326,raster_stretch_range,
 identify_raster,raster_statistics,render_raster,
)
from vector_layers import (
 build_vector_spatial_index,create_buffer,get_vector_spatial_index,identify_vector,
 list_geopackage_layers,read_vector_layer,render_vector,vector_bounds4326,
)
from console_banner import print_server_banner
from admin_auth import AdminAuth
from sld_styles import apply_sld,read_sld
from i18n_auth import auth_page as localized_auth_page
from coordinate_transform import transform_coordinates
from postgis_connections import PostgisConnectionStore
from wps import discover_processes,execute_process
BASE=Path(__file__).resolve().parent; CONFIG=json.loads((BASE/'config.json').read_text(encoding='utf-8')); RESULTS=BASE/'results'; RESULTS.mkdir(exist_ok=True)
LAYERS_CSV=BASE/'layers.csv'
WPS_RESULTS_JSON=RESULTS/'wps_results.json'
WPS_RESULT_TTL_SECONDS=24*60*60
CSV_FIELDS=['name','title','type','path','crs','connection_id','schema','table','geometry_column','columns','stroke','fill','fill_opacity','max_records','sld_path','enabled']
LAYER_TYPES={'raster','shp','dxf','gpkg','postgis'}
UPLOAD_EXTENSIONS={'.tif','.tiff','.shp','.shx','.dbf','.prj','.cpg','.dxf','.gpkg','.sld'}
UPLOADS=BASE/'data'/'uploads';UPLOADS.mkdir(parents=True,exist_ok=True)
_LAYER_LOCK=threading.RLock()
_WPS_RESULT_LOCK=threading.RLock()
_WPS_RESULTS={}
_RENDER_LOCK=threading.RLock()
_RENDER_CACHE=OrderedDict()
RENDER_CACHE_SIZE=256
RENDER_CACHE_TTL=60
RENDER_DISK_CACHE=BASE/'cache'/'rendered';RENDER_DISK_CACHE.mkdir(parents=True,exist_ok=True)
RENDER_DISK_CACHE_SIZE=2048
RENDER_CACHE_VERSION=2

def layer_to_row(layer):
 style=layer.get('style',{})
 return {k:str(layer.get(k,'')) for k in CSV_FIELDS}|{
  'stroke':str(style.get('stroke',layer.get('stroke',''))),
  'fill':str(style.get('fill',layer.get('fill',''))),
  'fill_opacity':str(style.get('fill_opacity',layer.get('fill_opacity',''))),
  'enabled':str(layer.get('enabled',True)).lower()
 }
def row_to_layer(row):
 layer={k:(v or '').strip() for k,v in row.items() if k in CSV_FIELDS}
 layer['enabled']=layer.get('enabled','true').lower() not in ('false','0','no','off')
 style={k:layer.pop(k) for k in ('stroke','fill','fill_opacity') if layer.get(k)}
 if 'fill_opacity' in style:
  try: style['fill_opacity']=float(style['fill_opacity'])
  except ValueError: style.pop('fill_opacity')
 if style: layer['style']=style
 return {k:v for k,v in layer.items() if v != ''}
def write_layer_rows(rows):
 tmp=LAYERS_CSV.with_suffix('.csv.tmp')
 with tmp.open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=CSV_FIELDS);w.writeheader();w.writerows([{k:r.get(k,'') for k in CSV_FIELDS} for r in rows])
 tmp.replace(LAYERS_CSV)
def read_layer_rows():
 with _LAYER_LOCK:
  if not LAYERS_CSV.exists(): write_layer_rows([layer_to_row(x) for x in CONFIG.get('layers',[])])
  with LAYERS_CSV.open('r',encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def write_wps_results():
 temporary=WPS_RESULTS_JSON.with_suffix('.json.tmp')
 temporary.write_text(json.dumps(_WPS_RESULTS,ensure_ascii=False,indent=2),encoding='utf-8')
 temporary.replace(WPS_RESULTS_JSON)
def cleanup_wps_results():
 now=time.time();changed=False
 with _WPS_RESULT_LOCK:
  for result_id,item in list(_WPS_RESULTS.items()):
   path=(BASE/item.get('layer',{}).get('path','')).resolve()
   if float(item.get('expires_at',0))<=now or not path.is_file():
    if path.parent==RESULTS.resolve():path.unlink(missing_ok=True)
    _WPS_RESULTS.pop(result_id,None);changed=True
  if changed:write_wps_results()
def load_wps_results():
 if not WPS_RESULTS_JSON.exists():return
 try:
  saved=json.loads(WPS_RESULTS_JSON.read_text(encoding='utf-8'))
  if isinstance(saved,dict):_WPS_RESULTS.update(saved)
 except (OSError,json.JSONDecodeError):pass
 cleanup_wps_results()
def temporary_wps_layers():
 cleanup_wps_results()
 with _WPS_RESULT_LOCK:return [dict(item['layer']) for item in _WPS_RESULTS.values()]
def publish_wps_raster(result,process_id,source_name,parameters=None):
 path=Path(result.data).resolve()
 if path.parent!=RESULTS.resolve() or not path.is_file():raise ValueError('WPS result is not a publishable server file')
 description=describe_raster({'name':'result','path':path.relative_to(BASE).as_posix()},BASE)
 result_id=uuid.uuid4().hex
 layer_name=f'wps_{process_id.replace(".","_")}_{result_id[:12]}'
 expires_at=time.time()+WPS_RESULT_TTL_SECONDS
 layer={'name':layer_name,'title':f'WPS {process_id} - {source_name}','type':'raster','path':path.relative_to(BASE).as_posix(),'crs':description['crs'],'enabled':True,'temporary':True,'result_id':result_id}
 if process_id=='vector.heatmap':
  low,high=raster_stretch_range(layer,BASE);parameters=parameters or {}
  layer['_sld_colormap']=[
   {'quantity':low,'color':parameters.get('low_color','#2c7bb6'),'opacity':0.35},
   {'quantity':high,'color':parameters.get('high_color','#d7191c'),'opacity':0.95},
  ]
 with _WPS_RESULT_LOCK:
  _WPS_RESULTS[result_id]={'process':process_id,'source':source_name,'filename':result.filename or path.name,'media_type':result.media_type,'expires_at':expires_at,'layer':layer}
  write_wps_results()
 return result_id,layer,expires_at
load_wps_results()
def active_layers():
 permanent=[row_to_layer(r) for r in read_layer_rows() if row_to_layer(r).get('enabled',True)]
 return permanent+temporary_wps_layers()
def layer_map(): return {x['name']:x for x in active_layers()}
def get_layer(name):
 layer=layer_map().get(name)
 if not layer: raise KeyError(f'Unknown or disabled layer: {name}')
 return layer
def start_spatial_index(layer):
 if layer.get('type')!='shp':return
 def worker():
  try:build_vector_spatial_index(layer,BASE)
  except Exception as error:print(f'[SPATIAL INDEX] {layer.get("name")}: {error}')
 threading.Thread(target=worker,name=f'vector-index-{layer.get("name","layer")}',daemon=True).start()
POSTGIS_URL=os.getenv('POSTGIS_URL', CONFIG.get('postgis',{}).get('url','postgresql+psycopg://neung:neungpass@127.0.0.1:5432/neunggureongi'))
_DB_ENGINES={}
def db_engine(connection_id=''):
 key=connection_id or 'default'
 if key not in _DB_ENGINES:
  url=POSTGIS_URL if key=='default' else POSTGIS_CONNECTIONS.url(key)
  _DB_ENGINES[key]=create_engine(url,pool_pre_ping=True,pool_timeout=5,connect_args={'connect_timeout':5,'options':'-c statement_timeout=15000'})
 return _DB_ENGINES[key]
@asynccontextmanager
async def lifespan(application:FastAPI):
 pid_file=BASE/'server.pid'
 port_file=BASE/'server.port'
 selected_port=str(os.getenv('APP_PORT',CONFIG.get('server',{}).get('port',8000)))
 os.environ.setdefault('APP_PORT',selected_port)
 pid_file.write_text(str(os.getpid()),encoding='ascii')
 port_file.write_text(selected_port,encoding='ascii')
 print_server_banner()
 for layer in active_layers():start_spatial_index(layer)
 try:
  yield
 finally:
  try:
   if pid_file.exists() and pid_file.read_text(encoding='ascii').strip()==str(os.getpid()):pid_file.unlink()
  except OSError:pass

app=FastAPI(title='Project Neunggureongi OGC Server',version='1.0.0',lifespan=lifespan)
ADMIN_AUTH=AdminAuth(BASE)
POSTGIS_CONNECTIONS=PostgisConnectionStore(BASE,lambda:ADMIN_AUTH._key())

def auth_page(mode,error=''):
 setup=mode=='setup';title='관리자 계정 만들기' if setup else '관리자 로그인'
 action='/admin/setup' if setup else '/admin/login';button='암호화 계정 생성' if setup else '로그인'
 note='최초 1회만 설정합니다. ID와 비밀번호는 암호화된 바이너리로 저장됩니다.' if setup else '레이어 관리를 계속하려면 로그인하세요.'
 confirm='<label>비밀번호 확인<input type="password" name="confirm" minlength="8" required autocomplete="new-password"></label>' if setup else ''
 error_html=f'<div class="error">{html.escape(error)}</div>' if error else ''
 return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:28px;background:radial-gradient(circle at 18% 15%,#254b48 0,#14253a 32%,#0b1427 72%);font-family:Inter,"Noto Sans KR",sans-serif;color:#172033}}.shell{{width:min(920px,100%);display:grid;grid-template-columns:1.12fr .88fr;background:white;border-radius:18px;overflow:hidden;box-shadow:0 30px 90px #0008}}.story{{position:relative;padding:48px;color:#edf8ed;background:linear-gradient(145deg,#173b36,#28664d)}}.story:after{{content:"S";position:absolute;right:18px;bottom:-58px;font-family:Georgia,serif;font-size:250px;color:#ffffff0b;transform:rotate(-18deg)}}.version{{display:inline-block;padding:6px 10px;border:1px solid #d8efdc55;border-radius:20px;font-size:11px;font-weight:800;letter-spacing:.12em}}.brand{{margin:22px 0 5px;font-size:30px;letter-spacing:-.04em}}.english{{font-size:11px;letter-spacing:.18em;color:#b9ddc4}}.legend{{margin-top:44px;position:relative;z-index:1}}.legend h2{{font-family:Georgia,"Noto Serif KR",serif;font-size:18px;margin:0 0 12px;color:#f4d98c}}.legend p{{color:#d1e5d6;font-size:13px;line-height:1.85;margin:0 0 16px}}.wish{{padding-left:14px;border-left:2px solid #e6c76b;color:#fff!important;font-weight:650}}.box{{padding:48px 38px;align-self:center}}.box h1{{margin:0 0 8px;font-size:24px}}.box>p{{margin:0 0 24px;color:#667085;font-size:13px;line-height:1.6}}label{{display:block;font-size:12px;font-weight:700;margin-top:15px}}input{{width:100%;padding:12px;margin-top:7px;border:1px solid #cbd3df;border-radius:8px}}button{{width:100%;margin-top:24px;padding:12px;border:0;border-radius:8px;background:#28664d;color:white;font-weight:800;cursor:pointer}}button:hover{{background:#1e523d}}.error{{background:#fff0f1;color:#b4232d;padding:10px;border-radius:7px;font-size:13px}}@media(max-width:720px){{body{{padding:14px}}.shell{{grid-template-columns:1fr}}.story{{padding:30px}}.legend{{margin-top:26px}}.box{{padding:32px 28px}}}}</style></head>
<body><main class="shell"><section class="story"><span class="version">VERSION 1.0.0</span><h1 class="brand">능구렁이</h1><div class="english">PROJECT NEUNGGUREONGI</div><div class="legend"><h2>오래된 터를 지키는 존재</h2><p>우리 옛이야기에서 큰 구렁이는 집과 터에 오래 머물며 복을 지키는 신령한 존재로 여겨지곤 했습니다. 조용히 주변을 살피고 땅의 굽이를 타고 흐르는 모습에는 삶의 터전을 아끼는 마음이 담겨 있습니다.</p><p class="wish">그 이름처럼 지형과 도면, 공간 데이터의 굽이굽이를 능숙하게 잇고, 모든 공간정보를 빠르고 든든하게 서비스하고자 하는 염원을 이 프로젝트에 담았습니다.</p></div></section><form class="box" action="{action}" method="post"><h1>{title}</h1><p>{note}</p>{error_html}<label>ID<input name="username" maxlength="100" required autofocus autocomplete="username"></label><label>비밀번호<input type="password" name="password" minlength="8" required autocomplete="{'new-password' if setup else 'current-password'}"></label>{confirm}<button>{button}</button></form></main></body></html>"""

auth_page=localized_auth_page

@app.middleware('http')
async def admin_security(request:Request,call_next):
 protected=request.url.path.startswith('/admin/layers') or request.url.path.startswith('/api/layers') or request.url.path.startswith('/api/layer-files') or request.url.path.startswith('/api/transform') or request.url.path.startswith('/api/wps') or request.url.path.startswith('/api/postgis')
 if not protected:return await call_next(request)
 if not ADMIN_AUTH.is_configured():
  if request.url.path.startswith('/admin/'):return Response(status_code=303,headers={'Location':'/admin/setup'})
  return JSONResponse({'error':'관리자 계정 설정이 필요합니다.','setup':'/admin/setup'},status_code=503)
 if not ADMIN_AUTH.verify_session(request.cookies.get('admin_session')):
  if request.url.path.startswith('/admin/'):return Response(status_code=303,headers={'Location':'/admin/login'})
  return JSONResponse({'error':'인증이 필요합니다.'},status_code=401)
 return await call_next(request)

class TransformInput(BaseModel):
 source_crs:str
 target_crs:str
 coordinates:list[list[float]]

class PostgisConnectionInput(BaseModel):
 id:str
 name:str
 host:str
 port:int=5432
 database:str
 username:str
 password:str=''
 sslmode:str='prefer'

@app.post('/api/transform')
def coordinate_transform(data:TransformInput):
 try:return transform_coordinates(data.source_crs,data.target_crs,data.coordinates)
 except ValueError as e:raise HTTPException(400,str(e))

@app.get('/api/wps/processes')
def wps_process_catalog():
 return {'processes':[process['metadata'] for process in discover_processes().values()]}

@app.get('/api/wps/results/{result_id}/download')
def download_wps_result(result_id:str):
 cleanup_wps_results()
 with _WPS_RESULT_LOCK:item=_WPS_RESULTS.get(result_id)
 if not item:raise HTTPException(404,'WPS result not found or expired')
 path=BASE/item['layer']['path']
 return FileResponse(path,media_type=item['media_type'],filename=item['filename'])

@app.get('/admin/setup',response_class=HTMLResponse)
def admin_setup_page(request:Request):
 if ADMIN_AUTH.is_configured():return Response(status_code=303,headers={'Location':'/admin/login'})
 return HTMLResponse(auth_page('setup'))

@app.post('/admin/setup',response_class=HTMLResponse)
async def admin_setup(request:Request):
 if ADMIN_AUTH.is_configured():return Response(status_code=303,headers={'Location':'/admin/login'})
 if request.client and request.client.host not in ('127.0.0.1','::1'):
  return HTMLResponse(auth_page('setup','최초 계정 설정은 서버 PC에서만 가능합니다.'),status_code=403)
 form={k:v[0] for k,v in parse_qs((await request.body()).decode('utf-8')).items()}
 if form.get('password')!=form.get('confirm'):
  return HTMLResponse(auth_page('setup','비밀번호 확인이 일치하지 않습니다.'),status_code=400)
 try:ADMIN_AUTH.set_credentials(form.get('username',''),form.get('password',''))
 except ValueError as e:return HTMLResponse(auth_page('setup',str(e)),status_code=400)
 response=Response(status_code=303,headers={'Location':'/admin/layers'})
 response.set_cookie('admin_session',ADMIN_AUTH.create_session(form['username'].strip()),max_age=28800,httponly=True,samesite='strict')
 return response

@app.get('/admin/login',response_class=HTMLResponse)
def admin_login_page():
 if not ADMIN_AUTH.is_configured():return Response(status_code=303,headers={'Location':'/admin/setup'})
 return HTMLResponse(auth_page('login'))

@app.post('/admin/login',response_class=HTMLResponse)
async def admin_login(request:Request):
 form={k:v[0] for k,v in parse_qs((await request.body()).decode('utf-8')).items()}
 username=form.get('username','');password=form.get('password','')
 if not ADMIN_AUTH.authenticate(username,password):
  return HTMLResponse(auth_page('login','ID 또는 비밀번호가 올바르지 않습니다.'),status_code=401)
 response=Response(status_code=303,headers={'Location':'/admin/layers'})
 response.set_cookie('admin_session',ADMIN_AUTH.create_session(username),max_age=28800,httponly=True,samesite='strict')
 return response

@app.post('/admin/logout')
def admin_logout():
 response=Response(status_code=303,headers={'Location':'/admin/login'})
 response.delete_cookie('admin_session')
 return response

class LayerInput(BaseModel):
 name:str
 title:str
 type:str
 path:str=''
 crs:str='EPSG:5186'
 connection_id:str=''
 schema:str=''
 table:str=''
 geometry_column:str=''
 columns:str=''
 stroke:str=''
 fill:str=''
 fill_opacity:str=''
 max_records:int=5000
 sld_path:str=''
 enabled:bool=True

def validate_layer(data,original_name=None):
 row={k:str(getattr(data,k,'')) for k in CSV_FIELDS};row['enabled']=str(data.enabled).lower()
 row={k:v.strip() for k,v in row.items()}
 if not re.fullmatch(r'[A-Za-z0-9_.-]+',row['name']): raise HTTPException(400,'레이어 이름은 영문, 숫자, _, -, .만 사용할 수 있습니다.')
 if not row['title']: raise HTTPException(400,'표시 제목이 필요합니다.')
 if not row['crs']: raise HTTPException(400,'좌표계가 필요합니다.')
 if row['type'] not in LAYER_TYPES: raise HTTPException(400,'지원하지 않는 레이어 형식입니다.')
 if row['type']=='postgis':
  if not row['table']: raise HTTPException(400,'PostGIS 레이어는 테이블명이 필요합니다.')
  if row['connection_id'] and not any(item['id']==row['connection_id'] for item in POSTGIS_CONNECTIONS.list()):raise HTTPException(400,'PostGIS 연결을 찾을 수 없습니다.')
  row['schema']=row['schema'] or 'public';row['geometry_column']=row['geometry_column'] or 'geom'
 elif not row['path']: raise HTTPException(400,'파일 레이어는 경로가 필요합니다.')
 if row['type']=='gpkg':
  try:
   contents=list_geopackage_layers(BASE/row['path'])
   if not contents:raise ValueError('공간 레이어가 없습니다.')
   names=[item['name'] for item in contents]
   row['table']=row['table'] or names[0]
   if row['table'] not in names:raise ValueError(f'GeoPackage 레이어를 찾을 수 없습니다: {row["table"]}')
  except Exception as error:raise HTTPException(400,f'GeoPackage를 읽을 수 없습니다: {error}')
 if row['stroke'] and not re.fullmatch(r'#[0-9A-Fa-f]{6}',row['stroke']): raise HTTPException(400,'선 색상 형식이 올바르지 않습니다.')
 if row['fill'] and not re.fullmatch(r'#[0-9A-Fa-f]{6}',row['fill']): raise HTTPException(400,'채움 색상 형식이 올바르지 않습니다.')
 if row['fill_opacity']:
  try:
   if not 0<=float(row['fill_opacity'])<=1: raise ValueError
  except ValueError: raise HTTPException(400,'채움 투명도는 0~1 사이여야 합니다.')
 try:
  if not 1<=int(row['max_records'])<=100000:raise ValueError
 except ValueError:raise HTTPException(400,'최대 레코드는 1~100,000 사이여야 합니다.')
 return row
def xml(s,c=200): return Response(s,status_code=c,media_type='text/xml; charset=utf-8')
def qv(q,n,d=None): return q.get(n.upper(),d)
def bb(v):
 a=tuple(map(float,v.split(','))); 
 if len(a)!=4: raise ValueError('BBOX requires 4 values')
 return a
def layer_bounds4326(l):
 if l['type']=='raster':
  return raster_bounds4326(l,BASE)
 return vector_bounds4326(l,BASE,db_engine)
def wms_bbox(query):
 bounds=bb(qv(query,'BBOX'))
 version=qv(query,'VERSION','1.3.0')
 crs=(qv(query,'CRS') or qv(query,'SRS') or '').upper()
 if version=='1.3.0' and crs=='EPSG:4326':
  south,west,north,east=bounds
  return west,south,east,north
 return bounds
def wms_layer_capability(layer):
 name=html.escape(layer['name']);title=html.escape(layer['title'])
 extent=''
 try:
  west,south,east,north=layer_bounds4326(layer)
  extent=(f'<EX_GeographicBoundingBox><westBoundLongitude>{west}</westBoundLongitude>'
          f'<eastBoundLongitude>{east}</eastBoundLongitude><southBoundLatitude>{south}</southBoundLatitude>'
          f'<northBoundLatitude>{north}</northBoundLatitude></EX_GeographicBoundingBox>'
          f'<BoundingBox CRS="EPSG:4326" minx="{south}" miny="{west}" maxx="{north}" maxy="{east}"/>')
 except Exception:
  pass
 return (f'<Layer queryable="1"><Name>{name}</Name><Title>{title}</Title>'
         f'<CRS>EPSG:4326</CRS><CRS>EPSG:3857</CRS>{extent}'
         f'<Style><Name>default</Name><Title>Default style</Title></Style></Layer>')
def wfs_feature_type_capability(layer):
 name=html.escape(layer['name']);title=html.escape(layer['title'])
 bounds=''
 try:
  west,south,east,north=layer_bounds4326(layer)
  bounds=(f'<ows:WGS84BoundingBox><ows:LowerCorner>{west} {south}</ows:LowerCorner>'
          f'<ows:UpperCorner>{east} {north}</ows:UpperCorner></ows:WGS84BoundingBox>')
 except Exception:
  pass
 return (f'<wfs:FeatureType><wfs:Name>neung:{name}</wfs:Name><wfs:Title>{title}</wfs:Title>'
         f'<wfs:DefaultCRS>urn:ogc:def:crs:EPSG::{html.escape(layer["crs"].split(":")[-1])}</wfs:DefaultCRS>'
         f'{bounds}<wfs:OutputFormats><wfs:Format>application/json</wfs:Format>'
         f'<wfs:Format>application/geo+json</wfs:Format></wfs:OutputFormats></wfs:FeatureType>')
def caps(service,service_url=''):
 if service=='WMS':
  ls=''.join(wms_layer_capability(layer) for layer in active_layers())
  url=html.escape(service_url,quote=True);title=html.escape(CONFIG['service']['title'])
  online=f'<OnlineResource xmlns:xlink="http://www.w3.org/1999/xlink" xlink:type="simple" xlink:href="{url}"/>'
  return (f'<?xml version="1.0" encoding="UTF-8"?>'
   f'<WMS_Capabilities xmlns="http://www.opengis.net/wms" xmlns:xlink="http://www.w3.org/1999/xlink" '
   f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="1.3.0" '
   f'xsi:schemaLocation="http://www.opengis.net/wms https://schemas.opengis.net/wms/1.3.0/capabilities_1_3_0.xsd">'
   f'<Service><Name>WMS</Name><Title>{title}</Title><Abstract>Project Neunggureongi Web Map Service</Abstract>'
   f'<OnlineResource xlink:type="simple" xlink:href="{url}"/><Fees>none</Fees><AccessConstraints>none</AccessConstraints></Service>'
   f'<Capability><Request>'
   f'<GetCapabilities><Format>text/xml</Format><DCPType><HTTP><Get>{online}</Get></HTTP></DCPType></GetCapabilities>'
   f'<GetMap><Format>image/png</Format><DCPType><HTTP><Get>{online}</Get></HTTP></DCPType></GetMap>'
   f'<GetFeatureInfo><Format>application/json</Format><DCPType><HTTP><Get>{online}</Get></HTTP></DCPType></GetFeatureInfo>'
   f'</Request><Exception><Format>XML</Format></Exception>'
   f'<Layer queryable="0"><Title>{title}</Title><CRS>EPSG:4326</CRS><CRS>EPSG:3857</CRS>{ls}</Layer>'
   f'</Capability></WMS_Capabilities>')
 if service=='WFS':
  ls=''.join(wfs_feature_type_capability(layer) for layer in active_layers() if layer['type']!='raster')
  url=html.escape(service_url,quote=True);title=html.escape(CONFIG['service']['title'])
  operation=lambda name: (f'<ows:Operation name="{name}"><ows:DCP><ows:HTTP>'
                           f'<ows:Get xlink:href="{url}"/></ows:HTTP></ows:DCP></ows:Operation>')
  return (f'<?xml version="1.0" encoding="UTF-8"?>'
   f'<wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0" '
   f'xmlns:ows="http://www.opengis.net/ows/1.1" xmlns:fes="http://www.opengis.net/fes/2.0" '
   f'xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:xlink="http://www.w3.org/1999/xlink" '
   f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:neung="urn:neunggureongi" version="2.0.0" '
   f'xsi:schemaLocation="http://www.opengis.net/wfs/2.0 https://schemas.opengis.net/wfs/2.0/wfs.xsd">'
   f'<ows:ServiceIdentification><ows:Title>{title}</ows:Title>'
   f'<ows:Abstract>Project Neunggureongi Web Feature Service</ows:Abstract>'
   f'<ows:ServiceType>WFS</ows:ServiceType><ows:ServiceTypeVersion>2.0.0</ows:ServiceTypeVersion>'
   f'<ows:Fees>none</ows:Fees><ows:AccessConstraints>none</ows:AccessConstraints></ows:ServiceIdentification>'
   f'<ows:ServiceProvider><ows:ProviderName>Project Neunggureongi</ows:ProviderName>'
   f'<ows:ProviderSite xlink:href="{url}"/></ows:ServiceProvider>'
   f'<ows:OperationsMetadata>{operation("GetCapabilities")}{operation("DescribeFeatureType")}{operation("GetFeature")}'
   f'<ows:Constraint name="ImplementsBasicWFS"><ows:DefaultValue>TRUE</ows:DefaultValue></ows:Constraint>'
   f'</ows:OperationsMetadata><wfs:FeatureTypeList>{ls}</wfs:FeatureTypeList>'
   f'<fes:Filter_Capabilities><fes:Conformance>'
   f'<fes:Constraint name="ImplementsQuery"><ows:DefaultValue>TRUE</ows:DefaultValue></fes:Constraint>'
   f'</fes:Conformance></fes:Filter_Capabilities></wfs:WFS_Capabilities>')
 if service=='WCS':
  ls=''.join(f'<CoverageSummary><CoverageId>{html.escape(l["name"])}</CoverageId><CoverageSubtype>RectifiedGridCoverage</CoverageSubtype></CoverageSummary>' for l in active_layers() if l['type']=='raster')
  return f'<?xml version="1.0"?><Capabilities version="2.0.1"><ServiceIdentification><Title>{CONFIG["service"]["title"]}</Title></ServiceIdentification><Contents>{ls}</Contents></Capabilities>'
 if service=='WPS':
  ps=''.join(f'<wps:ProcessSummary processVersion="1.0"><ows:Identifier>{html.escape(x)}</ows:Identifier></wps:ProcessSummary>' for x in discover_processes())
  return f'<?xml version="1.0"?><wps:Capabilities xmlns:wps="http://www.opengis.net/wps/2.0" xmlns:ows="http://www.opengis.net/ows/2.0" version="2.0.0"><wps:Contents>{ps}</wps:Contents></wps:Capabilities>'
def render(layer,b,crs,w,h):
 if layer['type']=='raster': return render_raster(layer,b,crs,w,h,BASE)
 return render_vector(layer,b,crs,w,h,BASE,db_engine)
def render_cached(layer,b,crs,w,h):
 source=BASE/layer['path'] if layer.get('path') else LAYERS_CSV
 modified=source.stat().st_mtime_ns if source.exists() else 0
 sld=BASE/layer['sld_path'] if layer.get('sld_path') else None
 sld_modified=sld.stat().st_mtime_ns if sld and sld.exists() else 0
 style_signature=json.dumps({'style':layer.get('style',{}),'cql_filter':layer.get('_cql_filter','')},sort_keys=True,ensure_ascii=False)
 key=(RENDER_CACHE_VERSION,layer['name'],tuple(b),crs,w,h,modified,sld_modified,style_signature)
 disk_key=hashlib.sha256(repr(key).encode('utf-8')).hexdigest()
 disk_path=RENDER_DISK_CACHE/f'{disk_key}.png'
 now=time.monotonic()
 with _RENDER_LOCK:
  cached=_RENDER_CACHE.get(key)
  if cached and now-cached[0]<RENDER_CACHE_TTL:
   _RENDER_CACHE.move_to_end(key);return cached[1]
  if disk_path.exists():
   image=disk_path.read_bytes()
   _RENDER_CACHE[key]=(now,image);_RENDER_CACHE.move_to_end(key)
   return image
 image=render(apply_sld(layer,BASE),b,crs,w,h)
 with _RENDER_LOCK:
  _RENDER_CACHE[key]=(now,image);_RENDER_CACHE.move_to_end(key)
  temporary=disk_path.with_suffix('.tmp')
  temporary.write_bytes(image);temporary.replace(disk_path)
  cached_files=sorted(RENDER_DISK_CACHE.glob('*.png'),key=lambda path:path.stat().st_mtime)
  for old_file in cached_files[:-RENDER_DISK_CACHE_SIZE]:
   old_file.unlink(missing_ok=True)
  while len(_RENDER_CACHE)>RENDER_CACHE_SIZE:_RENDER_CACHE.popitem(last=False)
 return image
def tms_tile_bounds(z,x,tms_y):
 if not 0<=z<=22:raise ValueError('Zoom must be between 0 and 22')
 tile_count=1<<z
 if not 0<=x<tile_count or not 0<=tms_y<tile_count:raise ValueError('Tile coordinate is outside the zoom level')
 xyz_y=tile_count-1-tms_y
 world=20037508.342789244;tile_span=(world*2)/tile_count
 minx=-world+x*tile_span;maxx=minx+tile_span
 maxy=world-xyz_y*tile_span;miny=maxy-tile_span
 return minx,miny,maxx,maxy
@app.get('/')
def root(): return {'project':'Project Neunggureongi v1 + PostGIS','services':['WMS','WFS','WCS','WPS','TMS'],'formats':['GeoTIFF','SHP','DXF','GeoPackage','PostGIS'],'dwg':False,'layer_admin':'/admin/layers','postgis':'/api/postgis/health','docs':'/docs'}
@app.get('/tms/{layer_name}/{z}/{x}/{y}.png')
def tms_tile(layer_name:str,z:int,x:int,y:int):
 try:
  image=render_cached(get_layer(layer_name),tms_tile_bounds(z,x,y),'EPSG:3857',256,256)
  return Response(image,media_type='image/png',headers={'Cache-Control':'public, max-age=300'})
 except Exception as e:return JSONResponse({'error':str(e)},status_code=400)
@app.get('/tiles/{layer_name}/{z}/{x}/{y}.png')
def leaflet_tms_tile(layer_name:str,z:int,x:int,y:int):
 try:
  if not 0<=z<=22:raise ValueError('Zoom must be between 0 and 22')
  return tms_tile(layer_name,z,x,(1<<z)-1-y)
 except Exception as e:return JSONResponse({'error':str(e)},status_code=400)
@app.get('/wms')
def wms(r:Request):
 q={k.upper():v for k,v in r.query_params.items()};op=(qv(q,'REQUEST') or 'GetCapabilities').upper()
 if op=='GETCAPABILITIES': return xml(caps('WMS',str(r.url_for('wms'))))
 if op=='GETMAP':
  try:
   layer=get_layer(qv(q,'LAYERS').split(',')[0]);cql_filter=qv(q,'CQL_FILTER','')
   if cql_filter and layer['type']!='raster':layer={**layer,'_cql_filter':cql_filter}
   return Response(render_cached(layer,wms_bbox(q),qv(q,'CRS') or qv(q,'SRS'),int(qv(q,'WIDTH')),int(qv(q,'HEIGHT'))),media_type='image/png',headers={'Cache-Control':'public, max-age=60'})
  except Exception as e:return xml(f'<ServiceException>{html.escape(str(e))}</ServiceException>',400)
 if op=='GETFEATUREINFO':
  try:
   name=(qv(q,'QUERY_LAYERS') or qv(q,'LAYERS')).split(',')[0];layer=get_layer(name);cql_filter=qv(q,'CQL_FILTER','')
   if cql_filter and layer['type']!='raster':layer={**layer,'_cql_filter':cql_filter}
   bounds=wms_bbox(q);width=int(qv(q,'WIDTH'));height=int(qv(q,'HEIGHT'))
   column=int(qv(q,'I',qv(q,'X')));row=int(qv(q,'J',qv(q,'Y')))
   if not 0<=column<width or not 0<=row<height:raise ValueError('Click pixel is outside the map image')
   minx,miny,maxx,maxy=bounds;x=minx+(column+.5)/width*(maxx-minx);y=maxy-(row+.5)/height*(maxy-miny)
   crs=qv(q,'CRS') or qv(q,'SRS') or 'EPSG:4326'
   if layer['type']=='raster':result=identify_raster(layer,x,y,crs,BASE)
   else:result=identify_vector(layer,x,y,(maxx-minx)*8/width,(maxy-miny)*8/height,crs,BASE,db_engine)
   return JSONResponse(result)
  except Exception as e:return JSONResponse({'error':str(e)},status_code=400)
 return xml('<ServiceException>Unsupported request</ServiceException>',400)
@app.get('/wfs')
async def wfs(r:Request):
 q={k.upper():v for k,v in r.query_params.items()};op=(qv(q,'REQUEST') or 'GetCapabilities').upper()
 if op=='GETCAPABILITIES': return xml(caps('WFS',str(r.url_for('wfs'))))
 if op=='DESCRIBEFEATURETYPE':
  try:
   requested=(qv(q,'TYPENAMES') or qv(q,'TYPENAME') or '').split(',')[0]
   name=requested.split(':')[-1];layer=get_layer(name)
   if layer['type']=='raster':raise ValueError('WFS supports vector layers only')
   safe_name=html.escape(name,quote=True)
   return xml(f'<?xml version="1.0" encoding="UTF-8"?>'
    f'<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:gml="http://www.opengis.net/gml/3.2" '
    f'xmlns:neung="urn:neunggureongi" targetNamespace="urn:neunggureongi" elementFormDefault="qualified">'
    f'<xsd:import namespace="http://www.opengis.net/gml/3.2" schemaLocation="https://schemas.opengis.net/gml/3.2.1/gml.xsd"/>'
    f'<xsd:element name="{safe_name}" type="neung:{safe_name}Type" substitutionGroup="gml:AbstractFeature"/>'
    f'<xsd:complexType name="{safe_name}Type"><xsd:complexContent><xsd:extension base="gml:AbstractFeatureType">'
    f'<xsd:sequence><xsd:element name="geometry" type="gml:GeometryPropertyType" minOccurs="0" nillable="true"/>'
    f'</xsd:sequence></xsd:extension></xsd:complexContent></xsd:complexType></xsd:schema>')
  except Exception as e:return xml(f'<Exception>{html.escape(str(e))}</Exception>',400)
 if op=='GETFEATURE':
  try:
   requested=(qv(q,'TYPENAMES') or qv(q,'TYPENAME')).split(',')[0]
   name=requested.split(':')[-1];layer=get_layer(name)
   requested=max(1,int(qv(q,'COUNT',qv(q,'MAXFEATURES','1000'))));maximum=int(layer.get('max_records') or CONFIG.get('server',{}).get('wfs_max_records',5000));count=min(requested,maximum);bboxv=bb(qv(q,'BBOX')) if qv(q,'BBOX') else None
   g=read_vector_layer(layer,BASE,db_engine,qv(q,'SRSNAME') or None,bbox_filter=bboxv,limit=count)
   return JSONResponse(json.loads(g.head(count).to_json()))
  except Exception as e:return JSONResponse({'error':str(e)},status_code=400)
 return xml('<Exception>Unsupported request</Exception>',400)
@app.get('/wcs')
async def wcs(r:Request):
 q={k.upper():v for k,v in r.query_params.items()};op=(qv(q,'REQUEST') or 'GetCapabilities').upper()
 if op=='GETCAPABILITIES': return xml(caps('WCS'))
 if op=='DESCRIBECOVERAGE':
  l=get_layer(qv(q,'COVERAGEID'));
  return JSONResponse(describe_raster(l,BASE))
 if op=='GETCOVERAGE':
  try:
   l=get_layer(qv(q,'COVERAGEID')); src=BASE/l['path'];
   if not qv(q,'BBOX'): return FileResponse(src,media_type='image/tiff',filename=f'{l["name"]}.tif')
   b=bb(qv(q,'BBOX'));w=int(qv(q,'WIDTH','512'));h=int(qv(q,'HEIGHT','512'));crs=qv(q,'CRS',l['crs']);out=RESULTS/f'wcs_{uuid.uuid4().hex}.tif'
   create_coverage(l,b,crs,w,h,out,BASE)
   return FileResponse(out,media_type='image/tiff',filename=f'{l["name"]}_coverage.tif')
  except Exception as e:return JSONResponse({'error':str(e)},status_code=400)
 return xml('<Exception>Unsupported request</Exception>',400)
@app.get('/wps')
async def wps(r:Request):
 q={k.upper():v for k,v in r.query_params.items()};op=(qv(q,'REQUEST') or 'GetCapabilities').upper()
 if op=='GETCAPABILITIES': return xml(caps('WPS'))
 if op=='DESCRIBEPROCESS': return JSONResponse({'processes':[process['metadata'] for process in discover_processes().values()]})
 if op=='EXECUTE':
  temporary_input=None
  try:
   pid=qv(q,'IDENTIFIER');input_mode=(qv(q,'INPUT','LAYER') or 'LAYER').upper()
   if input_mode=='WKT':
    wkt_text=qv(q,'WKT','');wkt_crs=qv(q,'WKT_CRS','EPSG:4326')
    if not wkt_text:raise ValueError('WKT is required')
    if len(wkt_text)>1_000_000:raise ValueError('WKT exceeds 1 MB')
    geometry=shapely_wkt.loads(wkt_text)
    if geometry.is_empty:raise ValueError('WKT geometry is empty')
    temporary_input=RESULTS/f'wps_wkt_{uuid.uuid4().hex}.gpkg'
    gpd.GeoDataFrame({'geometry':[geometry]},crs=wkt_crs).to_file(temporary_input,layer='wkt_input',driver='GPKG')
    layer={'name':'wkt_input','title':'WKT input','type':'gpkg','path':temporary_input.relative_to(BASE).as_posix(),'table':'wkt_input','crs':wkt_crs,'enabled':True}
   else:
    name=qv(q,'LAYER');layer=get_layer(name)
   publish=(qv(q,'PUBLISH','false') or '').lower() in ('true','1','yes','on')
   parameters={key.lower():value for key,value in q.items() if key not in ('SERVICE','REQUEST','IDENTIFIER','LAYER','INPUT','WKT','WKT_CRS','PUBLISH')}
   result=execute_process(pid,layer,parameters,{'base_dir':BASE,'results_dir':RESULTS,'db_engine':db_engine,'get_layer':get_layer})
   if result.kind=='json':return JSONResponse(result.data)
   if result.kind=='file':
    if publish and result.media_type in ('image/tiff','image/geotiff'):
     result_id,result_layer,expires_at=publish_wps_raster(result,pid,layer['name'],parameters)
     base_wms=str(r.url_for('wms'))
     return JSONResponse({'status':'completed','result_id':result_id,'layer':result_layer,'layer_name':result_layer['name'],'wms_url':f'{base_wms}?SERVICE=WMS&REQUEST=GetMap&LAYERS={result_layer["name"]}','wms_capabilities_url':f'{base_wms}?SERVICE=WMS&REQUEST=GetCapabilities','download_url':str(r.url_for('download_wps_result',result_id=result_id)),'bounds':list(layer_bounds4326(result_layer)),'expires_at':datetime.fromtimestamp(expires_at,timezone.utc).isoformat()})
    return FileResponse(result.data,media_type=result.media_type,filename=result.filename)
   return JSONResponse({'error':f'Unsupported WPS result kind: {result.kind}'},status_code=500)
  except Exception as e:return JSONResponse({'error':str(e)},status_code=400)
  finally:
   if temporary_input and temporary_input.exists():
    try:temporary_input.unlink()
    except OSError:pass
 return xml('<Exception>Unsupported request</Exception>',400)

@app.get('/api/postgis/connections')
def postgis_connections():
 return {'connections':POSTGIS_CONNECTIONS.list()}

@app.post('/api/postgis/connections',status_code=201)
def create_postgis_connection(data:PostgisConnectionInput):
 try:
  return POSTGIS_CONNECTIONS.save(data.model_dump())
 except Exception as error:raise HTTPException(400,str(error))

@app.put('/api/postgis/connections/{connection_id}')
def update_postgis_connection(connection_id:str,data:PostgisConnectionInput):
 try:
  old=_DB_ENGINES.pop(connection_id,None)
  if old:old.dispose()
  return POSTGIS_CONNECTIONS.save(data.model_dump(),connection_id)
 except Exception as error:raise HTTPException(400,str(error))

@app.delete('/api/postgis/connections/{connection_id}',status_code=204)
def delete_postgis_connection(connection_id:str):
 if any(row.get('connection_id')==connection_id for row in read_layer_rows()):raise HTTPException(409,'이 연결을 사용하는 레이어가 있습니다.')
 try:POSTGIS_CONNECTIONS.delete(connection_id)
 except KeyError:raise HTTPException(404,'연결을 찾을 수 없습니다.')
 engine=_DB_ENGINES.pop(connection_id,None)
 if engine:engine.dispose()

@app.post('/api/postgis/connections/{connection_id}/test')
def test_postgis_connection(connection_id:str):
 try:
  with db_engine(connection_id).connect() as connection:
   version=connection.execute(text('SELECT PostGIS_Full_Version()')).scalar()
  return {'status':'ok','connection_id':connection_id,'postgis':version}
 except Exception as error:raise HTTPException(400,str(error))

@app.get('/api/postgis/health')
def postgis_health(connection_id:str=''):
 try:
  with db_engine(connection_id).connect() as c:
   version=c.execute(text('SELECT PostGIS_Full_Version()')).scalar()
  return {'status':'ok','connection_id':connection_id or 'default','postgis':version}
 except Exception as e:
  return JSONResponse({'status':'error','error':str(e)},status_code=503)

@app.get('/api/postgis/tables')
def postgis_tables(connection_id:str=''):
 try:
  sql=text("""SELECT f_table_schema AS schema, f_table_name AS table,
   f_geometry_column AS geometry_column, srid, type
   FROM geometry_columns ORDER BY f_table_schema,f_table_name""")
  with db_engine(connection_id).connect() as c: rows=[dict(r._mapping) for r in c.execute(sql)]
  return {'tables':rows}
 except Exception as e:
  return JSONResponse({'error':str(e)},status_code=503)

@app.get('/api/layers')
def list_layers():
 return {'layers':[row_to_layer(r) for r in read_layer_rows()]}

@app.post('/api/layer-files',status_code=201)
async def upload_layer_file(request:Request,filename:str,group:str):
 if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',group):raise HTTPException(400,'업로드 그룹 값이 올바르지 않습니다.')
 safe_name=Path(filename).name
 if safe_name!=filename or not safe_name:raise HTTPException(400,'파일명이 올바르지 않습니다.')
 extension=Path(safe_name).suffix.lower()
 if extension not in UPLOAD_EXTENSIONS:raise HTTPException(400,f'지원하지 않는 파일 확장자입니다: {extension}')
 directory=UPLOADS/group;directory.mkdir(parents=True,exist_ok=True)
 destination=directory/safe_name;temporary=directory/(safe_name+'.part')
 maximum=int(CONFIG.get('server',{}).get('upload_max_mb',2048))*1024*1024
 size=0
 try:
  with temporary.open('wb') as output:
   async for chunk in request.stream():
    size+=len(chunk)
    if size>maximum:raise HTTPException(413,f'파일은 최대 {maximum//1024//1024}MB까지 업로드할 수 있습니다.')
    output.write(chunk)
  temporary.replace(destination)
  if extension=='.sld':
   try:
    parsed=read_sld(destination)
    if not parsed['style'] and not parsed['color_map']:raise ValueError('지원되는 Symbolizer 또는 ColorMapEntry가 없습니다.')
   except Exception as e:
    destination.unlink(missing_ok=True)
    raise HTTPException(400,f'SLD 파일을 읽을 수 없습니다: {e}')
 except Exception:
  if temporary.exists():temporary.unlink()
  raise
 return {'filename':safe_name,'path':destination.relative_to(BASE).as_posix(),'size':size,'group':group}

@app.post('/api/layers',status_code=201)
def create_layer(data:LayerInput):
 row=validate_layer(data)
 with _LAYER_LOCK:
  rows=read_layer_rows()
  if any(r.get('name')==row['name'] for r in rows): raise HTTPException(409,'이미 존재하는 레이어 이름입니다.')
  rows.append(row);write_layer_rows(rows)
 layer=row_to_layer(row);start_spatial_index(layer)
 return layer

@app.put('/api/layers/{name}')
def update_layer(name:str,data:LayerInput):
 row=validate_layer(data,name)
 with _LAYER_LOCK:
  rows=read_layer_rows();index=next((i for i,r in enumerate(rows) if r.get('name')==name),None)
  if index is None: raise HTTPException(404,'레이어를 찾을 수 없습니다.')
  if row['name']!=name and any(r.get('name')==row['name'] for r in rows): raise HTTPException(409,'이미 존재하는 레이어 이름입니다.')
  rows[index]=row;write_layer_rows(rows)
 layer=row_to_layer(row);start_spatial_index(layer)
 return layer

@app.delete('/api/layers/{name}',status_code=204)
def delete_layer(name:str):
 with _LAYER_LOCK:
  rows=read_layer_rows();kept=[r for r in rows if r.get('name')!=name]
  if len(kept)==len(rows): raise HTTPException(404,'레이어를 찾을 수 없습니다.')
 write_layer_rows(kept)
 return Response(status_code=204)

@app.get('/api/layers/{name}/bounds')
def get_layer_bounds(name:str):
 try:
  return {'name':name,'bounds':list(layer_bounds4326(get_layer(name))),'crs':'EPSG:4326'}
 except KeyError as e:
  raise HTTPException(404,str(e))
 except Exception as e:
  raise HTTPException(400,f'레이어 영역을 읽을 수 없습니다: {e}')

@app.get('/api/layers/{name}/fields')
def get_layer_fields(name:str):
 try:
  layer=get_layer(name)
  if layer['type']=='raster':return {'name':name,'fields':[]}
  frame=read_vector_layer(layer,BASE,db_engine,limit=1)
  return {'name':name,'fields':[str(column) for column in frame.columns if column!=frame.geometry.name]}
 except Exception as error:raise HTTPException(400,f'필드 목록을 읽을 수 없습니다: {error}')

@app.get('/api/layers/{name}/spatial-index')
def spatial_index_status(name:str):
 layer=get_layer(name);path=get_vector_spatial_index(layer,BASE)
 return {'name':name,'supported':layer.get('type')=='shp','ready':bool(path),'path':path.relative_to(BASE).as_posix() if path else None}

@app.get('/api/layers/{name}/geopackage-layers')
def geopackage_contents(name:str):
 layer=get_layer(name)
 if layer.get('type')!='gpkg':raise HTTPException(400,'GeoPackage layer is required')
 return {'name':name,'layers':list_geopackage_layers(BASE/layer['path']),'selected':layer.get('table')}

@app.post('/api/layers/{name}/spatial-index')
def rebuild_spatial_index(name:str):
 layer=get_layer(name)
 if layer.get('type')!='shp':raise HTTPException(400,'Spatial indexing is supported for SHP layers')
 return build_vector_spatial_index(layer,BASE,force=True)

@app.get('/admin/layers',response_class=HTMLResponse)
def layer_admin():
 return HTMLResponse("""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>레이어 관리</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""><style>
:root{font-family:Inter,"Noto Sans KR",system-ui,sans-serif;color:#172033;background:#f3f5f8}*{box-sizing:border-box}
body{margin:0}.top{background:#14213d;color:white;padding:22px 32px;display:flex;align-items:center;justify-content:space-between}.top h1{font-size:22px;margin:0 0 5px}.top p{margin:0;color:#b9c4d8;font-size:13px}.topTools{display:flex;align-items:center;gap:7px}.langBtn,.logout{border:1px solid #ffffff55;background:transparent;color:white;border-radius:7px;padding:8px 10px;cursor:pointer}.langBtn.active{background:white;color:#14213d}
main{max-width:1400px;margin:24px auto;padding:0 24px}.bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.count{color:#647086;font-size:14px}.btn{border:0;border-radius:7px;padding:10px 15px;font-weight:700;cursor:pointer}.primary{background:#2762e9;color:white}.ghost{background:#e7ebf2;color:#344054}.danger{color:#c62d38;background:#fff0f1}
.card{background:white;border:1px solid #e0e5ed;border-radius:10px;overflow:auto;box-shadow:0 2px 9px #1e293b0c}
table{width:100%;border-collapse:collapse;white-space:nowrap}th{background:#f8f9fb;color:#657086;text-align:left;font-size:12px;padding:12px}td{border-top:1px solid #edf0f4;padding:12px;font-size:13px}td b{font-size:14px}
.badge{display:inline-block;border-radius:20px;background:#eaf0ff;color:#2454bc;padding:4px 8px;font-size:11px;font-weight:700}.off{background:#eee;color:#777}.actions button{margin-right:5px;padding:7px 9px}
dialog{border:0;border-radius:12px;padding:0;box-shadow:0 25px 70px #0005;width:min(780px,calc(100% - 28px))}dialog::backdrop{background:#10182888}
.modal{padding:24px}.modal h2{margin:0 0 18px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}.full{grid-column:1/-1}
label{display:block;font-size:12px;font-weight:700;color:#475467}input,select,textarea{width:100%;margin-top:6px;border:1px solid #cfd6e1;border-radius:7px;padding:10px;background:white}textarea{resize:vertical;font-family:Consolas,monospace;line-height:1.5}.check{display:flex;gap:9px;align-items:center}.check input{width:auto;margin:0}
.hint{display:block;margin-top:6px;color:#7a8495;font-size:11px;font-weight:400}.uploadState{color:#28664d;font-weight:700}
.serviceUrlField input{font-family:Consolas,monospace;background:#f8f9fb;color:#344054}
.foot{display:flex;justify-content:flex-end;gap:8px;margin-top:22px}.empty{text-align:center;padding:50px;color:#7b8495}.toast{position:fixed;right:24px;bottom:24px;padding:12px 18px;background:#172033;color:#fff;border-radius:8px;display:none}
.previewModal{width:min(1100px,calc(100% - 28px))}.previewHead{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}.previewHead h2{margin:0}.previewTools{display:flex;align-items:center;gap:8px}.previewSelect{width:auto;margin:0;padding:8px 32px 8px 10px;font-weight:700}.previewMeta{font-size:13px;color:#687386;margin-bottom:12px}
.previewFilter{display:flex;align-items:end;gap:8px;margin-bottom:12px}.previewFilter label{flex:1}.previewFilter input{font-family:Consolas,monospace}.previewFilter .btn{white-space:nowrap}
#map{height:min(68vh,680px);min-height:420px;border-radius:9px;background:#e9edf3}.mapClose{padding:8px 12px}
.actions button:disabled{opacity:.45;cursor:not-allowed}
@media(max-width:650px){.grid{grid-template-columns:1fr}.full{grid-column:auto}.top{padding:18px 20px}main{padding:0 12px}}
</style></head><body><header class="top"><div><h1 id="adminTitle">레이어 목록 관리</h1></div><div class="topTools"><button class="langBtn" id="adminKo" onclick="setUiLanguage('ko')">KO</button><button class="langBtn" id="adminEn" onclick="setUiLanguage('en')">EN</button><form action="/admin/logout" method="post"><button class="logout" id="logoutButton">로그아웃</button></form></div></header>
<main><div class="bar"><span class="count" id="count">불러오는 중…</span><div><button class="btn ghost" onclick="openPostgisConnections()">PostGIS 연결</button> <button class="btn ghost" id="wpsButton" onclick="openWps()">WPS 작업</button> <button class="btn ghost" id="transformButton" onclick="openTransform()">좌표 변환</button> <button class="btn primary" id="addLayerButton" onclick="openForm()">+ 레이어 추가</button></div></div>
<div class="card"><table><thead><tr><th id="thStatus">상태</th><th id="thName">이름 / 제목</th><th id="thType">형식</th><th id="thSource">소스</th><th id="thCrs">좌표계</th><th id="thStyle">스타일</th><th id="thManage">관리</th></tr></thead><tbody id="rows"></tbody></table></div></main>
<dialog id="dialog"><form class="modal" id="form"><h2 id="formTitle">레이어 추가</h2><div class="grid">
<label>레이어 이름 *<input name="name" required pattern="[A-Za-z0-9_.-]+"></label><label>표시 제목 *<input name="title" required></label>
<label>형식 *<select name="type" onchange="typeChanged()"><option value="raster">Raster / GeoTIFF</option><option value="shp">Shapefile</option><option value="dxf">DXF</option><option value="gpkg">GeoPackage</option><option value="postgis">PostGIS</option></select></label>
<label>좌표계 *<input name="crs" value="EPSG:5186" required></label>
<label class="wfsField">WFS 최대 레코드<input name="max_records" type="number" min="1" max="100000" value="5000" required><span class="hint">GetFeature 및 WFS 미리보기 최대 조회 건수</span></label>
<label class="full serviceUrlField">WMS URL<input id="wmsUrl" readonly></label>
<label class="full serviceUrlField wfsServiceUrlField">WFS URL<input id="wfsUrl" readonly></label>
<label class="full serviceUrlField heatmapWpsUrlField">WPS Heatmap URL<input id="heatmapWpsUrl" readonly><span class="hint">기본값: 영향 반경 1,000m · 픽셀 크기 100m</span></label>
<label class="full fileField">레이어 파일 업로드 *<input id="layerFiles" name="upload_files" type="file" multiple><span class="hint" id="uploadHint">GeoTIFF 파일을 선택하세요.</span></label>
<label class="full fileField">서버 저장 경로<input name="path" readonly placeholder="파일 업로드 후 자동 입력됩니다"><span class="hint uploadState" id="uploadState"></span></label>
<label class="dbField">PostGIS 연결<select name="connection_id" id="layerConnection" onchange="loadPostgisTables()"></select><span class="hint">기본 연결은 config.json 또는 POSTGIS_URL을 사용합니다.</span></label>
<label class="dbField">스키마<input name="schema" placeholder="public"></label><label class="dbField">테이블 *<input name="table" list="postgisTableOptions" onchange="selectPostgisTable()"><datalist id="postgisTableOptions"></datalist><span class="hint" id="postgisTableState"></span></label>
<label class="dbField">Geometry 컬럼<input name="geometry_column" placeholder="geom"></label><label class="dbField">조회 컬럼<input name="columns" placeholder="*"></label>
<label>선 색상<input name="stroke" type="color" value="#0055cc"></label><label>채움 색상<input name="fill" type="color" value="#66aaff"></label>
<label>채움 투명도<input name="fill_opacity" type="number" min="0" max="1" step="0.05" value="0.25"></label>
<label class="check"><input name="enabled" type="checkbox" checked> 서비스에 활성화</label></div>
<label class="full" style="margin-top:14px">SLD 스타일 파일<input id="sldFile" name="upload_sld" type="file" accept=".sld"><span class="hint">선택 사항 · SLD 1.0 / Symbology Encoding 형식</span></label>
<label class="full" style="margin-top:10px">SLD 서버 경로<input name="sld_path" readonly placeholder="SLD 업로드 후 자동 입력됩니다"><span class="hint uploadState" id="sldUploadState"></span></label>
<div class="foot"><button type="button" class="btn ghost" id="cancelButton" onclick="dialog.close()">취소</button><button class="btn primary" id="saveButton">저장</button></div></form></dialog>
<dialog id="previewDialog" class="previewModal"><div class="modal"><div class="previewHead"><h2 id="previewTitle">레이어 미리보기</h2><div class="previewTools"><select id="previewService" class="previewSelect" onchange="changePreviewService()"></select><button class="btn ghost mapClose" onclick="previewDialog.close()">닫기</button></div></div><div class="previewFilter" id="previewFilterBar"><label>CQL_FILTER<input id="previewFilter" placeholder="예: id >= 2 AND name LIKE 'A%'"><span class="hint" id="previewFilterHint"></span></label><button class="btn primary" onclick="applyPreviewFilter()">필터 적용</button><button class="btn ghost" onclick="clearPreviewFilter()">초기화</button></div><div class="previewMeta" id="previewMeta"></div><div id="map"></div></div></dialog>
<dialog id="transformDialog"><form class="modal" id="transformForm"><h2 id="transformTitle">좌표 변환</h2><div class="grid"><label>원본 좌표계<input name="source_crs" value="EPSG:4326" required placeholder="EPSG:4326"></label><label>대상 좌표계<input name="target_crs" value="EPSG:5186" required placeholder="EPSG:5186"></label><label class="full">입력 좌표 · 한 줄에 X,Y<textarea name="coordinates" rows="7" required placeholder="127.0, 37.5&#10;127.1, 37.6"></textarea></label><label class="full">변환 결과<textarea id="transformResult" rows="7" readonly></textarea></label></div><div class="foot"><button type="button" class="btn ghost" onclick="transformDialog.close()">닫기</button><button class="btn primary" id="transformSubmit">변환 실행</button></div></form></dialog>
<dialog id="postgisDialog"><form class="modal" id="postgisForm"><h2>PostGIS 연결 관리</h2><div class="grid"><label class="full">저장된 연결<select id="postgisConnectionList" onchange="selectPostgisConnection()"></select></label><label>연결 ID *<input name="id" required pattern="[A-Za-z0-9_.-]+"></label><label>표시 이름 *<input name="name" required></label><label>호스트 *<input name="host" required placeholder="127.0.0.1"></label><label>포트 *<input name="port" type="number" min="1" max="65535" value="5432" required></label><label>데이터베이스 *<input name="database" required></label><label>사용자 *<input name="username" required></label><label>비밀번호<input name="password" type="password" autocomplete="new-password"><span class="hint">수정할 때 비우면 기존 비밀번호를 유지합니다.</span></label><label>SSL 모드<select name="sslmode"><option value="prefer">prefer</option><option value="require">require</option><option value="verify-ca">verify-ca</option><option value="verify-full">verify-full</option><option value="disable">disable</option></select></label><div class="full"><span class="hint uploadState" id="postgisState"></span></div></div><div class="foot"><button type="button" class="btn danger" onclick="deletePostgisConnection()">삭제</button><button type="button" class="btn ghost" onclick="testPostgisConnection()">연결 테스트</button><button type="button" class="btn ghost" onclick="newPostgisConnection()">새 연결</button><button type="button" class="btn ghost" onclick="postgisDialog.close()">닫기</button><button class="btn primary">저장</button></div></form></dialog>
<dialog id="wpsDialog"><form class="modal" id="wpsForm"><h2 id="wpsTitle">WPS 공간 처리</h2><div class="grid"><label>입력 방식<select id="wpsInputMode" onchange="updateWpsInputMode()"><option value="LAYER">등록 레이어 선택</option><option value="WKT">WKT 직접 입력</option></select></label><label id="wpsLayerField">대상 레이어<select id="wpsLayer" onchange="updateWpsProcesses()" required></select></label><label id="wpsWktCrsField" style="display:none">WKT 좌표계<input id="wpsWktCrs" value="EPSG:4326" placeholder="EPSG:4326"></label><label id="wpsWktField" class="full" style="display:none">Geometry WKT<textarea id="wpsWkt" rows="5" placeholder="POINT (127.0 37.5)"></textarea></label><label>처리 프로세스<select id="wpsProcess" onchange="updateWpsOptions()" required></select></label><div id="wpsParameters" class="full grid"></div><label class="full">외부 애플리케이션 호출 URL<input id="wpsCallUrl" readonly><span class="hint">HTTP GET 요청 시 결과 파일이 응답됩니다.</span></label><label class="full">작업 결과<textarea id="wpsResult" rows="7" readonly placeholder="처리 결과와 상태가 표시됩니다."></textarea></label><div class="full"><a id="wpsDownload" class="btn primary" style="display:none;text-decoration:none" download>결과 파일 다운로드</a></div></div><div class="foot"><button type="button" class="btn ghost" onclick="wpsDialog.close()">닫기</button><button class="btn primary" id="wpsSubmit">프로세스 실행</button></div></form></dialog>
<div class="toast" id="toast"></div><script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script><script>
let layers=[],wpsPreviewLayers={},postgisConnections=[],postgisTables=[],editing=null,editingConnection=null,map=null,wmsLayer=null,wfsLayer=null,activePreview=null,activePreviewMode='WMS',activePreviewFilter='',wpsObjectUrl=null,wpsProcesses=[],previewRefreshTimer=null,previewRequestSeq=0;const dialog=document.querySelector('#dialog'),previewDialog=document.querySelector('#previewDialog'),transformDialog=document.querySelector('#transformDialog'),wpsDialog=document.querySelector('#wpsDialog'),postgisDialog=document.querySelector('#postgisDialog'),form=document.querySelector('#form'),transformForm=document.querySelector('#transformForm'),wpsForm=document.querySelector('#wpsForm'),postgisForm=document.querySelector('#postgisForm');
let uiLang=localStorage.getItem('neunggureongi_language')||'ko';const uiMessages={ko:{title:'레이어 목록 관리',logout:'로그아웃',add:'+ 레이어 추가',wps:'WPS 작업',transform:'좌표 변환',loading:'불러오는 중…',status:'상태',name:'이름 / 제목',type:'형식',source:'소스',crs:'좌표계',style:'스타일',manage:'관리',active:'활성',inactive:'비활성',map:'지도',edit:'수정',remove:'삭제',empty:'등록된 레이어가 없습니다.',total:'전체',enabled:'활성',addTitle:'레이어 추가',editTitle:'레이어 수정',cancel:'취소',save:'저장',wms:'WMS 이미지',wfs:'WFS 피처'},en:{title:'Layer Management',logout:'Sign out',add:'+ Add layer',wps:'WPS Tasks',transform:'Transform Coordinates',loading:'Loading…',status:'Status',name:'Name / Title',type:'Type',source:'Source',crs:'CRS',style:'Style',manage:'Actions',active:'Active',inactive:'Inactive',map:'Map',edit:'Edit',remove:'Delete',empty:'No layers registered.',total:'Total',enabled:'Active',addTitle:'Add Layer',editTitle:'Edit Layer',cancel:'Cancel',save:'Save',wms:'WMS Image',wfs:'WFS Features'}};
const t=key=>uiMessages[uiLang][key]||key;
function setUiLanguage(lang){uiLang=lang in uiMessages?lang:'ko';localStorage.setItem('neunggureongi_language',uiLang);document.documentElement.lang=uiLang;const ids={adminTitle:'title',logoutButton:'logout',addLayerButton:'add',wpsButton:'wps',transformButton:'transform',thStatus:'status',thName:'name',thType:'type',thSource:'source',thCrs:'crs',thStyle:'style',thManage:'manage',cancelButton:'cancel',saveButton:'save'};for(const [id,key] of Object.entries(ids)){const element=document.getElementById(id);if(element)element.textContent=t(key)}document.getElementById('adminKo').classList.toggle('active',uiLang==='ko');document.getElementById('adminEn').classList.toggle('active',uiLang==='en');render()}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function source(l){return l.type==='postgis'?`${l.schema||'public'}.${l.table||''}`:l.type==='gpkg'?`${l.path||'-'}${l.table?' # '+l.table:''}`:(l.path||'-')}
async function load(){const [layerResponse,connectionResponse]=await Promise.all([fetch('/api/layers'),fetch('/api/postgis/connections')]);layers=(await layerResponse.json()).layers;postgisConnections=(await connectionResponse.json()).connections||[];updateLayerConnections();render()}
function updateLayerConnections(){document.querySelector('#layerConnection').innerHTML='<option value="">기본 연결</option>'+postgisConnections.map(item=>`<option value="${esc(item.id)}">${esc(item.name)} (${esc(item.host)}:${esc(item.port)})</option>`).join('')}
async function loadPostgisTables(){if(form.type.value!=='postgis')return;const state=document.querySelector('#postgisTableState'),connectionId=document.querySelector('#layerConnection').value,params=new URLSearchParams();if(connectionId)params.set('connection_id',connectionId);state.textContent='Geometry 테이블 조회 중…';postgisTables=[];document.querySelector('#postgisTableOptions').innerHTML='';try{const response=await fetch('/api/postgis/tables'+(params.size?'?'+params:'')),data=await response.json();if(!response.ok)throw new Error(data.error||data.detail||'테이블 조회 실패');postgisTables=data.tables||[];document.querySelector('#postgisTableOptions').innerHTML=postgisTables.map(item=>`<option value="${esc(item.schema+'.'+item.table)}">${esc(item.type||'Geometry')} · EPSG:${esc(item.srid)}</option>`).join('');state.textContent=`Geometry 테이블 ${postgisTables.length}개`;selectPostgisTable()}catch(error){state.textContent='조회 실패 · '+error.message}}
function selectPostgisTable(){if(form.type.value!=='postgis')return;const entered=form.table.value.trim(),qualified=entered.includes('.')?entered:`${form.schema.value||'public'}.${entered}`,item=postgisTables.find(table=>`${table.schema}.${table.table}`===qualified);if(!item)return;form.schema.value=item.schema;form.table.value=item.table;form.geometry_column.value=item.geometry_column||'geom';if(item.srid)form.crs.value='EPSG:'+item.srid;document.querySelector('#postgisTableState').textContent=`${item.type||'Geometry'} · EPSG:${item.srid||'-'} · ${item.geometry_column||'geom'}`}
function render(){document.querySelector('#count').textContent=`${t('total')} ${layers.length} · ${t('enabled')} ${layers.filter(x=>x.enabled).length}`;
 document.querySelector('#rows').innerHTML=layers.length?layers.map(l=>`<tr><td><span class="badge ${l.enabled?'':'off'}">${l.enabled?t('active'):t('inactive')}</span></td><td><b>${esc(l.name)}</b><br>${esc(l.title)}</td><td>${esc(l.type)}</td><td>${esc(source(l))}</td><td>${esc(l.crs)}</td><td>${l.style?`<span style="color:${esc(l.style.stroke||'#777')}">●</span> ${esc(l.style.stroke||'-')}`:'-'}</td><td class="actions"><button class="btn primary" onclick="previewLayer('${esc(l.name)}')" ${l.enabled?'':'disabled'}>${t('map')}</button><button class="btn ghost" onclick="openForm('${esc(l.name)}')">${t('edit')}</button><button class="btn danger" onclick="removeLayer('${esc(l.name)}')">${t('remove')}</button></td></tr>`).join(''):`<tr><td colspan="7" class="empty">${t('empty')}</td></tr>`}
function updateServiceUrls(){const layerName=form.name.value.trim(),editingLayer=!!editing,isVector=form.type.value!=='raster';document.querySelectorAll('.serviceUrlField').forEach(x=>x.style.display=editingLayer?'block':'none');document.querySelector('#wmsUrl').value=layerName?`${location.origin}/wms?SERVICE=WMS&REQUEST=GetCapabilities&LAYERS=${encodeURIComponent(layerName)}`:'';document.querySelector('#wfsUrl').value=layerName?`${location.origin}/wfs?SERVICE=WFS&REQUEST=GetCapabilities&TYPENAMES=${encodeURIComponent(layerName)}`:'';const heatmapParams=new URLSearchParams({SERVICE:'WPS',REQUEST:'Execute',IDENTIFIER:'vector.heatmap',LAYER:layerName,RADIUS_M:'1000',PIXEL_SIZE_M:'100'});document.querySelector('#heatmapWpsUrl').value=layerName?location.origin+'/wps?'+heatmapParams:'';document.querySelector('.wfsServiceUrlField').style.display=editingLayer&&isVector?'block':'none';document.querySelector('.heatmapWpsUrlField').style.display=editingLayer&&isVector?'block':'none'}
function openForm(name=null){editing=name;form.reset();document.querySelector('#uploadState').textContent='';document.querySelector('#sldUploadState').textContent='';form.crs.value='EPSG:5186';form.max_records.value='5000';form.stroke.value='#0055cc';form.fill.value='#66aaff';form.fill_opacity.value='0.25';form.enabled.checked=true;
 if(name){const l=layers.find(x=>x.name===name);document.querySelector('#formTitle').textContent=t('editTitle');for(const k of ['name','title','type','path','crs','connection_id','schema','table','geometry_column','columns','max_records','sld_path'])if(form.elements[k])form.elements[k].value=l[k]||'';form.max_records.value=l.max_records||'5000';form.enabled.checked=!!l.enabled;if(l.style){form.stroke.value=l.style.stroke||'#0055cc';form.fill.value=l.style.fill||'#66aaff';form.fill_opacity.value=l.style.fill_opacity??'0.25'}}
 else document.querySelector('#formTitle').textContent=t('addTitle');typeChanged();dialog.showModal()}
function typeChanged(){const db=form.type.value==='postgis',gpkg=form.type.value==='gpkg',raster=form.type.value==='raster',files=document.querySelector('#layerFiles'),hint=document.querySelector('#uploadHint');document.querySelectorAll('.dbField').forEach(x=>x.style.display=(db||gpkg)?'block':'none');document.querySelectorAll('.fileField').forEach(x=>x.style.display=db?'none':'block');document.querySelectorAll('.wfsField').forEach(x=>x.style.display=raster?'none':'block');form.max_records.required=!raster;form.path.required=false;form.table.required=db;files.required=!db&&!editing;document.querySelector('#layerConnection').closest('label').style.display=db?'block':'none';document.querySelector('#postgisTableState').style.display=db?'block':'none';if(db)loadPostgisTables();else{postgisTables=[];document.querySelector('#postgisTableOptions').innerHTML=''}updateServiceUrls();
 const options={raster:['.tif,.tiff','GeoTIFF(.tif/.tiff) 파일을 선택하세요.'],shp:['.shp,.shx,.dbf,.prj,.cpg','같은 이름의 .shp, .shx, .dbf 파일을 함께 선택하세요.'],dxf:['.dxf','DXF 파일을 선택하세요.'],gpkg:['.gpkg','GeoPackage(.gpkg) 파일을 선택하세요. 테이블을 비우면 첫 공간 레이어를 사용합니다.']};if(!db){files.accept=options[form.type.value][0];hint.textContent=options[form.type.value][1]}}
form.name.addEventListener('input',updateServiceUrls);
async function uploadFiles(files,type){const required={raster:['.tif','.tiff'],shp:['.shp'],dxf:['.dxf'],gpkg:['.gpkg']}[type],extensions=[...files].map(f=>'.'+f.name.split('.').pop().toLowerCase());
 if(!required.some(x=>extensions.includes(x)))throw new Error('선택한 레이어 형식의 주 파일이 없습니다.');
 if(type==='shp'&&!['.shp','.shx','.dbf'].every(x=>extensions.includes(x)))throw new Error('Shapefile은 .shp, .shx, .dbf 파일을 함께 선택해야 합니다.');
 const group=(crypto.randomUUID?crypto.randomUUID():Date.now()+'_'+Math.random().toString(36).slice(2)).replaceAll('-','_'),results=[];for(let i=0;i<files.length;i++){const f=files[i];document.querySelector('#uploadState').textContent=`업로드 중 ${i+1}/${files.length} · ${f.name}`;const r=await fetch(`/api/layer-files?filename=${encodeURIComponent(f.name)}&group=${encodeURIComponent(group)}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f});if(!r.ok){const x=await r.json();throw new Error(x.detail||'파일 업로드에 실패했습니다.')}results.push(await r.json())}
 document.querySelector('#uploadState').textContent=`${files.length}개 파일 업로드 완료`;const mainExt=type==='raster'?['.tif','.tiff']:type==='shp'?['.shp']:type==='gpkg'?['.gpkg']:['.dxf'];return results.find(x=>mainExt.some(e=>x.filename.toLowerCase().endsWith(e))).path}
async function uploadSld(file){const group=('sld_'+(crypto.randomUUID?crypto.randomUUID():Date.now()+'_'+Math.random().toString(36).slice(2))).replaceAll('-','_');document.querySelector('#sldUploadState').textContent='SLD 업로드 중…';const r=await fetch(`/api/layer-files?filename=${encodeURIComponent(file.name)}&group=${encodeURIComponent(group)}`,{method:'POST',headers:{'Content-Type':'application/xml'},body:file});if(!r.ok){const x=await r.json();throw new Error(x.detail||'SLD 업로드에 실패했습니다.')}const result=await r.json();document.querySelector('#sldUploadState').textContent='SLD 업로드 완료';return result.path}
function notify(s){const t=document.querySelector('#toast');t.textContent=s;t.style.display='block';setTimeout(()=>t.style.display='none',2200)}
function renderPostgisConnectionList(selected=''){const list=document.querySelector('#postgisConnectionList');list.innerHTML='<option value="">새 연결</option>'+postgisConnections.map(item=>`<option value="${esc(item.id)}">${esc(item.name)} · ${esc(item.host)}:${esc(item.port)}/${esc(item.database)}</option>`).join('');list.value=selected}
function newPostgisConnection(){editingConnection=null;postgisForm.reset();postgisForm.id.readOnly=false;postgisForm.port.value='5432';postgisForm.sslmode.value='prefer';document.querySelector('#postgisState').textContent='';renderPostgisConnectionList()}
function selectPostgisConnection(){const id=document.querySelector('#postgisConnectionList').value;if(!id){newPostgisConnection();return}const item=postgisConnections.find(connection=>connection.id===id);editingConnection=id;for(const key of ['id','name','host','port','database','username','sslmode'])postgisForm.elements[key].value=item[key]||'';postgisForm.id.readOnly=true;postgisForm.password.value='';document.querySelector('#postgisState').textContent=item.has_password?'암호화된 비밀번호 저장됨':''}
async function openPostgisConnections(){const response=await fetch('/api/postgis/connections'),data=await response.json();if(!response.ok)return alert(data.error||data.detail||'연결 목록을 불러오지 못했습니다.');postgisConnections=data.connections||[];updateLayerConnections();newPostgisConnection();postgisDialog.showModal()}
postgisForm.onsubmit=async event=>{event.preventDefault();const button=event.submitter,data=Object.fromEntries(new FormData(postgisForm));button.disabled=true;try{const response=await fetch(editingConnection?'/api/postgis/connections/'+encodeURIComponent(editingConnection):'/api/postgis/connections',{method:editingConnection?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}),result=await response.json();if(!response.ok)throw new Error(result.detail||'연결을 저장하지 못했습니다.');const listResponse=await fetch('/api/postgis/connections');postgisConnections=(await listResponse.json()).connections||[];updateLayerConnections();editingConnection=result.id;renderPostgisConnectionList(result.id);selectPostgisConnection();document.querySelector('#postgisState').textContent='저장했습니다.'}catch(error){alert(error.message)}finally{button.disabled=false}}
async function testPostgisConnection(){if(!editingConnection)return alert('먼저 연결을 저장해 주세요.');const state=document.querySelector('#postgisState');state.textContent='연결 확인 중…';const response=await fetch('/api/postgis/connections/'+encodeURIComponent(editingConnection)+'/test',{method:'POST'}),data=await response.json();state.textContent=response.ok?'연결 성공 · '+data.postgis:(data.detail||'연결 실패')}
async function deletePostgisConnection(){if(!editingConnection||!confirm('이 PostGIS 연결을 삭제할까요?'))return;const response=await fetch('/api/postgis/connections/'+encodeURIComponent(editingConnection),{method:'DELETE'});if(!response.ok){const data=await response.json();return alert(data.detail||'연결을 삭제하지 못했습니다.')}postgisConnections=postgisConnections.filter(item=>item.id!==editingConnection);updateLayerConnections();newPostgisConnection();notify('PostGIS 연결을 삭제했습니다.')}
function openTransform(){document.querySelector('#transformResult').value='';transformDialog.showModal()}
transformForm.onsubmit=async event=>{event.preventDefault();const button=document.querySelector('#transformSubmit');button.disabled=true;try{const values=transformForm.coordinates.value.trim().split(/\\r?\\n/).filter(Boolean).map((line,index)=>{const coordinate=line.trim().split(/[\\s,]+/).map(Number);if(![2,3].includes(coordinate.length)||coordinate.some(value=>!Number.isFinite(value)))throw new Error(`${index+1}행의 좌표 형식이 올바르지 않습니다.`);return coordinate}),response=await fetch('/api/transform',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source_crs:transformForm.source_crs.value,target_crs:transformForm.target_crs.value,coordinates:values})}),data=await response.json();if(!response.ok)throw new Error(data.detail||'좌표변환에 실패했습니다.');document.querySelector('#transformResult').value=data.coordinates.map(coordinate=>coordinate.map(value=>Number(value.toFixed(8))).join(', ')).join('\\n')}catch(error){alert(error.message)}finally{button.disabled=false}};
async function openWps(){const select=document.querySelector('#wpsLayer');select.innerHTML=layers.filter(layer=>layer.enabled).map(layer=>`<option value="${esc(layer.name)}">${esc(layer.title)} (${esc(layer.type)})</option>`).join('');document.querySelector('#wpsInputMode').value='LAYER';document.querySelector('#wpsWkt').value='';document.querySelector('#wpsWktCrs').value='EPSG:4326';document.querySelector('#wpsResult').value='플러그인 목록을 불러오는 중…';document.querySelector('#wpsDownload').style.display='none';updateWpsInputMode();wpsDialog.showModal();try{const response=await fetch('/api/wps/processes'),data=await response.json();if(!response.ok)throw new Error(data.error||'WPS 플러그인 조회 실패');wpsProcesses=data.processes||[];updateWpsProcesses();document.querySelector('#wpsResult').value=`WPS 플러그인 ${wpsProcesses.length}개를 발견했습니다.`}catch(error){document.querySelector('#wpsResult').value='오류: '+error.message}}
function updateWpsInputMode(){const isWkt=document.querySelector('#wpsInputMode').value==='WKT',layer=document.querySelector('#wpsLayer'),wkt=document.querySelector('#wpsWkt'),wktCrs=document.querySelector('#wpsWktCrs');document.querySelector('#wpsLayerField').style.display=isWkt?'none':'block';document.querySelector('#wpsWktField').style.display=isWkt?'block':'none';document.querySelector('#wpsWktCrsField').style.display=isWkt?'block':'none';layer.required=!isWkt;wkt.required=isWkt;wktCrs.required=isWkt;updateWpsProcesses()}
function updateWpsProcesses(){const mode=document.querySelector('#wpsInputMode').value,name=document.querySelector('#wpsLayer').value,layer=layers.find(item=>item.name===name),process=document.querySelector('#wpsProcess'),vectorTypes=['shp','dxf','gpkg','postgis'];let options;if(mode==='WKT')options=wpsProcesses.filter(item=>!item.layer_types.length||item.layer_types.some(type=>vectorTypes.includes(type)));else if(layer)options=wpsProcesses.filter(item=>!item.layer_types.length||item.layer_types.includes(layer.type));else options=[];process.innerHTML=options.map(item=>`<option value="${esc(item.id)}">${esc(uiLang==='en'?item.title_en:item.title_ko)}</option>`).join('');updateWpsOptions()}
function buildWpsParams(){const mode=document.querySelector('#wpsInputMode').value,process=document.querySelector('#wpsProcess').value,params=new URLSearchParams({SERVICE:'WPS',REQUEST:'Execute',IDENTIFIER:process,INPUT:mode,PUBLISH:'true'});if(mode==='WKT'){params.set('WKT',document.querySelector('#wpsWkt').value);params.set('WKT_CRS',document.querySelector('#wpsWktCrs').value)}else params.set('LAYER',document.querySelector('#wpsLayer').value);document.querySelectorAll('[data-wps-param]').forEach(input=>{if(input.value!=='')params.set(input.dataset.wpsParam,input.value)});return params}
function updateWpsCallUrl(){const process=document.querySelector('#wpsProcess').value;if(!process){document.querySelector('#wpsCallUrl').value='';return}document.querySelector('#wpsCallUrl').value=location.origin+'/wps?'+buildWpsParams()}
function updateWpsOptions(){const process=wpsProcesses.find(item=>item.id===document.querySelector('#wpsProcess').value),container=document.querySelector('#wpsParameters');container.innerHTML=process?(process.parameters||[]).map(parameter=>{const title=esc(uiLang==='en'?parameter.title_en:parameter.title_ko);if(parameter.type==='layer'){const allowed=parameter.layer_types||[],options=layers.filter(layer=>layer.enabled&&(!allowed.length||allowed.includes(layer.type))).map(layer=>`<option value="${esc(layer.name)}">${esc(layer.title)} (${esc(layer.type)})</option>`).join('');return `<label>${title}<select data-wps-param="${esc(parameter.name)}" ${parameter.required?'required':''}><option value="">선택</option>${options}</select></label>`}const inputType=parameter.type==='number'?'number':parameter.type==='color'?'color':'text';return `<label>${title}<input data-wps-param="${esc(parameter.name)}" type="${inputType}" value="${esc(parameter.default??'')}" ${parameter.type==='number'?'step="any"':''} ${parameter.required?'required':''}></label>`}).join(''):'';updateWpsCallUrl()}
document.querySelector('#wpsParameters').addEventListener('input',updateWpsCallUrl);
document.querySelector('#wpsWkt').addEventListener('input',updateWpsCallUrl);document.querySelector('#wpsWktCrs').addEventListener('input',updateWpsCallUrl);
wpsForm.onsubmit=async event=>{event.preventDefault();const button=document.querySelector('#wpsSubmit'),result=document.querySelector('#wpsResult'),download=document.querySelector('#wpsDownload'),preview=document.querySelector('#wpsPreview');button.disabled=true;download.style.display='none';preview.style.display='none';preview.dataset.layer='';if(wpsObjectUrl){URL.revokeObjectURL(wpsObjectUrl);wpsObjectUrl=null}result.value='처리 중…';try{const process=document.querySelector('#wpsProcess').value,params=buildWpsParams();const response=await fetch('/wps?'+params),contentType=response.headers.get('content-type')||'';if(contentType.includes('json')){const data=await response.json();if(!response.ok)throw new Error(data.error||'WPS 실행 실패');result.value=JSON.stringify(data,null,2);if(data.layer){wpsPreviewLayers[data.layer.name]=data.layer;download.href=data.download_url;download.style.display='inline-block';preview.dataset.layer=data.layer.name;preview.style.display='inline-block'}}else{if(!response.ok)throw new Error('WPS 실행 실패');const blob=await response.blob();wpsObjectUrl=URL.createObjectURL(blob);const disposition=response.headers.get('content-disposition')||'',match=disposition.match(/filename=\"?([^\";]+)\"?/i);download.href=wpsObjectUrl;download.download=match?match[1]:(process.replace('.','_')+(contentType.includes('tiff')?'.tif':'.geojson'));download.style.display='inline-block';result.value=`완료\\n프로세스: ${process}\\n파일 크기: ${(blob.size/1024).toFixed(1)} KB`}}catch(error){result.value='오류: '+error.message}finally{button.disabled=false}};
document.querySelector('#wpsDownload').insertAdjacentHTML('afterend',' <button type="button" id="wpsPreview" class="btn primary" style="display:none" onclick="showWpsPreview()">지도에서 보기</button>');
function showWpsPreview(){const name=document.querySelector('#wpsPreview').dataset.layer;if(!name)return;wpsDialog.close();previewLayer(name)}
wpsDialog.addEventListener('close',()=>{document.querySelector('#wpsPreview').style.display='none';if(wpsObjectUrl){URL.revokeObjectURL(wpsObjectUrl);wpsObjectUrl=null}});
form.onsubmit=async e=>{e.preventDefault();const button=e.submitter;button.disabled=true;try{const d=Object.fromEntries(new FormData(form));delete d.upload_files;delete d.upload_sld;d.enabled=form.enabled.checked;if(d.type!=='postgis'&&document.querySelector('#layerFiles').files.length)d.path=await uploadFiles(document.querySelector('#layerFiles').files,d.type);if(document.querySelector('#sldFile').files.length)d.sld_path=await uploadSld(document.querySelector('#sldFile').files[0]);if(d.type!=='postgis'&&!d.path)throw new Error('레이어 파일을 선택해 주세요.');const r=await fetch(editing?'/api/layers/'+encodeURIComponent(editing):'/api/layers',{method:editing?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok){const x=await r.json();throw new Error(x.detail||'저장하지 못했습니다.')}dialog.close();notify('저장했습니다.');load()}catch(error){alert(error.message)}finally{button.disabled=false}};
async function removeLayer(name){if(!confirm(`'${name}' 레이어를 삭제할까요?\\nCSV에서도 제거됩니다.`))return;const r=await fetch('/api/layers/'+encodeURIComponent(name),{method:'DELETE'});if(!r.ok)return alert('삭제하지 못했습니다.');notify('삭제했습니다.');load()}
async function previewLayer(name){const l=layers.find(x=>x.name===name)||wpsPreviewLayers[name];if(!l)return alert('레이어를 찾을 수 없습니다.');if(!window.L)return alert('Leaflet을 불러오지 못했습니다. 인터넷 연결을 확인해 주세요.');activePreview=name;activePreviewFilter='';document.querySelector('#previewFilter').value='';document.querySelector('#previewFilterHint').textContent='';if(l.type!=='raster')fetch('/api/layers/'+encodeURIComponent(name)+'/fields').then(response=>response.json()).then(data=>{if(data.fields)document.querySelector('#previewFilterHint').textContent='사용 가능한 필드: '+data.fields.join(', ')}).catch(()=>{});
 document.querySelector('#previewTitle').textContent=l.title||l.name;const selector=document.querySelector('#previewService');selector.innerHTML=`<option value="WMS">${uiLang==='en'?'WMS Single Image':'WMS 단일 이미지'}</option><option value="WMS_TILED">${uiLang==='en'?'WMS Tiled':'WMS 타일 방식'}</option><option value="TMS">TMS Tiles</option>`+(l.type==='raster'?'':`<option value="WFS">${t('wfs')}</option>`);selector.value='WMS';activePreviewMode='WMS';previewDialog.showModal();
 if(!map){map=L.map('map',{center:[36.4,127.8],zoom:7});L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'© OpenStreetMap contributors'}).addTo(map);map.on('click',identifyAt);map.on('zoomstart',prepareWmsZoom);map.on('moveend',scheduleWmsRefresh)}
 setTimeout(()=>map.invalidateSize(),0);await changePreviewService()}
function clearPreviewLayers(){if(wmsLayer&&map){map.removeLayer(wmsLayer);wmsLayer=null}if(wfsLayer&&map){map.removeLayer(wfsLayer);wfsLayer=null}}
function applyPreviewFilter(){activePreviewFilter=document.querySelector('#previewFilter').value.trim();changePreviewService()}
function clearPreviewFilter(){document.querySelector('#previewFilter').value='';activePreviewFilter='';changePreviewService()}
function prepareWmsZoom(){if(activePreviewMode==='WMS'&&wmsLayer){wmsLayer.setOpacity(0);document.querySelector('#previewMeta').textContent=`${activePreview} · 새 축척 렌더링 대기…`}}
function scheduleWmsRefresh(){if(previewRefreshTimer)clearTimeout(previewRefreshTimer);if(activePreview&&activePreviewMode==='WMS')previewRefreshTimer=setTimeout(refreshWmsPreview,350)}
async function refreshWmsPreview(){if(!map||!activePreview||activePreviewMode!=='WMS')return;const seq=++previewRequestSeq,bounds=map.getBounds(),size=map.getSize(),width=Math.min(1200,Math.max(256,Math.round(size.x*1.1))),height=Math.min(900,Math.max(256,Math.round(size.y*1.1))),bbox=[bounds.getWest(),bounds.getSouth(),bounds.getEast(),bounds.getNorth()],params=new URLSearchParams({SERVICE:'WMS',VERSION:'1.1.1',REQUEST:'GetMap',LAYERS:activePreview,SRS:'EPSG:4326',BBOX:bbox.join(','),WIDTH:String(width),HEIGHT:String(height),FORMAT:'image/png',TRANSPARENT:'true',v:'9'});if(activePreviewFilter)params.set('CQL_FILTER',activePreviewFilter);document.querySelector('#previewMeta').textContent=`${activePreview} · WMS 렌더링 중…`;try{const response=await fetch('/wms?'+params);if(!response.ok){const message=(await response.text()).replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim();throw new Error(message||`HTTP ${response.status}`)}const blob=await response.blob();if(seq!==previewRequestSeq||activePreviewMode!=='WMS')return;const objectUrl=URL.createObjectURL(blob),nextLayer=L.imageOverlay(objectUrl,[[bbox[1],bbox[0]],[bbox[3],bbox[2]]],{opacity:.85});nextLayer.once('load',()=>{URL.revokeObjectURL(objectUrl);if(seq!==previewRequestSeq||activePreviewMode!=='WMS'){map.removeLayer(nextLayer);return}if(wmsLayer)map.removeLayer(wmsLayer);wmsLayer=nextLayer;document.querySelector('#previewMeta').textContent=`${activePreview} · WMS ${width}×${height}${activePreviewFilter?' · '+activePreviewFilter:''}`});nextLayer.once('error',()=>{URL.revokeObjectURL(objectUrl);map.removeLayer(nextLayer);if(seq===previewRequestSeq)document.querySelector('#previewMeta').textContent='WMS 이미지 표시 실패'});nextLayer.addTo(map)}catch(error){if(wmsLayer)wmsLayer.setOpacity(.85);if(seq===previewRequestSeq)document.querySelector('#previewMeta').textContent='WMS 렌더링 실패 · '+error.message}}
function propertyTable(properties){return `<table style="margin-top:5px">${Object.entries(properties||{}).slice(0,30).map(([key,value])=>`<tr><th style="text-align:left;padding:3px 8px 3px 0;color:#667085">${esc(key)}</th><td style="padding:3px 0">${esc(value===null?'-':value)}</td></tr>`).join('')}</table>`}
async function changePreviewService(){if(!activePreview||!map)return;const l=layers.find(x=>x.name===activePreview)||wpsPreviewLayers[activePreview],mode=document.querySelector('#previewService').value;activePreviewMode=mode;document.querySelector('#previewFilterBar').style.display=l.type!=='raster'&&['WMS','WMS_TILED'].includes(mode)?'flex':'none';clearPreviewLayers();document.querySelector('#previewMeta').textContent=`${l.name} · ${l.type} · ${mode} 미리보기`;
 if(mode==='TMS'){wmsLayer=L.tileLayer('/tiles/'+encodeURIComponent(l.name)+'/{z}/{x}/{y}.png',{tileSize:256,maxZoom:22,opacity:.85,keepBuffer:2,errorTileUrl:''}).addTo(map);try{const r=await fetch('/api/layers/'+encodeURIComponent(l.name)+'/bounds');if(!r.ok)throw new Error();const b=(await r.json()).bounds;map.fitBounds([[b[1],b[0]],[b[3],b[2]]],{padding:[24,24],maxZoom:17});document.querySelector('#previewMeta').textContent=`${l.name} · ${l.type} · TMS Tiles`}catch{map.setView([36.4,127.8],7);document.querySelector('#previewMeta').textContent+=' · 영역 자동 이동 실패'}}
 else if(mode==='WMS_TILED'){const wmsOptions={layers:l.name,format:'image/png',transparent:true,version:'1.1.1',tileSize:512,opacity:.85,updateWhenZooming:false,updateWhenIdle:true,keepBuffer:1};if(activePreviewFilter)wmsOptions.CQL_FILTER=activePreviewFilter;wmsLayer=L.tileLayer.wms(location.origin+'/wms',wmsOptions).addTo(map);try{const r=await fetch('/api/layers/'+encodeURIComponent(l.name)+'/bounds');if(!r.ok)throw new Error();const b=(await r.json()).bounds;map.fitBounds([[b[1],b[0]],[b[3],b[2]]],{padding:[24,24],maxZoom:17});document.querySelector('#previewMeta').textContent=`${l.name} · ${l.type} · WMS 타일 방식${activePreviewFilter?' · '+activePreviewFilter:''}`}catch{map.setView([36.4,127.8],7);document.querySelector('#previewMeta').textContent+=' · 영역 자동 이동 실패'}}
 else if(mode==='WMS'){try{document.querySelector('#previewMeta').textContent+=' · 영역 확인 중…';const r=await fetch('/api/layers/'+encodeURIComponent(l.name)+'/bounds');if(!r.ok)throw new Error();const b=(await r.json()).bounds;map.fitBounds([[b[1],b[0]],[b[3],b[2]]],{padding:[24,24],maxZoom:17});scheduleWmsRefresh()}catch{map.setView([36.4,127.8],7);document.querySelector('#previewMeta').textContent+=' · 영역 자동 이동 실패'}}
 else{document.querySelector('#previewMeta').textContent+=' · 불러오는 중…';try{const maxRecords=Number(l.max_records||5000),params=new URLSearchParams({SERVICE:'WFS',REQUEST:'GetFeature',TYPENAMES:l.name,SRSNAME:'EPSG:4326',COUNT:String(maxRecords)}),response=await fetch('/wfs?'+params),data=await response.json();if(!response.ok)throw new Error(data.error||'WFS 조회 실패');wfsLayer=L.geoJSON(data,{style:{color:l.style?.stroke||'#0055cc',fillColor:l.style?.fill||'#66aaff',fillOpacity:Number(l.style?.fill_opacity??.25),weight:2},pointToLayer:(_feature,latlng)=>L.circleMarker(latlng,{radius:5,color:l.style?.stroke||'#0055cc',fillOpacity:.8}),onEachFeature:(feature,layer)=>layer.bindPopup(`<div style="max-height:280px;overflow:auto;min-width:220px"><b>WFS 피처</b>${propertyTable(feature.properties)}</div>`)}).addTo(map);if(wfsLayer.getBounds().isValid())map.fitBounds(wfsLayer.getBounds(),{padding:[24,24],maxZoom:17});document.querySelector('#previewMeta').textContent=`${l.name} · ${l.type} · WFS 피처 ${data.features?.length||0}/${maxRecords}건`}catch(error){document.querySelector('#previewMeta').textContent=`WFS 불러오기 실패 · ${error.message}`}}}
async function identifyAt(event){if(!activePreview||!['WMS','WMS_TILED'].includes(activePreviewMode))return;const point=map.latLngToContainerPoint(event.latlng),size=map.getSize(),bounds=map.getBounds(),params=new URLSearchParams({SERVICE:'WMS',VERSION:'1.1.1',REQUEST:'GetFeatureInfo',LAYERS:activePreview,QUERY_LAYERS:activePreview,INFO_FORMAT:'application/json',SRS:'EPSG:4326',BBOX:[bounds.getWest(),bounds.getSouth(),bounds.getEast(),bounds.getNorth()].join(','),WIDTH:Math.round(size.x),HEIGHT:Math.round(size.y),X:Math.round(point.x),Y:Math.round(point.y)});if(activePreviewFilter)params.set('CQL_FILTER',activePreviewFilter);
 const popup=L.popup().setLatLng(event.latlng).setContent('<b>속성 조회 중…</b>').openOn(map);try{const response=await fetch('/wms?'+params);const data=await response.json();if(!response.ok)throw new Error(data.error||'조회 실패');const features=data.features||[];if(!features.length){popup.setContent('<b>조회된 객체가 없습니다.</b>');return}popup.setContent(`<div style="max-height:280px;overflow:auto;min-width:220px">${features.map((feature,index)=>`<div style="margin-bottom:8px"><b>객체 ${index+1}</b>${propertyTable(feature.properties)}</div>`).join('')}</div>`)}catch(error){popup.setContent(`<b>속성 조회 실패</b><br>${esc(error.message)}`)}}
previewDialog.addEventListener('close',()=>{activePreview=null;previewRequestSeq++;if(previewRefreshTimer)clearTimeout(previewRefreshTimer);clearPreviewLayers()});
setUiLanguage(uiLang);load().catch(()=>document.querySelector('#count').textContent=uiLang==='ko'?'목록을 불러오지 못했습니다.':'Could not load layers.');
</script></body></html>""")

if __name__=='__main__':
 import uvicorn
 server_config=CONFIG.get('server',{})
 uvicorn.run('app:app',host=server_config.get('host','0.0.0.0'),port=int(os.getenv('APP_PORT',server_config.get('port',8000))),reload=False)
