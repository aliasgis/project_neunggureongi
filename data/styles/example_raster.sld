<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
  xmlns="http://www.opengis.net/sld">
  <NamedLayer>
    <Name>example_raster</Name>
    <UserStyle>
      <FeatureTypeStyle>
        <Rule>
          <RasterSymbolizer>
            <ColorMap>
              <ColorMapEntry color="#173f5f" quantity="110" label="낮음"/>
              <ColorMapEntry color="#3caea3" quantity="130" label="중간"/>
              <ColorMapEntry color="#f6d55c" quantity="150" label="높음"/>
              <ColorMapEntry color="#ed553b" quantity="175" label="매우 높음"/>
            </ColorMap>
          </RasterSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
