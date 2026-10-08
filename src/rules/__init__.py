"""
Post-read rules: value validation ("validate") and cross-field checks ("checks").

Both are optional template keys; a template without them builds an empty
RuleSet whose apply() returns immediately, so nothing costs time unless it is
configured. See src/rules/validation.py and src/rules/checks.py for the
template syntax and docs/engine-guide.md for examples.

The RuleSet works on plain dicts (the shapes ScanResult.to_dict() uses), so
the same code runs after a scan (src/pipeline.py, src/entry.py) and again on a
stored result after manual corrections (reapply_rules()).
"""

from src.rules.checks import CheckRule, resolve
from src.rules.validation import VALIDATION_FLAG, ValidationRule
from src.utils.parsing import group_options_for, join_group

__all__ = ["RuleSet", "reapply_rules", "VALIDATION_FLAG"]


class RuleSet:
    def __init__(self, template, validate_spec=None, checks_spec=None):
        self.global_empty = template.global_empty_val or ""
        self.custom_labels = dict(template.custom_labels)
        self.template = template
        self.zones = {zone.name: zone for zone in template.zones}
        base_names = set(template.all_parsed_labels) | set(self.custom_labels)

        specs = list(checks_spec or []) + self._fallback_zone_checks(template)
        rules = [CheckRule(spec) for spec in specs]
        self._check_names(rules, base_names)
        by_name = {rule.name: rule for rule in rules}
        outputs = {rule.output: rule for rule in rules}
        known = base_names | set(outputs)
        for rule in rules:
            # A source may name another check; read that check's output
            mapped = []
            for source in rule.sources:
                if source not in known and source in by_name:
                    source = by_name[source].output
                if source not in known:
                    raise Exception(
                        f"check '{rule.name}': unknown source '{source}' (not a field, custom label, zone or check)"
                    )
                mapped.append(source)
            renames = dict(zip(rule.sources, mapped))
            rule.sources = mapped
            rule.priority = [renames.get(s, s) for s in rule.priority]
        self.checks = _topological_order(rules, outputs)

        self.validations = {}
        for name, spec in (validate_spec or {}).items():
            target = name
            if name not in known and name in by_name:
                target = by_name[name].output
            if target not in known:
                raise Exception(
                    f"validate: unknown name '{name}' (not a field, custom label, zone or check output)"
                )
            self.validations[target] = ValidationRule(target, spec)

        self.outputs = set(outputs)
        self.new_output_columns = [
            rule.output for rule in self.checks if rule.output not in base_names
        ]

    def __bool__(self):
        return bool(self.checks or self.validations)

    @staticmethod
    def _fallback_zone_checks(template):
        """Barcode zone option "fallbackZone": a check with an OCR zone fallback."""
        zones = {zone.name: zone for zone in template.zones}
        specs = []
        for zone in template.zones:
            fallback = zone.options.get("fallbackZone")
            if not fallback:
                continue
            if fallback not in zones or fallback == zone.name:
                raise Exception(
                    f"Zone '{zone.name}': fallbackZone '{fallback}' is not another zone"
                )
            if zone.options.get("lazy") is not None and zone.options.get("lazy"):
                raise Exception(f"Zone '{zone.name}' has a fallback and can't be lazy")
            target = zones[fallback]
            if target.options.get("lazy", True):
                # Read the fallback only when the barcode gives nothing
                target.lazy = True
            specs.append(
                {
                    "name": zone.name,
                    "sources": [zone.name, fallback],
                    "priority": [zone.name, fallback],
                    "normalize": zone.options.get("fallbackNormalize", "strip"),
                    "onMissing": "fallback",
                    "onConflict": "prefer",
                    "reviewOnConflict": False,
                    "reviewOnFallback": zone.options.get("reviewOnFallback", True),
                    "output": zone.name,
                }
            )
        return specs

    @staticmethod
    def _check_names(rules, base_names):
        seen_names, seen_outputs = set(), set()
        for rule in rules:
            if rule.name in seen_names:
                raise Exception(f"Duplicate check name '{rule.name}'")
            seen_names.add(rule.name)
            if rule.output in seen_outputs:
                raise Exception(f"Two checks write the same output '{rule.output}'")
            seen_outputs.add(rule.output)
            if rule.output in base_names and not rule.shadows:
                raise Exception(
                    f"check '{rule.name}': output '{rule.output}' already exists; "
                    "an existing column can only be replaced by a check that reads it"
                )
            if rule.name in base_names and rule.name != rule.output:
                raise Exception(
                    f"check '{rule.name}': name is already a field, custom label or zone"
                )

    # ------------------------------------------------------------------
    def apply(self, omr_response, responses, fields, zones, read_lazy=None):
        """
        Run validations and checks in place.

        omr_response: per-column field values and zone values;
        responses: the concatenated output row (updated in place);
        fields / zones: the result's field_details and zone dicts (flags and
        needs_review are updated in place);
        read_lazy(name) -> zone dict: reads a lazy zone on demand.

        Returns (checks, validation, review_items).
        """
        _restore(fields)
        _restore(zones)
        if not self:
            return {}, {}, []
        return _Run(self, omr_response, responses, fields, zones, read_lazy).run()


