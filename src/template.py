"""

OMRChecker

Author: Udayraj Deshmukh
Github: https://github.com/Udayraj123

"""

from src.color import apply_dropout, normalize_dropout
from src.constants.common import FIELD_TYPES
from src.core import ImageInstanceOps
from src.logger import logger
from src.processors.manager import PROCESSOR_MANAGER
from src.rules import RuleSet
from src.utils.image import ImageUtils
from src.utils.parsing import (
    custom_sort_output_columns,
    open_template_with_defaults,
    parse_fields,
)


class Template:
    def __init__(self, template_path, tuning_config, overrides=None):
        """overrides: top-level template keys that replace the file's (e.g. colorDropout)."""
        self.path = template_path
        self.image_instance_ops = ImageInstanceOps(tuning_config)

        json_object = open_template_with_defaults(template_path, overrides)
        self.color_dropout = normalize_dropout(json_object.get("colorDropout"))
        # Template-level alignment settings over config alignment_params
        self.alignment = dict(json_object.get("alignment") or {})
        (
            custom_labels_object,
            field_blocks_object,
            output_columns_array,
            pre_processors_object,
            self.bubble_dimensions,
            self.global_empty_val,
            self.options,
            self.page_dimensions,
            zones_object,
        ) = map(
            json_object.get,
            [
                "customLabels",
                "fieldBlocks",
                "outputColumns",
                "preProcessors",
                "bubbleDimensions",
                "emptyValue",
                "options",
                "pageDimensions",
                "zones",
            ],
        )

        self.parse_output_columns(output_columns_array)
        self.setup_pre_processors(pre_processors_object, template_path.parent)
        self.setup_field_blocks(field_blocks_object)
        self.setup_zones(zones_object)
        self.parse_custom_labels(custom_labels_object)
        # Output columns that identify a sheet; equal keys flag duplicates
        self.primary_key = [str(c) for c in json_object.get("primaryKey") or []]
        # Optional per-group placeholders (src/utils/parsing.py join_group)
        self.group_options = {
            name: dict(options or {})
            for name, options in (json_object.get("groupOptions") or {}).items()
            if name in self.custom_labels
        }
        # Optional "validate" and "checks" (src/rules); rule outputs are columns
        self.rules = RuleSet(
            self, json_object.get("validate"), json_object.get("checks")
        )

        non_custom_columns, all_custom_columns = (
            list(self.non_custom_labels),
            list(custom_labels_object.keys()) + self.rules.new_output_columns,
        )

        if len(self.output_columns) == 0:
            self.fill_output_columns(non_custom_columns, all_custom_columns)

        self.validate_template_columns(non_custom_columns, all_custom_columns)

    # ------------------------------------------------------------------ colour
    def set_color_dropout(self, spec):
        """Change the page colour dropout at runtime (dict, mode string or None)."""
        self.color_dropout = normalize_dropout(spec)

    def zone_dropout_specs(self):
        """Distinct per-zone dropout settings that differ from the page's."""
        specs = []
        for zone in self.zones:
            spec = zone.color_dropout
            if spec is not None and spec != self.color_dropout and spec not in specs:
                specs.append(spec)
        return specs

    @property
    def needs_color(self):
        """True when sheets should be decoded in colour (some dropout isn't plain grey)."""
        return self.color_dropout.mode != "grey" or bool(self.zone_dropout_specs())

    def prepare_image(self, image):
        """
        Turn a loaded sheet into (grey page image, {dropout spec: grey variant}).

        A grey (2-D) image is used as-is with no variants. Variants are only
        computed for zones whose colorDropout differs from the page's.
        """
        if image is None or image.ndim == 2:
            return image, {}
        grey = apply_dropout(image, self.color_dropout)
        variants = {
            spec: apply_dropout(image, spec) for spec in self.zone_dropout_specs()
        }
        return grey, variants

    def read_zones_with_variants(
        self, aligned_image, variants, engines=None, only=None
    ):
        """
        Read zones; those with their own colorDropout read the matching variant
        (registered alongside the page by apply_preprocessors' companions).
        """
        from src.readers import read_zone
        from src.readers.base import skipped_zone

        page_w, page_h = self.page_dimensions
        prepared, results = {}, {}
        for zone in self.zones if only is None else only:
            if only is None and zone.lazy:
                # Lazy fallback zones are read later, only if a check needs them
                results[zone.name] = skipped_zone(zone)
                continue
            image = aligned_image
            spec = zone.color_dropout
            if variants and spec is not None and spec in variants:
                if spec not in prepared:
                    # The same final resize/normalise the page gets before reading
                    variant = ImageUtils.resize_util(variants[spec], page_w, page_h)
                    if variant.max() > variant.min():
                        variant = ImageUtils.normalize_util(variant)
                    prepared[spec] = variant
                image = prepared[spec]
            results[zone.name] = read_zone(zone, image, engines)
        return results

    def parse_output_columns(self, output_columns_array):
        self.output_columns = parse_fields(f"Output Columns", output_columns_array)

    def setup_pre_processors(self, pre_processors_object, relative_dir):
        # load image pre_processors
        self.pre_processors = []
        for pre_processor in pre_processors_object:
            ProcessorClass = PROCESSOR_MANAGER.processors[pre_processor["name"]]
            pre_processor_instance = ProcessorClass(
                options=pre_processor["options"],
                relative_dir=relative_dir,
                image_instance_ops=self.image_instance_ops,
            )
            # Registration preprocessors warp straight into template coordinates
            pre_processor_instance.page_dimensions = self.page_dimensions
            # Registration may use the template's blocks (timing-mark joint fit)
            pre_processor_instance.template = self
            self.pre_processors.append(pre_processor_instance)

    def setup_field_blocks(self, field_blocks_object):
        # Add field_blocks
        self.field_blocks = []
        self.all_parsed_labels = set()
        for block_name, field_block_object in field_blocks_object.items():
            self.parse_and_add_field_block(block_name, field_block_object)

    def setup_zones(self, zones_object):
        self.zones = []
        page_width, page_height = self.page_dimensions
        for zone_name, zone_object in zones_object.items():
            if zone_name in self.all_parsed_labels:
                raise Exception(
                    f"Zone name '{zone_name}' overlaps with an existing field label"
                )
            (x, y), (w, h) = zone_object["origin"], zone_object["dimensions"]
            if x + w > page_width or y + h > page_height:
                raise Exception(
                    f"Overflowing zone '{zone_name}' with origin {[x, y]} and dimensions {[w, h]} in template with dimensions {self.page_dimensions}"
                )
            zone = Zone(
                zone_name,
                zone_object["type"],
                [x, y],
                [w, h],
                zone_object.get("options", {}),
            )
            if "colorDropout" in zone.options:
                zone.color_dropout = normalize_dropout(zone.options["colorDropout"])
            self.zones.append(zone)
            self.all_parsed_labels.add(zone_name)

    def parse_custom_labels(self, custom_labels_object):
        all_parsed_custom_labels = set()
        self.custom_labels = {}
        for custom_label, label_strings in custom_labels_object.items():
            parsed_labels = parse_fields(f"Custom Label: {custom_label}", label_strings)
            parsed_labels_set = set(parsed_labels)
            self.custom_labels[custom_label] = parsed_labels

            missing_custom_labels = sorted(
                parsed_labels_set.difference(self.all_parsed_labels)
            )
            if len(missing_custom_labels) > 0:
                logger.critical(
                    f"For '{custom_label}', Missing labels - {missing_custom_labels}"
                )
                raise Exception(
                    f"Missing field block label(s) in the given template for {missing_custom_labels} from '{custom_label}'"
                )

            if not all_parsed_custom_labels.isdisjoint(parsed_labels_set):
                # Note: this can be made a warning, but it's a choice
                logger.critical(
                    f"field strings overlap for labels: {label_strings} and existing custom labels: {all_parsed_custom_labels}"
                )
                raise Exception(
                    f"The field strings for custom label '{custom_label}' overlap with other existing custom labels"
                )

            all_parsed_custom_labels.update(parsed_labels)

        self.non_custom_labels = self.all_parsed_labels.difference(
            all_parsed_custom_labels
        )

    def fill_output_columns(self, non_custom_columns, all_custom_columns):
        all_template_columns = non_custom_columns + all_custom_columns
        # Typical case: sort alpha-numerical (natural sort)
        self.output_columns = sorted(
            all_template_columns, key=custom_sort_output_columns
        )

    def validate_template_columns(self, non_custom_columns, all_custom_columns):
        output_columns_set = set(self.output_columns)
        all_custom_columns_set = set(all_custom_columns)

        missing_output_columns = sorted(
            output_columns_set.difference(all_custom_columns_set).difference(
                self.all_parsed_labels
            )
        )
        if len(missing_output_columns) > 0:
            logger.critical(f"Missing output columns: {missing_output_columns}")
            raise Exception(
                f"Some columns are missing in the field blocks for the given output columns"
            )

        all_template_columns_set = set(non_custom_columns + all_custom_columns)
        missing_label_columns = sorted(
            all_template_columns_set.difference(output_columns_set)
        )
        if len(missing_label_columns) > 0:
            logger.warning(
                f"Some label columns are not covered in the given output columns: {missing_label_columns}"
            )

    def parse_and_add_field_block(self, block_name, field_block_object):
        field_block_object = self.pre_fill_field_block(field_block_object)
        block_instance = FieldBlock(block_name, field_block_object)
        self.field_blocks.append(block_instance)
        self.validate_parsed_labels(field_block_object["fieldLabels"], block_instance)

    def pre_fill_field_block(self, field_block_object):
        if "fieldType" in field_block_object:
            field_block_object = {
                **FIELD_TYPES[field_block_object["fieldType"]],
                **field_block_object,
            }
        else:
            field_block_object = {**field_block_object, "fieldType": "__CUSTOM__"}

        return {
            "direction": "vertical",
            "emptyValue": self.global_empty_val,
            "bubbleDimensions": self.bubble_dimensions,
            **field_block_object,
        }

    def validate_parsed_labels(self, field_labels, block_instance):
        parsed_field_labels, block_name = (
            block_instance.parsed_field_labels,
            block_instance.name,
        )
        field_labels_set = set(parsed_field_labels)
        if not self.all_parsed_labels.isdisjoint(field_labels_set):
            # Note: in case of two fields pointing to same column, use a custom column instead of same field labels.
            logger.critical(
                f"An overlap found between field string: {field_labels} in block '{block_name}' and existing labels: {self.all_parsed_labels}"
            )
            raise Exception(
                f"The field strings for field block {block_name} overlap with other existing fields"
            )
        self.all_parsed_labels.update(field_labels_set)

        page_width, page_height = self.page_dimensions
        block_width, block_height = block_instance.dimensions
        [block_start_x, block_start_y] = block_instance.origin

        block_end_x, block_end_y = (
            block_start_x + block_width,
            block_start_y + block_height,
        )

        if (
            block_end_x >= page_width
            or block_end_y >= page_height
            or block_start_x < 0
            or block_start_y < 0
        ):
            raise Exception(
                f"Overflowing field block '{block_name}' with origin {block_instance.origin} and dimensions {block_instance.dimensions} in template with dimensions {self.page_dimensions}"
            )

    def __str__(self):
        return str(self.path)


