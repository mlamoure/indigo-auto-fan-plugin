"""
IWS Form Helpers - WTForms without Flask

This module provides form generation utilities for IWS mode, using WTForms
without Flask dependencies. Forms are used for validation and rendering in
templates.
"""

from collections import OrderedDict
from wtforms import (
    Form, FormField, SubmitField, StringField, IntegerField,
    DecimalField, BooleanField, SelectField, SelectMultipleField, Field
)
from wtforms.validators import DataRequired, Optional
from markupsafe import Markup


def create_field(field_name, field_schema):
    """
    Create a WTForms field based on JSON schema definition.

    Args:
        field_name: Name of the field
        field_schema: JSON schema for this field

    Returns:
        WTForms field instance
    """
    label_text = field_schema.get("title", field_name)
    tooltip_text = field_schema.get("tooltip", "")
    field_type = field_schema.get("type")
    required = field_schema.get("required", False)

    validators = []
    if required:
        validators.append(DataRequired())
    else:
        validators.append(Optional())

    # Special handling for mapping fields (complex custom widgets)
    if field_name == "global_behavior_variables_map":
        return GlobalBehaviorMapField(label=label_text, description=tooltip_text, validators=validators)

    # Special handling for global_behavior_variables (array of objects)
    if field_name == "global_behavior_variables":
        return GlobalBehaviorVariablesField(label=label_text, description=tooltip_text, validators=validators)

    # Special handling for variable ID dropdowns
    if field_name.endswith("_var_id") and field_schema.get("x-drop-down"):
        choices = []
        if not required:
            choices.insert(0, (-1, "None Selected"))
        f = SelectField(label=label_text, description=tooltip_text, choices=choices, coerce=int, validators=validators)

    # Special handling for single device ID dropdown
    elif field_name.endswith("_dev_id") and field_schema.get("x-drop-down"):
        choices = [(-1, "None Selected")]
        f = SelectField(label=label_text, description=tooltip_text, choices=choices, coerce=int, validators=validators)

    # Special handling for device ID multi-select
    elif field_name.endswith("_dev_ids") and field_schema.get("x-drop-down"):
        choices = []
        f = SelectMultipleField(label=label_text, description=tooltip_text, choices=choices, coerce=int, validators=validators)

    # Array fields with dropdown (e.g., multi-select arrays)
    elif field_type == "array" and field_schema.get("x-drop-down"):
        choices = []
        # Determine coerce type from items schema
        items_type = field_schema.get("items", {}).get("type", "string")
        coerce_func = int if items_type == "integer" else str
        f = SelectMultipleField(label=label_text, description=tooltip_text, choices=choices, coerce=coerce_func, validators=validators)

    # Integer/number enum fields (dropdowns with numeric values)
    elif field_type in ("integer", "number") and field_schema.get("enum"):
        enum_values = field_schema.get("enum", [])
        enum_labels = field_schema.get("x-enum-labels", [str(v) for v in enum_values])
        choices = list(zip(enum_values, enum_labels))
        coerce_func = int if field_type == "integer" else float
        f = SelectField(label=label_text, description=tooltip_text, choices=choices, coerce=coerce_func, validators=validators)

    # Integer fields
    elif field_type == "integer":
        f = IntegerField(label=label_text, description=tooltip_text, validators=validators)

    # Number/decimal fields
    elif field_type == "number":
        f = DecimalField(label=label_text, description=tooltip_text, validators=validators)

    # Boolean fields
    elif field_type == "boolean":
        f = BooleanField(label=label_text, description=tooltip_text)

    # Enum select fields
    elif field_type == "string" and field_schema.get("enum"):
        enum_values = field_schema.get("enum", [])
        choices = [(val, val) for val in enum_values]
        f = SelectField(label=label_text, description=tooltip_text, choices=choices, validators=validators)

    # Default string field
    else:
        f = StringField(label=label_text, description=tooltip_text, validators=validators)

    f.description = tooltip_text
    return f


def generate_form_class_from_schema(schema):
    """
    Dynamically generate a WTForms Form class from a JSON schema.

    This is the IWS equivalent of the Flask version, using Form instead of FlaskForm.

    Args:
        schema: JSON schema definition

    Returns:
        Dynamically generated Form class
    """
    attrs = OrderedDict()

    for prop, subschema in schema.get("properties", {}).items():
        # Determine if this field is required
        # Handle both list format (parent schema) and boolean format (already processed)
        required_fields = schema.get("required", [])
        if isinstance(required_fields, list):
            subschema["required"] = (prop in required_fields)
        # If required is already a boolean on subschema, leave it as is

        # Special handling for custom mapping fields (must come before object type check)
        # These fields have type: "object" but need custom field classes with widgets
        if prop in ("global_behavior_variables_map",):
            attrs[prop] = create_field(prop, subschema)
        # Nested object becomes a FormField
        elif subschema.get("type") == "object":
            subform_class = generate_form_class_from_schema(subschema)
            attrs[prop] = FormField(subform_class, label=subschema.get("title", prop))
        else:
            attrs[prop] = create_field(prop, subschema)

    # Create a base Form class (not FlaskForm - no CSRF needed)
    class DynamicForm(Form):
        pass

    return type("DynamicForm", (DynamicForm,), attrs)


