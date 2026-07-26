CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS public.sample_area (
  id bigserial PRIMARY KEY,
  name varchar(100) NOT NULL,
  geom geometry(Polygon, 5186) NOT NULL
);
CREATE INDEX IF NOT EXISTS sample_area_geom_gix ON public.sample_area USING gist (geom);
INSERT INTO public.sample_area(name, geom)
SELECT 'PostGIS sample', ST_GeomFromText(
  'POLYGON((200000 550000,201000 550000,201000 551000,200000 551000,200000 550000))',5186)
WHERE NOT EXISTS (SELECT 1 FROM public.sample_area);
