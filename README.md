# Project Neunggureongi v1 + PostGIS

Python 기반 WMS/WFS/WCS/WPS 서버입니다. GeoTIFF/DEM, SHP, DXF, PostgreSQL/PostGIS 레이어를 지원합니다.

## 실행

1. Docker Desktop 설치
2. `start_postgis.bat` 실행
3. `install_and_run.bat` 실행
4. 레이어 관리 화면 접속: `http://127.0.0.1:8001/admin/layers`

API 문서는 `http://127.0.0.1:8001/docs`에서 확인할 수 있습니다.

## 서버 포트 설정

`config.json`의 `server` 항목에서 서버 주소와 포트를 설정합니다.

```json
"server": {
  "host": "0.0.0.0",
  "port": 8001,
  "fallback_on_conflict": true,
  "fallback_max_port": 8010
}
```

- `port`: 우선 사용할 포트
- `fallback_on_conflict`: 포트 충돌 시 다음 포트를 자동으로 사용할지 여부
- `fallback_max_port`: 자동 탐색할 마지막 포트

실제로 선택된 포트는 콘솔과 `server.port` 파일에서 확인할 수 있습니다.

## PROJ 좌표변환

`pyproj`의 PROJ 엔진을 사용해 좌표를 변환할 수 있습니다. 관리 화면의 `좌표 변환` 도구에서 원본·대상 CRS와 `X,Y` 좌표를 입력하거나 인증된 API를 사용할 수 있습니다.

```http
POST /api/transform
Content-Type: application/json

{
  "source_crs": "EPSG:4326",
  "target_crs": "EPSG:5186",
  "coordinates": [[127.0, 37.5]]
}
```

EPSG 코드, WKT, PROJ 문자열을 지원하며 한 요청에 최대 10,000개 좌표를 변환합니다. 축 순서는 항상 `X,Y`입니다.

## 관리자 인증

최초 실행 후 서버 PC에서 `http://127.0.0.1:8001/admin/setup`에 접속해 관리자 ID와 8자 이상의 비밀번호를 만드십시오. 이후 관리 화면과 레이어 관리 API는 로그인이 필요합니다.

- 암호화 키: `.admin.key`
- 암호화 자격증명: `data/admin_credentials.bin`
- 로그인 세션 유효시간: 8시간

두 파일은 Git에서 제외됩니다. 운영 환경에서는 `.admin.key` 파일 대신 `ADMIN_ENCRYPTION_KEY` 환경변수를 사용할 수 있습니다. 계정을 터미널에서 변경하려면 다음 명령을 실행하십시오.

```bat
venv\Scripts\python.exe manage_admin.py
```

ID 또는 비밀번호를 잊었다면 서버 PC에서 `reset_admin_password.bat`를 실행하십시오. 확인 문구 `RESET`을 입력한 후 새 ID와 비밀번호를 설정할 수 있습니다. 초기화하면 암호화 키가 교체되어 기존 로그인 세션이 모두 무효화되며, 기존 키와 인증 파일은 `data/admin_auth_backups/날짜_시간/`에 보관됩니다.

암호화 키를 분실하면 기존 자격증명 파일을 복호화할 수 없으므로 별도로 안전하게 백업하십시오.

## 레이어 관리

레이어 목록의 원본은 프로젝트 루트의 `layers.csv`입니다. 관리 화면에서 레이어를 추가·수정·삭제하거나 활성 상태를 변경하면 CSV에 즉시 저장되며 서버 재시작 없이 OGC 서비스에 반영됩니다.

파일 레이어를 추가할 때는 관리 화면에서 파일을 직접 업로드합니다.

- GeoTIFF: `.tif` 또는 `.tiff`
- DXF: `.dxf`
- Shapefile: 같은 이름의 `.shp`, `.shx`, `.dbf`를 함께 선택하고 필요하면 `.prj`, `.cpg`도 추가
- GeoPackage: `.gpkg` 파일을 선택하고 내부 레이어명은 `table`에 입력합니다. 비우면 첫 공간 레이어를 사용합니다.

업로드 파일은 `data/uploads/업로드그룹/`에 저장되며 CSV에는 서버 저장 경로가 자동으로 기록됩니다. 최대 업로드 크기는 `config.json`의 `server.upload_max_mb`에서 설정합니다.

### SLD 스타일

