"""
IWS Web Handler for Auto Fan Plugin

This module handles HTTP requests through Indigo's Web Server (IWS) for the
Auto Fan configuration interface. It replaces the Flask-based web server
with a Jinja2-standalone implementation that integrates with IWS.
"""

import json
import logging
import os
import mimetypes
from typing import Dict, Any, Tuple, Optional
from urllib.parse import parse_qs, unquote_plus
from werkzeug.datastructures import MultiDict

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config_editor import WebConfigEditor
from .iws_form_helpers import generate_form_class_from_schema

# Try to import indigo for production use
try:
    import indigo
    HAS_INDIGO = True
except ImportError:
    HAS_INDIGO = False

logger = logging.getLogger("Plugin")


def create_reply_dict() -> Dict[str, Any]:
    """
    Create a reply dict for IWS responses.
    Uses indigo.Dict() in production, regular dict in tests.

    Returns:
        indigo.Dict() if indigo is available, otherwise {}
    """
    if HAS_INDIGO:
        return indigo.Dict()
    else:
        return {}


def create_headers_dict(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Create a headers dict for IWS responses.
    Uses indigo.Dict() in production, regular dict in tests.

    Args:
        headers: Dictionary of header key-value pairs

    Returns:
        indigo.Dict(headers) if indigo is available, otherwise headers
    """
    if HAS_INDIGO:
        return indigo.Dict(headers)
    else:
        return headers


def dict_to_multidict(d: Dict[str, Any]) -> MultiDict:
    """
    Convert a regular dict to MultiDict for WTForms compatibility.

    IWS provides body_params as a dict where multi-value fields (like checkboxes
    or multi-selects) have list values. This function converts it to MultiDict
    format that WTForms expects.

    Handles edge cases:
    - indigo.List objects (IWS returns these for multi-select fields)
    - Nested lists are flattened (IWS sometimes wraps multi-values in extra list)
    - All values are converted to strings (WTForms expects string form data)

    Args:
        d: Dictionary from IWS body_params

    Returns:
        MultiDict with expanded list values as strings
    """
    items = []
    for key, value in d.items():
        # Check if value is list-like (handles both list and indigo.List)
        # Use duck typing: check for __iter__ but exclude strings
        is_list_like = (
            hasattr(value, '__iter__')
            and not isinstance(value, (str, bytes))
        )

        if is_list_like:
            # Convert to Python list to ensure we can iterate properly
            try:
                value_list = list(value)
            except (TypeError, ValueError):
                # If conversion fails, treat as single value
                items.append((key, str(value) if value is not None else ''))
                continue

            # Flatten and convert list values
            for v in value_list:
                # Check for nested list-like objects
                v_is_list_like = (
                    hasattr(v, '__iter__')
                    and not isinstance(v, (str, bytes))
                )
                if v_is_list_like:
                    # Nested list - flatten it
                    try:
                        for inner_v in list(v):
                            items.append((key, str(inner_v)))
                    except (TypeError, ValueError):
                        items.append((key, str(v)))
                else:
                    items.append((key, str(v)))
        else:
            items.append((key, str(value) if value is not None else ''))
    return MultiDict(items)


def create_html_response(html: str, status: int = 200) -> Dict[str, Any]:
    """
    Create an HTML response using indigo.Dict() format.

    Args:
        html: HTML content to return
        status: HTTP status code (default 200)

    Returns:
        Response dict (indigo.Dict() in production, regular dict in tests)
    """
    reply = create_reply_dict()
    reply["status"] = status
    reply["headers"] = create_headers_dict({"Content-Type": "text/html; charset=utf-8"})
    reply["content"] = html
    return reply


def _filter_devices_by_class(devices, allowed_classes_str):
    """
    Filter devices by Indigo device class or deviceTypeId.

    Checks both 'class' (SDK device type, e.g. indigo.SensorDevice) and
    'deviceTypeId' (plugin-specific type, e.g. ha_fan) so that schema
    filters work regardless of which classification the device exposes.
    """
    if not allowed_classes_str:
        return devices
    allowed = {cls.strip() for cls in allowed_classes_str.split(",")}
    return [
        d for d in devices
        if d.get("class", "") in allowed
        or d.get("deviceTypeId", "") in allowed
    ]


class IWSWebHandler:
    """
    Handles web requests through Indigo's Web Server (IWS) using Jinja2 for templating.
    """

    def __init__(self, config_editor: WebConfigEditor, plugin_id: str):
        """
        Initialize the IWS web handler.

        Args:
            config_editor: The WebConfigEditor instance for config management
            plugin_id: The plugin ID for generating IWS URLs
        """
        self.config_editor = config_editor
        self.plugin_id = plugin_id

        # Set up Jinja2 environment
        template_dir = os.path.join(os.path.dirname(__file__), 'templates')
        self.jinja_env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(['html', 'xml']),
        )

        # Register custom functions for templates
        self.jinja_env.globals['url_for'] = self._url_for
        self.jinja_env.globals['enumerate'] = enumerate
        self.jinja_env.globals['os'] = os
        self.jinja_env.globals['get_cached_indigo_variables'] = (
            self.config_editor.get_cached_indigo_variables
        )

        # Create a simple plugin object for templates (matches SDK pattern)
        # Templates can use {{ plugin.pluginId }} to reference the plugin ID
        class PluginRef:
            def __init__(self, plugin_id):
                self.pluginId = plugin_id

        self.jinja_env.globals['plugin'] = PluginRef(plugin_id)

        logger.debug("IWSWebHandler initialized")

    def _normalize_array_fields(self, data: dict, array_fields: list) -> dict:
        """
        Ensure array fields are never None (convert to empty list).

        WTForms can return None for empty array fields, which causes crashes
        when the config is reloaded. This function ensures all array fields
        are valid lists.

        Args:
            data: Dictionary to normalize
            array_fields: List of field names that should be arrays

        Returns:
            The same dictionary (modified in place) with normalized arrays
        """
        for field in array_fields:
            if data.get(field) is None:
                logger.debug(f"Normalizing field '{field}' from None to []")
                data[field] = []
        return data

    def _url_for(self, endpoint: str, **kwargs) -> str:
        """
        Generate IWS-compatible URLs to replace Flask's url_for().

        Args:
            endpoint: The route endpoint (e.g., 'zones', 'plugin_config')
            **kwargs: Additional URL parameters

        Returns:
            IWS URL string
        """
        # Static files - automatically served from Resources/static/ by IWS
        if endpoint == 'static':
            filename = kwargs.get('filename', '')
            return f"/{self.plugin_id}/static/{filename}"

        # Regular pages
        page_map = {
            'index': '',
            'zones': 'zones',
            'zone_config': f"zone/{kwargs.get('zone_id', '')}",
            'plugin_config': 'plugin_config',
            'config_backup': 'config_backup',
            'zone_delete': f"zone/delete/{kwargs.get('zone_id', '')}",
            'create_new_variable': 'create_new_variable',
            'refresh_variables': 'refresh_variables',
        }

        page = page_map.get(endpoint, endpoint)
        base_url = f"/message/{self.plugin_id}/web_ui/"

        if page:
            return f"{base_url}?page={page}"
        return base_url

    def handle_request(
        self,
        method: str,
        headers: Dict[str, str],
        body_params: Dict[str, Any],
        params: Dict[str, str] = None,
        request_body: str = ""
    ) -> Dict[str, Any]:
        """
        Handle an HTTP request from IWS.

        Args:
            method: HTTP method (GET, POST, etc.)
            headers: Request headers dict
            body_params: Pre-parsed POST body parameters from IWS (body_params)
            params: Pre-parsed URL query parameters from IWS (url_query_args)
            request_body: Raw request body (for JSON POST requests)

        Returns:
            Response dict with status, headers, and content
        """
        logger.debug(f"IWS Web Handler: {method} request received")

        # Use pre-parsed params from IWS (no manual parsing needed)
        if params is None:
            params = {}

        logger.debug(f"URL query params: {params}")

        try:
            # Extract page parameter (IWS provides params as dict, not list)
            page = params.get('page', '')  # Get value directly (already parsed by IWS)
            logger.debug(f"Extracted page parameter: '{page}' (type: {type(page).__name__})")
            logger.debug(f"Page is empty: {not page}, Page == 'index': {page == 'index'}")
            logger.debug(f"Will render page: {page if page else 'index (default)'}")

            # Route to appropriate handler
            if method == "GET":
                return self._handle_get(page, params)
            elif method == "POST":
                return self._handle_post(page, body_params, params, request_body)
            else:
                return self._error_response(405, "Method Not Allowed")

        except Exception as e:
            logger.exception(f"Error handling IWS request: {e}")
            return self._safe_error_response(500, f"Internal Server Error: {str(e)}")

    def _handle_get(self, page: str, params: Dict[str, str]) -> Dict[str, Any]:
        """Handle GET requests with pre-parsed params from IWS."""
        logger.debug(f"_handle_get called with page='{page}'")

        # Flash messages are no longer passed via URL (POST/re-render pattern, not POST/redirect/GET)
        flash = {}

        # Route to appropriate page handler (pass flash messages)
        if not page or page == 'index':
            logger.debug("Routing to: _render_index (page is empty or 'index')")
            return self._render_index(flash)
        elif page == 'zones':
            logger.debug("Routing to: _render_zones")
            return self._render_zones(flash)
        elif page.startswith('zone/'):
            zone_id = page.split('/')[-1]
            logger.debug(f"Routing to: _render_zone_edit with zone_id='{zone_id}'")
            return self._render_zone_edit(zone_id, flash)
        elif page == 'plugin_config':
            logger.debug("Routing to: _render_plugin_config")
            return self._render_plugin_config(flash)
        elif page == 'config_backup':
            logger.debug("Routing to: _render_config_backup")
            return self._render_config_backup(flash)
        else:
            logger.warning(f"No route matched for page='{page}', returning 404")
            return self._error_response(404, f"Page not found: {page}")

    def _handle_post(self, page: str, body_params: Dict[str, Any], params: Dict[str, str], request_body: str = "") -> Dict[str, Any]:
        """Handle POST requests with pre-parsed body_params from IWS."""
        logger.debug(f"POST to page: {page}")
        logger.debug(f"Body params keys: {list(body_params.keys())}")

        # Route to specific POST handler
        if not page or page == 'zones':
            return self._post_zones(body_params)
        elif page.startswith('zone/delete/'):
            zone_id = page.split('/')[-1]
            return self._post_zone_delete(zone_id)
        elif page.startswith('zone/'):
            zone_id = page.split('/')[-1]
            return self._post_zone_save(zone_id, body_params)
        elif page == 'plugin_config':
            return self._post_plugin_config(body_params)
        elif page == 'config_backup':
            return self._post_config_backup(body_params)
        elif page == 'create_new_variable':
            return self._post_create_variable(body_params)
        elif page == 'refresh_variables':
            return self._post_refresh_variables()
        else:
            return self._error_response(404, f"Unknown POST endpoint: {page}")

    def _post_zone_save(self, zone_id: str, body_params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle zone save POST."""
        flash = {}

        try:
            # Convert IWS body_params dict to MultiDict for WTForms
            form_data = dict_to_multidict(body_params)

            # Load config
            config_data = self.config_editor.load_config()
            zones_data = config_data.get("zones", [])
            zone_schema = self.config_editor.config_schema["properties"]["zones"]["items"]

            # Generate form and populate with POST data
            ZonesFormClass = generate_form_class_from_schema(zone_schema)
            zone_form = ZonesFormClass(formdata=form_data)

            # Extract data
            zone_data = {
                field_name: field.data
                for field_name, field in zone_form._fields.items()
                if field_name != "submit"
            }

            # Coerce array fields from string representations to actual lists
            for arr_field in ["temp_sensor_dev_ids", "presence_dev_ids", "humidity_dev_ids"]:
                val = zone_data.get(arr_field)
                if val is None:
                    zone_data[arr_field] = []
                elif isinstance(val, str):
                    try:
                        parsed = json.loads(val)
                        zone_data[arr_field] = parsed if isinstance(parsed, list) else []
                    except (json.JSONDecodeError, TypeError):
                        zone_data[arr_field] = []

            # Coerce integer fields from strings
            for int_field in ["fan_dev_id", "thermostat_dev_id",
                              "zone_index", "indigo_dev_id",
                              "ideal_temp_var_id", "lock_duration", "lock_extension_duration"]:
                val = zone_data.get(int_field)
                if val is not None and val != "":
                    try:
                        zone_data[int_field] = int(val)
                    except (ValueError, TypeError):
                        zone_data[int_field] = None
                elif val == "":
                    zone_data[int_field] = None

            # Coerce float fields
            for float_field in ["ideal_temp_value"]:
                val = zone_data.get(float_field)
                if val is not None and val != "":
                    try:
                        zone_data[float_field] = float(val)
                    except (ValueError, TypeError):
                        zone_data[float_field] = None
                elif val == "":
                    zone_data[float_field] = None

            # Convert -1 sentinel to None for single-select device fields
            for dev_field in ["fan_dev_id", "thermostat_dev_id"]:
                if zone_data.get(dev_field) == -1:
                    zone_data[dev_field] = None

            # Note: modifier integer fields (speed_boost_pct, clamp_min_pct, etc.)
            # are coerced by WTForms SelectField(coerce=int) during form processing,
            # so no manual coercion is needed here.

            # Coerce boolean fields
            for bool_field in ["enabled"]:
                val = zone_data.get(bool_field)
                if isinstance(val, str):
                    zone_data[bool_field] = val.lower() in ("true", "y", "1", "on", "yes")

            # Process seasonal curves from hidden JSON field
            seasonal_curves_json = body_params.get("seasonal_curves_json", "")
            if seasonal_curves_json:
                try:
                    zone_data["seasonal_curves"] = json.loads(seasonal_curves_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            zone_data.pop("fan_curve", None)

            # Validate required fields
            if not zone_data.get("temp_sensor_dev_ids"):
                flash["error"] = "At least one temperature sensor is required"
                return self._render_zone_edit(zone_id, flash)

            # Save based on new or existing
            if zone_id == "new":
                zones_data.append(zone_data)
                config_data["zones"] = zones_data
                self.config_editor.save_config(config_data)

                # Re-render zones list with success message
                flash["message"] = "New zone created successfully"
                return self._render_zones(flash)
            else:
                index = int(zone_id)
                zones_data[index] = zone_data
                config_data["zones"] = zones_data
                self.config_editor.save_config(config_data)

                # Re-render zone edit page with success message
                flash["message"] = "Zone updated successfully"
                return self._render_zone_edit(zone_id, flash)

        except Exception as e:
            logger.exception(f"Error saving zone: {e}")
            flash["error"] = f"Error saving zone: {str(e)}"
            # Try to render the appropriate page based on zone_id
            if zone_id == "new":
                return self._render_zones(flash)
            else:
                return self._render_zone_edit(zone_id, flash)

    def _post_zone_delete(self, zone_id: str) -> Dict[str, Any]:
        """Handle zone delete."""
        flash = {}

        try:
            config_data = self.config_editor.load_config()
            zones_data = config_data.get("zones", [])

            index = int(zone_id)
            if 0 <= index < len(zones_data):
                deleted_zone = zones_data.pop(index)
                config_data["zones"] = zones_data
                self.config_editor.save_config(config_data)
                flash["message"] = f"Zone '{deleted_zone.get('name', index)}' deleted successfully"
            else:
                flash["error"] = "Invalid zone index"

        except Exception as e:
            logger.exception(f"Error deleting zone: {e}")
            flash["error"] = f"Error deleting zone: {str(e)}"

        # Re-render zones list page
        return self._render_zones(flash)

    def _post_zones(self, body_params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle zones list update (if needed)."""
        return self._render_zones({})

    def _post_plugin_config(self, body_params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle plugin config save."""
        flash = {}

        try:
            logger.debug("_post_plugin_config: Starting plugin config save")
            logger.debug(f"_post_plugin_config: Body params keys: {list(body_params.keys())}")

            form_data = dict_to_multidict(body_params)
            logger.debug(f"_post_plugin_config: Parsed form data keys: {list(form_data.keys())}")

            config_data = self.config_editor.load_config()
            plugin_schema = self.config_editor.config_schema["properties"]["plugin_config"]

            PluginFormClass = generate_form_class_from_schema(plugin_schema)
            plugin_form = PluginFormClass(formdata=form_data)

            plugin_config = {
                field_name: field.data
                for field_name, field in plugin_form._fields.items()
                if field_name != "submit"
            }

            # Coerce integer fields
            for int_field in ["default_lock_duration", "default_lock_extension_duration", "weather_dev_id"]:
                val = plugin_config.get(int_field)
                if val is not None and val != "" and val != "-1":
                    try:
                        plugin_config[int_field] = int(val)
                    except (ValueError, TypeError):
                        plugin_config[int_field] = None
                elif val in ("", "-1"):
                    plugin_config[int_field] = None

            logger.debug(f"_post_plugin_config: Extracted plugin_config: {plugin_config}")

            config_data["plugin_config"] = plugin_config
            logger.debug("_post_plugin_config: Calling save_config")
            self.config_editor.save_config(config_data)
            logger.debug("_post_plugin_config: save_config completed")

            # Re-render plugin config page with success message
            flash["message"] = "Plugin configuration saved successfully"
            logger.debug("_post_plugin_config: Re-rendering plugin config page with success message")
            return self._render_plugin_config(flash)

        except Exception as e:
            logger.exception(f"Error saving plugin config: {e}")
            flash["error"] = f"Error saving: {str(e)}"
            return self._render_plugin_config(flash)

    def _post_config_backup(self, body_params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle config backup operations."""
        flash = {}
        form_data = body_params
        action = form_data.get("action")

        try:
            if action == "create_manual_backup":
                self.config_editor.create_manual_backup()
                flash["message"] = "Manual backup created successfully"
            elif action == "restore":
                backup_type = form_data.get("backup_type")
                backup_file = form_data.get("backup_file")
                if self.config_editor.restore_backup(backup_type, backup_file):
                    flash["message"] = "Configuration restored successfully"
                else:
                    flash["error"] = "Backup file not found"
            elif action == "delete":
                backup_type = form_data.get("backup_type")
                backup_file = form_data.get("backup_file")
                if self.config_editor.delete_backup(backup_type, backup_file):
                    flash["message"] = "Backup deleted successfully"
                else:
                    flash["error"] = "Could not delete backup"
            elif action == "download":
                # Download action returns file directly, no re-render
                backup_type = form_data.get("backup_type")
                backup_file = form_data.get("backup_file")
                return self._download_backup_file(backup_type, backup_file)
            elif action == "download_config":
                # Download current config returns file directly, no re-render
                return self._get_download_config()
            elif action == "reset_defaults":
                # Reset defaults has its own re-render logic
                return self._post_reset_defaults()
            elif action == "upload_config":
                # Upload config has its own logic
                return self._post_upload_config(body_params)
            else:
                flash["error"] = "Unknown action"

        except Exception as e:
            logger.exception(f"Error in config backup operation: {e}")
            flash["error"] = f"Error: {str(e)}"

        # Re-render config backup page (except for download/reset/upload which return early)
        return self._render_config_backup(flash)

    def _post_create_variable(self, body_params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle create new variable API endpoint."""
        try:
            from .tools.indigo_api_tools import indigo_create_new_variable
            # IWS pre-parses JSON POST bodies into body_params
            var_name = body_params.get("var_name")
            var_id = indigo_create_new_variable(var_name)
            reply = create_reply_dict()
            reply["status"] = 200
            reply["headers"] = create_headers_dict({"Content-Type": "application/json"})
            reply["content"] = json.dumps({"var_id": var_id})
            return reply
        except Exception as e:
            logger.exception(f"Error creating variable: {e}")
            reply = create_reply_dict()
            reply["status"] = 500
            reply["headers"] = create_headers_dict({"Content-Type": "application/json"})
            reply["content"] = json.dumps({"error": str(e)})
            return reply

    def _post_refresh_variables(self) -> Dict[str, Any]:
        """Handle refresh variables API endpoint."""
        try:
            variables = self.config_editor.get_cached_indigo_variables()
            reply = create_reply_dict()
            reply["status"] = 200
            reply["headers"] = create_headers_dict({"Content-Type": "application/json"})
            reply["content"] = json.dumps(variables)
            return reply
        except Exception as e:
            logger.exception(f"Error refreshing variables: {e}")
            reply = create_reply_dict()
            reply["status"] = 500
            reply["headers"] = create_headers_dict({"Content-Type": "application/json"})
            reply["content"] = json.dumps({"error": str(e)})
            return reply

    def _render_index(self, flash: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Any]:
        """Render the index/home page."""
        try:
            template = self.jinja_env.get_template('index.html')
            html = template.render(flash=flash or {})
            logger.debug(f"_render_index: rendered {len(html)} bytes")
            return create_html_response(html)
        except Exception as e:
            logger.exception(f"Error rendering index page: {e}")
            return self._safe_error_response(500, f"Error rendering index: {str(e)}")

    def _render_zones(self, flash: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Any]:
        """Render the zones list page."""
        logger.debug(f"_render_zones called with flash={flash}")
        try:
            # Load config and schema
            logger.debug("Loading config data...")
            config_data = self.config_editor.load_config()
            zones_data = config_data.get("zones", [])
            logger.debug(f"Loaded {len(zones_data)} zones from config")

            # Generate form class from schema
            logger.debug("Generating form class from schema...")
            zone_schema = self.config_editor.config_schema["properties"]["zones"]["items"]
            ZonesFormClass = generate_form_class_from_schema(zone_schema)

            # Create form for each zone with data validation
            logger.debug(f"Creating {len(zones_data)} zone forms...")
            zones_forms = []
            for idx, zone in enumerate(zones_data):
                logger.debug(f"Processing zone {idx}: {zone.get('name', 'unnamed')}")

                # Validate and fix array fields (prevent "bool is not iterable" errors)
                array_fields = ['temp_sensor_dev_ids', 'presence_dev_ids']
                for field in array_fields:
                    if field in zone:
                        value = zone[field]
                        if not isinstance(value, list):
                            logger.warning(f"Zone {idx} {field} is {type(value).__name__} ({value}), coercing to empty list")
                            zone[field] = []

                try:
                    zone_form = ZonesFormClass(data=zone)
                    zones_forms.append(zone_form)
                    logger.debug(f"Successfully created form for zone {idx}")
                except Exception as e:
                    logger.exception(f"Error creating form for zone {idx} ({zone.get('name', 'unnamed')}): {e}")
                    raise

            # Render template
            logger.debug("Rendering zones.html template...")
            template = self.jinja_env.get_template('zones.html')
            html = template.render(zones_forms=zones_forms, flash=flash or {})
            logger.debug(f"Template rendered successfully, HTML length: {len(html)} bytes")

            logger.debug("Returning zones page response with status 200")
            return create_html_response(html)
        except Exception as e:
            logger.exception(f"Error rendering zones page: {e}")
            return self._error_response(500, f"Error rendering zones: {str(e)}")

    @staticmethod
    def _extract_schema_defaults(schema: dict) -> dict:
        """Recursively extract default values from a JSON schema.

        For 'object' type schemas, walks into nested properties.
        For leaf fields, returns the 'default' value if present.
        """
        defaults = {}
        for prop, subschema in schema.get("properties", {}).items():
            if subschema.get("type") == "object":
                nested = IWSWebHandler._extract_schema_defaults(subschema)
                if nested:
                    defaults[prop] = nested
            elif "default" in subschema:
                defaults[prop] = subschema["default"]
        return defaults

    def _render_zone_edit(self, zone_id: str, flash: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Any]:
        """Render the zone edit page."""
        try:
            # Load config and data
            config_data = self.config_editor.load_config()
            zones_data = config_data.get("zones", [])
            zone_schema = self.config_editor.config_schema["properties"]["zones"]["items"]

            # Determine if creating new or editing existing
            if zone_id == "new":
                # Create new zone with defaults (recursive to pick up nested modifier defaults)
                zone = self._extract_schema_defaults(zone_schema)
                is_new = True
            else:
                # Load existing zone
                try:
                    index = int(zone_id)
                    if index < 0 or index >= len(zones_data):
                        return self._error_response(404, f"Zone index {index} not found")
                    zone = zones_data[index]
                    is_new = False
                except ValueError:
                    return self._error_response(400, f"Invalid zone ID: {zone_id}")

            # Generate form class and create instance
            ZonesFormClass = generate_form_class_from_schema(zone_schema)
            zone_form = ZonesFormClass(data=zone)

            # Update choices for device dropdowns with cached data (schema-driven)
            try:
                devices = self.config_editor.get_cached_indigo_devices()
                logger.debug(f"[Zone Edit] Got {len(devices)} devices from cache")

                zone_props = zone_schema.get("properties", {})
                for field_name, field_schema in zone_props.items():
                    if not field_schema.get("x-drop-down"):
                        continue
                    if not hasattr(zone_form, field_name):
                        continue
                    field_obj = getattr(zone_form, field_name)
                    if not hasattr(field_obj, 'choices'):
                        continue

                    # Filter devices by class if specified
                    allowed_classes = field_schema.get("x-include-device-classes", "")
                    filtered = _filter_devices_by_class(devices, allowed_classes)
                    choices = [(d["id"], d["name"]) for d in filtered]

                    # Single-select device fields get a "None Selected" sentinel (-1).
                    # _post_zone_save() converts -1 back to None on submission.
                    if field_name.endswith("_dev_id"):
                        choices = [(-1, "None Selected")] + choices

                    field_obj.choices = choices
                    logger.debug(f"[Zone Edit] Updated {field_name} with {len(choices)} choices")

            except Exception as e:
                logger.exception(f"[Zone Edit] Could not update device choices: {e}")

            # Update variable dropdowns
            try:
                variables = self.config_editor.get_cached_indigo_variables()
                var_choices = [(-1, "None Selected")] + [(v["id"], v["name"]) for v in variables]

                # Helper function to update _var_id fields recursively
                def update_var_id_fields(form_obj):
                    for field_name, field in form_obj._fields.items():
                        if field_name.endswith("_var_id") and hasattr(field, 'choices'):
                            field.choices = var_choices
                        # Recursively handle nested FormFields
                        elif hasattr(field, 'form'):
                            update_var_id_fields(field.form)

                # Update all _var_id fields (including nested ones)
                update_var_id_fields(zone_form)

            except Exception as e:
                logger.warning(f"Could not update variable choices: {e}")

            # Configure global_behavior_variables_map field
            try:
                from .iws_form_helpers import GlobalBehaviorMapWidget

                plugin_config = config_data.get("plugin_config", {})
                global_vars = plugin_config.get("global_behavior_variables", [])
                # Get variable IDs from global_behavior_variables
                wanted_var_ids = {g.get("var_id") for g in global_vars if g.get("var_id")}

                # Filter cached variables to only those in global_behavior_variables
                variables = self.config_editor.get_cached_indigo_variables()
                filtered_vars = [v for v in variables if v["id"] in wanted_var_ids]

                # Configure the field with filtered variables
                if hasattr(zone_form, 'global_behavior_variables_map'):
                    zone_form.global_behavior_variables_map.variables = filtered_vars
                    # Re-create widget with updated variables list
                    zone_form.global_behavior_variables_map.widget = GlobalBehaviorMapWidget(filtered_vars)

            except Exception as e:
                logger.warning(f"Could not configure global_behavior_variables_map: {e}")

            # Render template
            template = self.jinja_env.get_template('zone_edit.html')
            html = template.render(zone_form=zone_form, index=zone_id, zone=zone, flash=flash or {})

            return create_html_response(html)

        except Exception as e:
            logger.exception(f"Error rendering zone edit page: {e}")
            return self._error_response(500, f"Error rendering zone edit: {str(e)}")

    def _render_plugin_config(self, flash: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Any]:
        """Render the plugin configuration page."""
        try:
            config_data = self.config_editor.load_config()
            plugin_config = config_data.get("plugin_config", {})

            # Generate form from schema
            plugin_schema = self.config_editor.config_schema["properties"]["plugin_config"]
            PluginFormClass = generate_form_class_from_schema(plugin_schema)
            plugin_form = PluginFormClass(data=plugin_config)

            # Update device dropdown for weather_dev_id
            try:
                devices = self.config_editor.get_cached_indigo_devices()
                device_choices = [(-1, "None Selected")] + [
                    (d["id"], d.get("name", f"Device {d['id']}")) for d in devices
                ]
                if hasattr(plugin_form, 'weather_dev_id') and hasattr(plugin_form.weather_dev_id, 'choices'):
                    plugin_form.weather_dev_id.choices = device_choices
            except Exception as e:
                logger.warning(f"Could not update device choices: {e}")

            template = self.jinja_env.get_template('plugin_edit.html')
            html = template.render(plugin_form=plugin_form, flash=flash or {})

            return create_html_response(html)
        except Exception as e:
            logger.exception(f"Error rendering plugin config page: {e}")
            return self._error_response(500, f"Error rendering plugin config: {str(e)}")

    def _render_config_backup(self, flash: Optional[Dict[str, Optional[str]]] = None) -> Dict[str, Any]:
        """Render the config backup page."""
        try:
            # Get backup lists from config editor
            manual_backups_raw = self.config_editor.list_manual_backups()
            auto_backups_raw = self.config_editor.list_auto_backups()

            # Convert to dictionaries for template compatibility
            manual_backups = [{"filename": filename} for filename in manual_backups_raw]

            # Extract filenames and create dict structure for auto backups
            auto_backups = [
                {
                    "filename": os.path.basename(path),
                    "description": "Automatic backup"
                }
                for path in auto_backups_raw
            ]

            template = self.jinja_env.get_template('config_backup.html')
            html = template.render(
                manual_backups=manual_backups,
                auto_backups=auto_backups,
                flash=flash or {}
            )

            return create_html_response(html)
        except Exception as e:
            logger.exception(f"Error rendering config backup page: {e}")
            return self._error_response(500, f"Error rendering config backup: {str(e)}")

    def _get_download_config(self) -> Dict[str, Any]:
        """Handle download current config request."""
        try:
            config_data = self.config_editor.load_config()
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"autofan_config_{timestamp}.json"

            reply = create_reply_dict()
            reply["status"] = 200
            reply["headers"] = create_headers_dict({
                "Content-Type": "application/json",
                "Content-Disposition": f'attachment; filename="{filename}"'
            })
            reply["content"] = json.dumps(config_data, indent=2)
            return reply
        except Exception as e:
            logger.exception(f"Error downloading config: {e}")
            return self._error_response(500, f"Error downloading config: {str(e)}")

    def _download_backup_file(self, backup_type: str, backup_file: str) -> Dict[str, Any]:
        """Download a specific backup file."""
        flash = {}

        try:
            if backup_type == "manual":
                backup_path = os.path.join(self.config_editor.backup_dir, backup_file)
            else:
                backup_path = os.path.join(self.config_editor.auto_backup_dir, backup_file)

            if not os.path.exists(backup_path):
                flash["error"] = "Backup file not found"
                return self._render_config_backup(flash)

            with open(backup_path, 'r') as f:
                content = f.read()

            reply = create_reply_dict()
            reply["status"] = 200
            reply["headers"] = create_headers_dict({
                "Content-Type": "application/json",
                "Content-Disposition": f'attachment; filename="{backup_file}"'
            })
            reply["content"] = content
            return reply
        except Exception as e:
            logger.exception(f"Error downloading backup: {e}")
            flash["error"] = f"Error downloading backup: {str(e)}"
            return self._render_config_backup(flash)

    def _post_reset_defaults(self) -> Dict[str, Any]:
        """Reset configuration to defaults."""
        flash = {}

        try:
            # Create backup before resetting
            if os.path.exists(self.config_editor.config_file):
                from datetime import datetime
                import shutil
                # Ensure backup directory exists
                os.makedirs(self.config_editor.auto_backup_dir, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
                backup_path = os.path.join(
                    self.config_editor.auto_backup_dir,
                    f"auto_backup_{timestamp}.json"
                )
                shutil.copy2(self.config_editor.config_file, backup_path)

            # Load default config from schema
            default_config = {}
            for field, schema in self.config_editor.config_schema.get("properties", {}).items():
                if "default" in schema:
                    default_config[field] = schema["default"]
                elif field == "zones":
                    default_config[field] = []
                elif field == "plugin_config":
                    default_config[field] = {}

            # Save default config
            self.config_editor.save_config(default_config)

            flash["message"] = "Configuration reset to defaults successfully"
        except Exception as e:
            logger.exception(f"Error resetting to defaults: {e}")
            flash["error"] = f"Error resetting config: {str(e)}"

        # Re-render config backup page
        return self._render_config_backup(flash)

    def _post_upload_config(self, body_params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle config file upload."""
        flash = {}

        try:
            # Parse multipart form data
            # Note: This is a simplified implementation. For full multipart/form-data support,
            # you may need to use a library like python-multipart

            # For now, return error indicating this feature needs implementation
            flash["error"] = "Config upload not yet supported in IWS mode. Please use download/restore from backups."
        except Exception as e:
            logger.exception(f"Error uploading config: {e}")
            flash["error"] = f"Error uploading config: {str(e)}"

        # Re-render config backup page
        return self._render_config_backup(flash)

    def _error_response(self, status: int, message: str) -> Dict[str, Any]:
        """Generate an error response using indigo.Dict() format."""
        try:
            template = self.jinja_env.get_template('config_editor_error.html')
            html = template.render(message=message)
            return create_html_response(html, status=status)
        except Exception as e:
            logger.exception(f"Error rendering error page: {e}")
            return self._safe_error_response(status, message)

    def _safe_error_response(self, status: int, message: str) -> Dict[str, Any]:
        """Generate a plain HTML error response without templates (cannot fail)."""
        html = f"<html><body><h1>{status} Error</h1><pre>{message}</pre></body></html>"
        return create_html_response(html, status=status)

    # Static files are now automatically served from Resources/static/ by IWS
    # No custom handler needed - IWS handles Resources/static/, Resources/images/, Resources/video/ automatically
