import re
from copy import deepcopy
from fractions import Fraction

from deepmerge import Merger
from dotmap import DotMap

from src.constants.common import FIELD_LABEL_NUMBER_REGEX
from src.defaults import CONFIG_DEFAULTS, TEMPLATE_DEFAULTS
from src.schemas.constants import FIELD_STRING_REGEX_GROUPS
from src.utils.file import load_json
from src.utils.validations import (
    validate_config_json,
    validate_evaluation_json,
    validate_template_json,
)

OVERRIDE_MERGER = Merger(
    # pass in a list of tuples,with the
    # strategies you are looking to apply
    # to each type.
    [
        # (list, ["prepend"]),
        (dict, ["merge"])
    ],
    # next, choose the fallback strategies,
    # applied to all other types:
    ["override"],
    # finally, choose the strategies in
    # the case where the types conflict:
    ["override"],
)


# Per-group placeholders (template "groupOptions"); a group without an entry
# keeps the plain join, so templates written before groupOptions read the same
GROUP_OPTION_DEFAULTS = {"empty": " ", "multi": "*", "issue": "-"}
GROUP_STATES = ("ok", "empty", "multi", "issue")


def group_options_for(template, name):
    """The resolved placeholders of a group, or None (plain join)."""
    options = (getattr(template, "group_options", None) or {}).get(name)
    if options is None:
        return None
    resolved = dict(GROUP_OPTION_DEFAULTS)
    resolved.update(options)
    return resolved


def column_state(value, details=None, empty_value=""):
    """
    "ok" | "empty" | "multi" | "issue" for one bubble column of a group.

    details: the column's field_details entry (flags, needs_review), when known.
    A column a person reviewed counts as read: empty or ok by its value.
    """
    text = "" if value is None else str(value)
    blank = not text.strip() or (empty_value != "" and text == empty_value)
    if details is None:
        return "empty" if blank else "ok"
    flags = details.get("flags") or []
    if blank:
        # also a column a rule blanked
        return "empty"
    if details.get("reviewed"):
        return "empty" if "empty" in flags else "ok"
    if "multi_marked" in flags:
        return "multi"
    if "empty" in flags:
        return "empty"
    if details.get("needs_review"):
        return "issue"
    return "ok"


def join_group(columns, omr_response, options, field_details=None, empty_value=""):
    """(value, states) of one group: one placeholder or bubble value per column."""
    pieces, states = [], []
    for column in columns:
        value = omr_response.get(column, "")
        details = field_details.get(column) if field_details else None
        state = column_state(value, details, empty_value)
        states.append(state)
        if options is None or state == "ok":
            pieces.append("" if value is None else str(value))
        elif state == "empty":
            if options.get("empty") is not None:
                pieces.append(str(options["empty"]))
        else:
            pieces.append(str(options.get(state) or ""))
    return "".join(pieces), states


def get_concatenated_response(
    omr_response, template, field_details=None, groups_out=None
):
    """
    Output row: groups (customLabels) joined, other fields as read.

    field_details (optional): per-column flags; a group with a groupOptions
    entry then yields exactly one placeholder or value per column. groups_out
    (optional dict) receives {group: {"value", "columns": [{"name", "state"}],
    "flagged"}} for groups that have a groupOptions entry.
    """
    concatenated_response = {}
    empty_value = getattr(template, "global_empty_val", "") or ""
    for field_label, concatenate_keys in template.custom_labels.items():
        options = group_options_for(template, field_label)
        if options is None:
            custom_label = "".join([omr_response[k] for k in concatenate_keys])
        else:
            custom_label, states = join_group(
                concatenate_keys, omr_response, options, field_details, empty_value
            )
            if groups_out is not None:
                groups_out[field_label] = {
                    "value": custom_label,
                    "columns": [
                        {"name": k, "state": st}
                        for k, st in zip(concatenate_keys, states)
                    ],
                    "flagged": any(st in ("multi", "issue") for st in states),
                }
        concatenated_response[field_label] = custom_label

    for field_label in template.non_custom_labels:
        concatenated_response[field_label] = omr_response[field_label]

    return concatenated_response


def describe_groups(omr_response, template, field_details=None):
    """{group: {"value", "columns", "flagged"}} for groups with groupOptions."""
    groups = {}
    if getattr(template, "group_options", None):
        get_concatenated_response(omr_response, template, field_details, groups)
    return groups


def group_review_items(groups, review):
    """Review items for flagged groups none of whose columns is already listed."""
    listed = {item.get("name") for item in review or []}
    items = []
    for name, group in (groups or {}).items():
        if not group.get("flagged") or name in listed:
            continue
        columns = [c["name"] for c in group["columns"]]
        if any(c in listed for c in columns):
            continue
        flags = sorted(
            {
                "multi_marked" if c["state"] == "multi" else "group_issue"
                for c in group["columns"]
                if c["state"] in ("multi", "issue")
            }
        )
        items.append(
            {"kind": "custom_label", "name": name, "flags": flags, "fields": columns}
        )
    return items


def open_config_with_defaults(config_path):
    user_tuning_config = load_json(config_path)
    user_tuning_config = OVERRIDE_MERGER.merge(
        deepcopy(CONFIG_DEFAULTS), user_tuning_config
    )
    validate_config_json(user_tuning_config, config_path)
    # https://github.com/drgrib/dotmap/issues/74
    return DotMap(user_tuning_config, _dynamic=False)


def open_template_with_defaults(template_path, overrides=None):
    user_template = load_json(template_path)
    # Top-level keys replace the file's (e.g. a regrade with another colorDropout);
    # None removes a key
    for key, value in (overrides or {}).items():
        if value is None:
            user_template.pop(key, None)
        else:
            user_template[key] = deepcopy(value)
    user_template = OVERRIDE_MERGER.merge(deepcopy(TEMPLATE_DEFAULTS), user_template)
    validate_template_json(user_template, template_path)
    return user_template


def open_evaluation_with_validation(evaluation_path):
    user_evaluation_config = load_json(evaluation_path)
    validate_evaluation_json(user_evaluation_config, evaluation_path)
    return user_evaluation_config


def parse_fields(key, fields):
    parsed_fields = []
    fields_set = set()
    for field_string in fields:
        fields_array = parse_field_string(field_string)
        current_set = set(fields_array)
        if not fields_set.isdisjoint(current_set):
            raise Exception(
                f"Given field string '{field_string}' has overlapping field(s) with other fields in '{key}': {fields}"
            )
        fields_set.update(current_set)
        parsed_fields.extend(fields_array)
    return parsed_fields


def parse_field_string(field_string):
    if "." in field_string:
        field_prefix, start, end = re.findall(FIELD_STRING_REGEX_GROUPS, field_string)[
            0
        ]
        start, end = int(start), int(end)
        if start >= end:
            raise Exception(
                f"Invalid range in fields string: '{field_string}', start: {start} is not less than end: {end}"
            )
        return [
            f"{field_prefix}{field_number}" for field_number in range(start, end + 1)
        ]
    else:
        return [field_string]


def custom_sort_output_columns(field_label):
    label_prefix, label_suffix = re.findall(FIELD_LABEL_NUMBER_REGEX, field_label)[0]
    return [label_prefix, int(label_suffix) if len(label_suffix) > 0 else 0]


def parse_float_or_fraction(result):
    if type(result) == str and "/" in result:
        result = float(Fraction(result))
    else:
        result = float(result)
    return result
