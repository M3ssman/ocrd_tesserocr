from __future__ import absolute_import

import os.path
from tesserocr import PyTessBaseAPI, RIL, PSM

from ocrd import Processor
from ocrd_utils import (
    getLogger, concat_padded,
    points_from_xywh,
    MIMETYPE_PAGE
)
from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import (
    CoordsType,
    LabelType, LabelsType,
    MetadataItemType,
    TextLineType,
    to_xml
)

from .config import TESSDATA_PREFIX, OCRD_TOOL
from .common import (
    image_from_page,
    image_from_region
)

TOOL = 'ocrd-tesserocr-segment-line'
LOG = getLogger('processor.TesserocrSegmentLine')

class TesserocrSegmentLine(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super(TesserocrSegmentLine, self).__init__(*args, **kwargs)


    def process(self):
        """Performs (text) line segmentation with Tesseract on the workspace.
        
        Open and deserialize PAGE input files and their respective images,
        then iterate over the element hierarchy down to the region level,
        and remove any existing TextLine elements (unless `overwrite_lines`
        is False).
        
        Set up Tesseract to detect lines, and add each one to the region
        at the detected coordinates.
        
        Produce a new output file by serialising the resulting hierarchy.
        """
        overwrite_lines = self.parameter['overwrite_lines']
        
        with PyTessBaseAPI(
                psm=PSM.SINGLE_BLOCK,
                path=TESSDATA_PREFIX
        ) as tessapi:
            for (n, input_file) in enumerate(self.input_files):
                page_id = input_file.pageId or input_file.ID
                LOG.info("INPUT FILE %i / %s", n, page_id)
                pcgts = page_from_file(self.workspace.download_file(input_file))
                metadata = pcgts.get_Metadata() # ensured by from_file()
                metadata.add_MetadataItem(
                    MetadataItemType(type_="processingStep",
                                     name=self.ocrd_tool['steps'][0],
                                     value=TOOL,
                                     # FIXME: externalRef is invalid by pagecontent.xsd, but ocrd does not reflect this
                                     # what we want here is `externalModel="ocrd-tool" externalId="parameters"`
                                     Labels=[LabelsType(#externalRef="parameters",
                                                        Label=[LabelType(type_=name,
                                                                         value=self.parameter[name])
                                                               for name in self.parameter.keys()])]))
                page = pcgts.get_Page()
                page_image = self.workspace.resolve_image_as_pil(page.imageFilename)
                dpi = page_image.info.get('dpi', (0,0))[0]
                if dpi:
                    tessapi.SetVariable('user_defined_dpi', str(dpi))
                page_image, page_xywh = image_from_page(
                    self.workspace, page, page_image, page_id)
                
                for region in page.get_TextRegion():
                    if region.get_TextLine():
                        if overwrite_lines:
                            LOG.info('removing existing TextLines in region "%s"', region.id)
                            region.set_TextLine([])
                        else:
                            LOG.warning('keeping existing TextLines in region "%s"', region.id)
                    LOG.debug("Detecting lines in region '%s'", region.id)
                    region_image, region_xywh = image_from_region(
                        self.workspace, region, page_image, page_xywh)
                    tessapi.SetImage(region_image)
                    for line_no, component in enumerate(tessapi.GetComponentImages(RIL.TEXTLINE, True, raw_image=True)):
                        line_id = '%s_line%04d' % (region.id, line_no)
                        line_xywh = component[1]
                        line_xywh['x'] += region_xywh['x']
                        line_xywh['y'] += region_xywh['y']
                        line_points = points_from_xywh(line_xywh)
                        region.add_TextLine(TextLineType(
                            id=line_id, Coords=CoordsType(line_points)))
                
                # Use input_file's basename for the new file -
                # this way the files retain the same basenames:
                file_id = input_file.ID.replace(self.input_file_grp, self.output_file_grp)
                if file_id == input_file.ID:
                    file_id = concat_padded(self.output_file_grp, n)
                self.workspace.add_file(
                    ID=file_id,
                    file_grp=self.output_file_grp,
                    pageId=input_file.pageId,
                    mimetype=MIMETYPE_PAGE,
                    local_filename=os.path.join(self.output_file_grp,
                                                file_id + '.xml'),
                    content=to_xml(pcgts))