def _topological_order(rules, outputs):
    deps = {}
    for rule in rules:
        deps[rule.name] = {
            outputs[source].name
            for source in rule.sources
            if source in outputs and outputs[source] is not rule
        }
    order, done, visiting = [], set(), []
    by_name = {rule.name: rule for rule in rules}

    def visit(name):
        if name in done:
            return
        if name in visiting:
            cycle = visiting[visiting.index(name) :] + [name]
            raise Exception(f"checks form a cycle: {' -> '.join(cycle)}")
        visiting.append(name)
        for dep in sorted(deps[name]):
            visit(dep)
        visiting.pop()
        done.add(name)
        order.append(by_name[name])

    for rule in rules:
        visit(rule.name)
    return order


def _remember(entity):
    if "pre_rules" not in entity:
        entity["pre_rules"] = {
            "flags": list(entity.get("flags") or []),
            "needs_review": bool(entity.get("needs_review")),
        }


def _restore(entities):
    """Undo a previous apply() so rules can be re-run on a stored result."""
    for entity in (entities or {}).values():
        before = entity.pop("pre_rules", None)
        if before is not None:
            entity["flags"] = before["flags"]
            entity["needs_review"] = before["needs_review"]
        entity.pop("validation_reasons", None)
        entity.pop("review_resolved_by", None)