레이어 추가·수정 화면에서 `.sld` 파일을 선택하면 WMS 렌더링에 자동 적용됩니다.

- 벡터: `stroke`, `stroke-width`, `fill`, `fill-opacity`, Point `Size`
- 래스터: `RasterSymbolizer`의 `ColorMapEntry`
- 예제: `data/styles/example_vector.sld`, `data/styles/example_raster.sld`

SLD 파일 경로는 `layers.csv`의 `sld_path` 열에 저장됩니다. SLD가 변경되면 해당 WMS 타일 캐시는 자동으로 무효화됩니다.

주요 CSV 열:

- `name`, `title`, `type`, `crs`
- 파일 레이어: `path`
- PostGIS 레이어: `schema`, `table`, `geometry_column`, `columns`
- WFS 조회 상한: `max_records` (1~100,000)
- 스타일: `stroke`, `fill`, `fill_opacity`
- 서비스 노출 여부: `enabled`

CSV를 외부 편집기로 직접 수정할 때는 UTF-8로 저장하십시오. `config.json`의 기존 레이어 목록은 `layers.csv`가 없을 때 최초 생성용으로만 사용됩니다.

## PostGIS

- 상태: `http://127.0.0.1:8001/api/postgis/health`
- 공간 테이블 목록: `http://127.0.0.1:8001/api/postgis/tables`
- 기본 DB: `neunggureongi`
- 기본 사용자: `neung`
- 기본 포트: `5432`

운영 환경에서는 기본 비밀번호를 변경하고 `POSTGIS_URL` 환경 변수를 사용하십시오.

## 서비스 예시

- WMS: `/wms?SERVICE=WMS&REQUEST=GetCapabilities`
- WFS: `/wfs?SERVICE=WFS&REQUEST=GetFeature&TYPENAMES=postgis_sample&COUNT=100`
- WCS: `/wcs?SERVICE=WCS&REQUEST=GetCapabilities`
- WPS: `/wps?SERVICE=WPS&REQUEST=GetCapabilities`

### WPS 관리 UI

레이어 관리 화면의 `WPS 작업` 버튼에서 레이어별 공간 처리를 실행할 수 있습니다.

- `raster.statistics`: 래스터 최소·최대·평균·표준편차
- `terrain.slope`: 경사도 GeoTIFF 생성
- `terrain.hillshade`: 음영기복 GeoTIFF 생성
- `vector.buffer`: 지정 거리의 버퍼 GeoJSON 생성
- `vector.heatmap`: 벡터 중심점 밀도 heatmap GeoTIFF 생성

통계 결과는 화면에 표시되며 GeoJSON 결과는 브라우저에서 다운로드할 수 있습니다. 관리 UI에서 생성한 GeoTIFF는 24시간 동안 임시 WMS 레이어로 자동 발행되어 `지도에서 보기`로 바로 확인할 수 있고, 원본 파일도 다운로드할 수 있습니다.

외부 호출에서 기존 파일 응답이 필요하면 종전과 같이 실행하고, WMS 발행 참조가 필요하면 `PUBLISH=true`를 추가합니다.

```text
/wps?SERVICE=WPS&REQUEST=Execute&IDENTIFIER=vector.heatmap&LAYER=sample_area&RADIUS_M=1000&PIXEL_SIZE_M=100&PUBLISH=true
```

발행 응답에는 `layer_name`, `wms_url`, `download_url`, `bounds`, `expires_at`이 포함됩니다. 임시 레이어와 결과 파일은 만료 후 다음 WPS/WMS 접근 시 정리됩니다.

#### WPS 플러그인 추가

WPS 알고리즘은 `wps/` 폴더에서 소스 파일 하나당 하나의 플러그인으로 관리됩니다. `wps/_plugin_template.py`를 복사하고 파일명 앞의 `_`를 제거한 뒤 다음 두 항목을 구현하면 자동 등록됩니다.

- `PROCESS`: ID, 한글·영문 이름, 지원 레이어 형식, 입력 파라미터, 출력 형식
- `execute(layer, parameters, context)`: `WpsResult` 반환

새 `.py` 파일은 WPS GetCapabilities, DescribeProcess, `/api/wps/processes`, 관리 UI에서 자동 발견됩니다. 파일명이 `_`로 시작하면 비활성 템플릿으로 간주되어 로딩하지 않습니다.
