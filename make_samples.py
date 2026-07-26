from pathlib import Path
import numpy as np, rasterio, geopandas as gpd, ezdxf
from rasterio.transform import from_origin
from shapely.geometry import Polygon
BASE=Path(__file__).resolve().parent; D=BASE/'data'; D.mkdir(exist_ok=True)
# DEM
w=h=160; x=np.linspace(-1,1,w); y=np.linspace(-1,1,h); xx,yy=np.meshgrid(x,y)
a=(120+45*np.exp(-3*(xx**2+yy**2))+10*np.sin(xx*7)*np.cos(yy*6)).astype('float32')
with rasterio.open(D/'sample_dem.tif','w',driver='GTiff',height=h,width=w,count=1,dtype='float32',crs='EPSG:5186',transform=from_origin(200000,550000,10,10),nodata=-9999,compress='deflate') as dst: dst.write(a,1)
# SHP
gdf=gpd.GeoDataFrame({'id':[1,2],'name':['A구역','B구역']},geometry=[Polygon([(200100,549900),(200700,549900),(200700,549300),(200100,549300)]),Polygon([(200800,549800),(201400,549800),(201400,549200),(200800,549200)])],crs='EPSG:5186')
gdf.to_file(D/'sample_area.shp',encoding='UTF-8')
# DXF
doc=ezdxf.new('R2010'); m=doc.modelspace(); m.add_lwpolyline([(200150,549850),(200600,549850),(200600,549400),(200150,549400)],close=True); m.add_circle((201050,549500),220); m.add_line((200000,549200),(201500,549900)); m.add_text('Neunggureongi',dxfattribs={'height':35,'insert':(200650,549700)}); doc.saveas(D/'sample_cad.dxf')
print('Sample data created.')