class _Run:
    def __init__(self, rules, omr_response, responses, fields, zones, read_lazy):
        self.rules = rules
        self.omr = omr_response
        self.responses = responses
        self.fields = fields
        self.zones = zones
        self.read_lazy = read_lazy
        self.overrides = {}
        self.check_values = {}
        self.checks = {}
        self.validation = {}
        self.review = []
        self.held_for_review = set()

    # values ----------------------------------------------------------
    def empty_of(self, name):
        zone = self.rules.zones.get(name)
        return zone.empty_val if zone is not None else self.rules.global_empty

    def base_value(self, name):
        if name in self.overrides:
            return self.overrides[name]
        if name in self.check_values:
            return self.check_values[name]
        if name in self.rules.custom_labels:
            options = group_options_for(self.rules.template, name)
            if options is not None:
                return join_group(
                    self.rules.custom_labels[name],
                    self.omr,
                    options,
                    self.fields,
                    self.rules.global_empty,
                )[0]
            return "".join(
                str(self.omr.get(col, "")) for col in self.rules.custom_labels[name]
            )
        if name in self.zones:
            return self.zones[name].get("value", "")
        return self.omr.get(name, "")

    def column_empty(self, label):
        field = self.fields.get(label)
        value = str(self.omr.get(label, ""))
        if field is not None and "empty" in (field.get("flags") or []):
            return True
        return not value.strip() or value == self.rules.global_empty

    def columns(self, name):
        """(values, is_empty) per position, or (None, None)."""
        if name in self.check_values or name in self.overrides:
            return None, None
        if name in self.rules.custom_labels:
            cols = self.rules.custom_labels[name]
            return [self.omr.get(c, "") for c in cols], [
                self.column_empty(c) for c in cols
            ]
        zone = self.zones.get(name)
        if zone is not None:
            characters = (zone.get("details") or {}).get("characters")
            if characters:
                return characters, [not str(c).strip() for c in characters]
        return None, None

    def is_missing(self, name, value):
        value = "" if value is None else str(value)
        if not value.strip() or value == self.empty_of(name):
            return True
        if name in self.rules.custom_labels and name not in self.overrides:
            return all(self.column_empty(c) for c in self.rules.custom_labels[name])
        if name in self.fields and name not in self.overrides:
            return "empty" in (self.fields[name].get("flags") or [])
        return False

    def entity(self, name):
        if name in self.check_values:
            return None
        return self.fields.get(name) or self.zones.get(name)

    def set_value(self, name, value):
        self.overrides[name] = value
        if name in self.responses or name in self.rules.outputs:
            self.responses[name] = value
        if name in self.omr and name not in self.rules.custom_labels:
            self.omr[name] = value
            for label, cols in self.rules.custom_labels.items():
                if name in cols and label not in self.overrides:
                    self.responses[label] = self.base_value(label)

    # phases ----------------------------------------------------------
    def run(self):
        pending = [
            name for name in self.rules.validations if name not in self.rules.outputs
        ]
        # Columns and zones first, so blanked columns show in their custom labels
        for name in sorted(pending, key=lambda n: n in self.rules.custom_labels):
            self.validate(name)
        # Groups with placeholders show columns their validation flagged
        for label in self.rules.custom_labels:
            if label in self.overrides or label in self.rules.outputs:
                continue
            if group_options_for(self.rules.template, label) is not None:
                if label in self.responses:
                    self.responses[label] = self.base_value(label)
        for rule in self.rules.checks:
            self.run_check(rule)
        return self.checks, self.validation, self.review

    def validate(self, name, check_record=None):
        rule = self.rules.validations[name]
        value = self.base_value(name)
        columns, is_empty = self.columns(name)
        reasons = rule.failures(value, columns, is_empty)
        if check_record is not None:
            kind = "check"
        elif name in self.rules.custom_labels:
            kind = "custom_label"
        elif name in self.zones:
            kind = "zone"
        else:
            kind = "field"
        self.validation[name] = {
            "ok": not reasons,
            "kind": kind,
            "value": value,
            "reasons": reasons,
            "action": rule.on_fail if reasons else None,
        }
        if not reasons:
            return
        if rule.blanks:
            self.set_value(name, self.empty_of(name))
        if check_record is not None:
            check_record["flags"].append(VALIDATION_FLAG)
            check_record["validation_reasons"] = reasons
            if rule.blanks:
                check_record["value"] = self.empty_of(name)
            if rule.reviews:
                check_record["needs_review"] = True
            return
        entity = self.entity(name)
        if entity is not None:
            _remember(entity)
            entity["flags"] = sorted(set(entity.get("flags") or []) | {VALIDATION_FLAG})
            entity["validation_reasons"] = reasons
            if rule.reviews:
                entity["needs_review"] = True
                self.held_for_review.add(name)
        elif rule.reviews:
            item = {"kind": kind, "name": name, "flags": [VALIDATION_FLAG]}
            item["reasons"] = reasons
            if kind == "custom_label":
                item["fields"] = list(self.rules.custom_labels[name])
            self.review.append(item)

    def source_reading(self, rule, source, have_value):
        """(raw, normalized, usable, note) for one source of a check."""
        zone = self.zones.get(source)
        if (
            zone is not None
            and source not in self.check_values
            and "not_read" in (zone.get("flags") or [])
        ):
            if have_value or self.read_lazy is None:
                return None, None, False, "not_read"
            zone = self.zones[source] = self.read_lazy(source)
            if source in self.responses and source not in self.overrides:
                self.responses[source] = zone.get("value", "")
            if source in self.omr:
                self.omr[source] = zone.get("value", "")
        raw = self.base_value(source)
        raw = "" if raw is None else str(raw)
        normalized = rule.normalize(raw)
        if self.is_missing(source, raw) or not normalized:
            return raw, normalized, False, "missing"
        validation = self.rules.validations.get(source)
        if rule.skip_invalid and validation is not None:
            if source in self.validation and source not in self.check_values:
                invalid = not self.validation[source]["ok"]
            else:
                columns, is_empty = self.columns(source)
                invalid = bool(validation.failures(raw, columns, is_empty))
            if invalid:
                return raw, normalized, False, "invalid"
        entity = self.entity(source)
        if rule.skip_flagged and entity is not None and entity.get("needs_review"):
            return raw, normalized, False, "flagged"
        return raw, normalized, True, None

    def run_check(self, rule):
        readings, notes, have_value = [], {}, False
        for source in rule.priority:
            raw, normalized, usable, note = self.source_reading(
                rule, source, have_value
            )
            readings.append((source, raw, normalized, usable))
            if note:
                notes[source] = note
            have_value = have_value or usable
        value, chosen, flags, needs_review = resolve(rule, readings)
        empty = self.empty_of(rule.output)
        record = {
            "value": empty if value is None else value,
            "chosen_source": chosen,
            "sources": {src: raw for src, raw, _n, _u in readings},
            "normalized": {src: norm for src, _r, norm, _u in readings},
            "skipped": notes,
            "flags": flags,
            "needs_review": needs_review,
            "output": rule.output,
        }
        self.check_values[rule.output] = record["value"]
        self.responses[rule.output] = record["value"]
        if rule.output in self.rules.validations:
            self.validate(rule.output, record)
            self.check_values[rule.output] = record["value"]
            self.responses[rule.output] = record["value"]
        record["flags"] = sorted(set(record["flags"]))
        if rule.absorb_review and not record["needs_review"] and value is not None:
            self.absorb(rule, readings, value)
        self.checks[rule.name] = record
        if record["needs_review"]:
            self.review.append(
                {"kind": "check", "name": rule.name, "flags": record["flags"]}
            )

    def absorb(self, rule, readings, value):
        """
        A resolved check settles its sources' own review flags when the source
        was missing (another source supplied the value) or agrees with another
        present source. A source that alone decided the value keeps its flags.
        """
        usable = [(src, norm) for src, _r, norm, ok in readings if ok]
        for source, _raw, normalized, ok in readings:
            if source in self.check_values and source != rule.output:
                continue  # another check's output
            entity = self.fields.get(source) or self.zones.get(source)
            if entity is None or not entity.get("needs_review"):
                continue
            if source in self.held_for_review:
                continue  # its own validation asked for review
            if ok:
                agrees = any(
                    other != source and norm == normalized for other, norm in usable
                )
                if not agrees:
                    continue
            _remember(entity)
            entity["needs_review"] = False
            entity["review_resolved_by"] = rule.name