class FieldBlock:
    # True while the current sheet's bubbles carry rectification offsets
    rectified = False
    last_rectification = None

    def __init__(self, block_name, field_block_object):
        self.name = block_name
        self.shift = 0
        self.shift_y = 0
        self.setup_field_block(field_block_object)

    def setup_field_block(self, field_block_object):
        # case mapping
        (
            bubble_dimensions,
            bubble_values,
            bubbles_gap,
            direction,
            field_labels,
            field_type,
            labels_gap,
            origin,
            self.empty_val,
        ) = map(
            field_block_object.get,
            [
                "bubbleDimensions",
                "bubbleValues",
                "bubblesGap",
                "direction",
                "fieldLabels",
                "fieldType",
                "labelsGap",
                "origin",
                "emptyValue",
            ],
        )
        self.parsed_field_labels = parse_fields(
            f"Field Block Labels: {self.name}", field_labels
        )
        # Border rectification: None inherits alignment_params.rectify_on_border
        self.rectify_on_border = field_block_object.get("rectifyOnBorder")
        self.border_padding = field_block_object.get("borderPadding")
        self.outer_border_padding = field_block_object.get("outerBorderPadding")
        # Bubble-outline perspective fit: None inherits alignment block_perspective
        self.block_perspective = field_block_object.get("blockPerspective")
        self.origin = origin
        self.bubble_dimensions = bubble_dimensions
        self.bubbles_gap = bubbles_gap
        self.labels_gap = labels_gap
        self.calculate_block_dimensions(
            bubble_dimensions,
            bubble_values,
            bubbles_gap,
            direction,
            labels_gap,
        )
        self.generate_bubble_grid(
            bubble_values,
            bubbles_gap,
            direction,
            field_type,
            labels_gap,
        )

    def calculate_block_dimensions(
        self,
        bubble_dimensions,
        bubble_values,
        bubbles_gap,
        direction,
        labels_gap,
    ):
        _h, _v = (1, 0) if (direction == "vertical") else (0, 1)

        values_dimension = int(
            bubbles_gap * (len(bubble_values) - 1) + bubble_dimensions[_h]
        )
        fields_dimension = int(
            labels_gap * (len(self.parsed_field_labels) - 1) + bubble_dimensions[_v]
        )
        self.dimensions = (
            [fields_dimension, values_dimension]
            if (direction == "vertical")
            else [values_dimension, fields_dimension]
        )

    def generate_bubble_grid(
        self,
        bubble_values,
        bubbles_gap,
        direction,
        field_type,
        labels_gap,
    ):
        _h, _v = (1, 0) if (direction == "vertical") else (0, 1)
        self.traverse_bubbles = []
        # Generate the bubble grid
        lead_point = [float(self.origin[0]), float(self.origin[1])]
        for field_label in self.parsed_field_labels:
            bubble_point = lead_point.copy()
            field_bubbles = []
            for bubble_value in bubble_values:
                field_bubbles.append(
                    Bubble(bubble_point.copy(), field_label, field_type, bubble_value)
                )
                bubble_point[_h] += bubbles_gap
            self.traverse_bubbles.append(field_bubbles)
            lead_point[_v] += labels_gap