def populate_form_from_dict(form, data):
    """
    Populate a WTForms form instance with data from a dictionary.

    Args:
        form: WTForms Form instance
        data: Dictionary of field values
    """
    for field_name, field in form._fields.items():
        if field_name in data:
            field.data = data[field_name]


def extract_form_data(form):
    """
    Extract data from a WTForms form into a dictionary.

    Args:
        form: WTForms Form instance

    Returns:
        Dictionary of field_name: field_value
    """
    return {
        field_name: field.data
        for field_name, field in form._fields.items()
        if field_name != "submit"
    }


# Custom widgets and fields for complex mapping objects

class GlobalBehaviorMapWidget:
    """Widget for rendering Global Behavior Variables Map as an HTML table."""

    def __init__(self, variables):
        """
        Initialize widget with list of variables.

        Args:
            variables: List of variable dicts with 'id' and 'name' keys
        """
        self.variables = variables

    def __call__(self, field, **kwargs):
        """
        Render the widget as HTML.

        Args:
            field: The field instance being rendered
            **kwargs: Additional HTML attributes

        Returns:
            Markup object with safe HTML
        """
        html = [
            '<table class="global-behavior-map">'
            '<thead><tr><th>Variable</th><th>Zone applies</th></tr></thead>'
            '<tbody>'
        ]
        for var in self.variables:
            vid = var.get("id")
            name = var.get("name")
            # Default to True if not explicitly set
            checked = 'checked' if field.data.get(str(vid), True) else ''
            html.append(
                f'<tr>'
                f'<td>{name}</td>'
                f'<td><input type="checkbox" name="global_behavior_variables_map-{vid}" {checked}></td>'
                f'</tr>'
            )
        html.append('</tbody></table>')
        return Markup(''.join(html))


class GlobalBehaviorMapField(Field):
    """
    Custom WTForms field for Global Behavior Variables Map.

    Renders as a table with checkboxes for each global behavior variable,
    allowing the user to enable/disable variables for this zone.
    """

    widget = None  # Will be set dynamically

    def __init__(self, label="", validators=None, variables=None, **kwargs):
        """
        Initialize the field.

        Args:
            label: Field label text
            validators: List of validators
            variables: List of variable dicts to display
            **kwargs: Additional field arguments
        """
        super().__init__(label, validators, **kwargs)
        self.variables = variables or []
        self.widget = GlobalBehaviorMapWidget(self.variables)

    def _value(self):
        """Return the current data or empty dict."""
        return self.data or {}

    def process(self, formdata, data=None, extra_filters=None):
        """
        Process form data to extract checkbox states.

        Args:
            formdata: MultiDict of form data
            data: Initial data (from zone config)
            extra_filters: Additional filters (unused)
        """
        if formdata:
            # Build map from which checkboxes were present in formdata
            self.data = {
                str(v["id"]): f"global_behavior_variables_map-{v['id']}" in formdata
                for v in self.variables
            }
        else:
            # Use the passed-in dict from zone config
            self.data = data or {}


class GlobalBehaviorVariablesField(Field):
    """
    Custom WTForms field for Global Behavior Variables (array of objects).

    Each entry has: var_id (int), comparison_type (str), var_value (str).
    The template renders this as a dynamic table with add/remove rows.
    """

    def __init__(self, label="", validators=None, **kwargs):
        """
        Initialize the field.

        Args:
            label: Field label text
            validators: List of validators
            **kwargs: Additional field arguments
        """
        super().__init__(label, validators, **kwargs)

    def _value(self):
        """Return the current data or empty list."""
        return self.data or []

    def process_formdata(self, valuelist):
        """
        Process form data (not used - handled in process() instead).

        Args:
            valuelist: List of values (unused)
        """
        pass

    def process(self, formdata, data=None, extra_filters=None):
        """
        Process form data to extract global behavior variables array.

        Parses fields with pattern: global_behavior_variables-{idx}-{field_name}

        Args:
            formdata: MultiDict of form data
            data: Initial data (from config)
            extra_filters: Additional filters (unused)
        """
        if formdata:
            # Build a dict of index -> entry dict
            entries_by_index = {}

            for key in formdata:
                if not key.startswith("global_behavior_variables-"):
                    continue

                # Parse: global_behavior_variables-{idx}-{field_name}
                parts = key.split("-")
                if len(parts) < 3:
                    continue

                # Handle field names with hyphens (e.g., "comparison_type")
                # Format: global_behavior_variables-{idx}-{field_name}
                # parts[0] = "global_behavior_variables"
                # parts[1] = idx
                # parts[2:] = field_name parts
                idx_str = parts[1]
                field_name = "-".join(parts[2:])  # Rejoin field name if it has hyphens

                try:
                    idx = int(idx_str)
                except ValueError:
                    continue

                if idx not in entries_by_index:
                    entries_by_index[idx] = {}

                value = formdata.get(key)

                # Convert var_id to integer
                if field_name == "var_id":
                    try:
                        entries_by_index[idx][field_name] = int(value)
                    except (ValueError, TypeError):
                        entries_by_index[idx][field_name] = None
                else:
                    entries_by_index[idx][field_name] = value

            # Convert to sorted list of entries
            self.data = [
                entries_by_index[idx]
                for idx in sorted(entries_by_index.keys())
            ]
        else:
            # Use the passed-in list from config
            self.data = data or []