def reapply_rules(result, template, omr_response=None):
    """
    Re-run a template's rules on a stored ScanResult dict (e.g. after manual
    corrections). Updates responses, checks, validation, review and status in
    place. Lazy zones that were never read stay unread.
    """
    from src.utils.parsing import (
        describe_groups,
        get_concatenated_response,
        group_review_items,
    )

    rules = getattr(template, "rules", None)
    fields = result.get("fields") or {}
    zones = result.get("zones") or {}
    if omr_response is None:
        omr_response = {name: f.get("value", "") for name, f in fields.items()}
        for name, zone in zones.items():
            omr_response[name] = zone.get("value", "")
    result["responses"] = get_concatenated_response(omr_response, template, fields)
    if rules is None:
        return result
    checks, validation, extra = rules.apply(
        omr_response, result["responses"], fields, zones
    )
    result["checks"] = checks
    result["validation"] = validation
    result["review"] = review_items(fields, zones, extra)
    groups = describe_groups(omr_response, template, fields)
    result["review"].extend(group_review_items(groups, result["review"]))
    if groups:
        result["groups"] = groups
    else:
        result.pop("groups", None)
    if result.get("status") != "error":
        result["status"] = "needs_review" if result["review"] else "ok"
    return result


def review_items(fields, zones, extra):
    """The review list: flagged fields, flagged zones, then rule items."""
    return (
        [
            {"kind": "field", "name": name, "flags": details["flags"]}
            for name, details in fields.items()
            if details.get("needs_review")
        ]
        + [
            {"kind": "zone", "name": name, "flags": zone["flags"]}
            for name, zone in zones.items()
            if zone.get("needs_review")
        ]
        + list(extra)
    )