class Zone:
    """A rectangular non-bubble region (barcode, QR code, printed or handwritten text)"""

    def __init__(self, name, zone_type, origin, dimensions, options):
        self.name = name
        self.type = zone_type
        self.origin = origin
        self.dimensions = dimensions
        self.options = options
        self.empty_val = options.get("emptyValue", "")
        # Lazy zones are read only when a check needs them as a fallback
        self.lazy = bool(options.get("lazy", False))
        # None: read the page image; otherwise a DropoutSpec for this zone's own variant
        self.color_dropout = None

    def crop(self, image, padding=0):
        (x, y), (w, h) = self.origin, self.dimensions
        img_h, img_w = image.shape[:2]
        x0, y0 = max(x - padding, 0), max(y - padding, 0)
        x1, y1 = min(x + w + padding, img_w), min(y + h + padding, img_h)
        return image[y0:y1, x0:x1]


class Bubble:
    """
    Container for a Point Box on the OMR

    field_label is the point's property- field to which this point belongs to
    It can be used as a roll number column as well. (eg roll1)
    It can also correspond to a single digit of integer type Q (eg q5d1)
    """

    # Per-sheet position correction (border rectification); 0 unless rectified
    dx = 0
    dy = 0

    def __init__(self, pt, field_label, field_type, field_value):
        self.x = round(pt[0])
        self.y = round(pt[1])
        self.field_label = field_label
        self.field_type = field_type
        self.field_value = field_value

    def __str__(self):
        return str([self.x, self.y])
