from __future__ import absolute_import
import os.path

import tesserocr
from ocrd_utils import (
    getLogger, concat_padded,
    MIMETYPE_PAGE
)
from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import (
    MetadataItemType,
    LabelsType, LabelType,
    CoordsType, AlternativeImageType,
    to_xml
)
from ocrd_models.ocrd_page_generateds import BorderType
from ocrd_models import OcrdExif
from ocrd import Processor

from .config import TESSDATA_PREFIX, OCRD_TOOL
from .common import (
    bbox_from_points, points_from_bbox,
    bbox_from_xywh, save_image_file
)

TOOL = 'ocrd-tesserocr-crop'
LOG = getLogger('processor.TesserocrCrop')
FILEGRP_IMG = 'OCR-D-IMG-CROP'

class TesserocrCrop(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super(TesserocrCrop, self).__init__(*args, **kwargs)

    def process(self):
        """Performs page cropping with Tesseract on the workspace.
        
        Open and deserialize PAGE input files and their respective images.
        Set up Tesseract to detect text blocks on each page, and find
        the largest coordinate extent spanning all of them. Use this
        extent in defining a Border, and add that to the page.
        
        Produce new output files by serialising the resulting hierarchy.
        """
        padding = self.parameter['padding']

        with tesserocr.PyTessBaseAPI(path=TESSDATA_PREFIX) as tessapi:
            # disable table detection here (tables count as text blocks),
            # because we do not want to risk confusing the spine with
            # a column separator and thus creeping into a neighbouring
            # page:
            tessapi.SetVariable("textord_tabfind_find_tables", "0")
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
                border = page.get_Border()
                if border:
                    left, top, right, bottom = bbox_from_points(border.get_Coords().points)
                    LOG.warning('Overwriting existing Border: %i:%i,%i:%i',
                                left, top, right, bottom)
                regions = page.get_TextRegion()
                if regions:
                    min_x = image.width
                    min_y = image.height
                    max_x = 0
                    max_y = 0
                    for region in regions:
                        left, top, right, bottom = bbox_from_points(region.get_Coords().points)
                        min_x = min(min_x, left)
                        min_y = min(min_y, top)
                        max_x = max(max_x, right)
                        max_y = max(max_y, bottom)
                    LOG.warning('Ignoring extent from existing TextRegions: %i:%i,%i:%i',
                                min_x, max_x, min_y, max_y)
                
                page_image = self.workspace.resolve_image_as_pil(page.imageFilename)
                page_image_info = OcrdExif(page_image)
                if page_image_info.xResolution != 1:
                    dpi = page_image_info.xResolution
                    if page_image_info.resolutionUnit == 'cm':
                        dpi = round(dpi * 2.54)
                    tessapi.SetVariable('user_defined_dpi', str(dpi))
                    zoom = 300 / dpi
                else:
                    zoom = 1
                LOG.debug("Cropping with tesseract")
                tessapi.SetImage(page_image)
                # PSM.SPARSE_TEXT: get as much text as possible in no particular order
                # PSM.AUTO (default): includes tables (dangerous)
                tessapi.SetPageSegMode(tesserocr.PSM.SPARSE_TEXT)
                #
                # helper variables for saving the box coordinates
                #
                min_x = page_image.width
                min_y = page_image.height
                max_x = 0
                max_y = 0
                # iterate over all text blocks and compare their
                # bbox extent to the running min and max values
                for component in tessapi.GetComponentImages(tesserocr.RIL.BLOCK, True):
                    image, xywh, index, para = component
                    #
                    # the region reference in the reading order element
                    #
                    ID = "region%04d" % index
                    left, top, right, bottom = bbox_from_xywh(xywh)
                    LOG.debug("Detected text region '%s': %i:%i,%i:%i",
                              ID, left, right, top, bottom)
                    # filter region results:
                    bin_bbox = image.getbbox()
                    if not bin_bbox:
                        # this does happen!
                        LOG.debug("Ignoring region '%s' because its binarization is empty", ID)
                        continue
                    width = bin_bbox[2]-bin_bbox[0]
                    if width < 25 / zoom:
                        # we must be conservative here: page numbers are tiny regions, too!
                        LOG.debug("Ignoring region '%s' because its width is too small (%d)", ID, width)
                        continue
                    height = bin_bbox[3]-bin_bbox[1]
                    if height < 25 / zoom:
                        # we must be conservative here: page numbers are tiny regions, too!
                        LOG.debug("Ignoring region '%s' because its height is too small (%d)", ID, height)
                        continue
                    min_x = min(min_x, left)
                    min_y = min(min_y, top)
                    max_x = max(max_x, right)
                    max_y = max(max_y, bottom)
                    LOG.debug("Updated page border: %i:%i,%i:%i", min_x, max_x, min_y, max_y)

                #
                # set the identified page border
                #
                if min_x < max_x and min_y < max_y:
                    # add padding:
                    min_x = max(min_x - padding, 0)
                    max_x = min(max_x + padding, page_image.width)
                    min_y = max(min_y - padding, 0)
                    max_y = min(max_y + padding, page_image.height)
                    LOG.debug("Padded page border: %i:%i,%i:%i", min_x, max_x, min_y, max_y)
                    border = BorderType(Coords=CoordsType(
                        points_from_bbox(min_x, min_y, max_x, max_y)))
                    # update PAGE (annotate border):
                    page.set_Border(border)
                    # update METS (add the image file):
                    page_image = page_image.crop(
                        box=(min_x, min_y, max_x, max_y))
                    file_id = input_file.ID.replace(self.input_file_grp, FILEGRP_IMG)
                    if file_id == input_file.ID:
                        file_id = concat_padded(FILEGRP_IMG, n)
                    file_path = save_image_file(self.workspace, page_image,
                                                file_id,
                                                page_id=page_id,
                                                file_grp=FILEGRP_IMG)
                    # update PAGE (reference the image file):
                    page.add_AlternativeImage(AlternativeImageType(
                        filename=file_path, comments="cropped"))
                else:
                    LOG.error("Cannot find valid extent for page '%s'", page_id)

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
