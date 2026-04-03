import json
import logging
import os
import shutil
import socket
from datetime import datetime

from auto_fan.auto_fan_agent import AutoFanAgent
from auto_fan.auto_fan_config import AutoFanConfig

try:
    import indigo
except ImportError:
    pass


class Plugin(indigo.PluginBase):

    def __init__(
        self,
        plugin_id: str,
        plugin_display_name: str,
        plugin_version: str,
        plugin_prefs: indigo.Dict,
        **kwargs: dict,
    ) -> None:
        super().__init__(
            plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs
        )

        self._agent = None
        self._iws_web_handler = None
        self._log_non_events = bool(plugin_prefs.get("log_non_events", False))

        # Configure logging
        self.log_level = int(plugin_prefs.get("log_level", logging.INFO))
        self.logger.debug(f"{self.log_level=}")
        self.indigo_log_handler.setLevel(self.log_level)
        self.plugin_file_handler.setLevel(logging.DEBUG)

        # Config file path (derived from plugin log file location)
        self._config_file_str = self.plugin_file_handler.baseFilename.replace(
            "Logs", "Preferences"
        ).replace("/plugin.log", "/config/auto_fan_conf.json")

    def _get_web_config_urls(self) -> list:
        """Detect all available URLs for the web configuration interface."""
        urls = []
        indigo_port = 8176
        path = f"/message/{self.pluginId}/web_ui/"

        urls.append({"label": "Local", "url": f"http://localhost:{indigo_port}{path}"})

        try:
            hostname = socket.gethostname()
            if hostname and hostname != "localhost":
                urls.append({
                    "label": "Network (hostname)",
                    "url": f"http://{hostname}:{indigo_port}{path}",
                })

            try:
                addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET)
                seen_ips = set()
                for info in addr_info:
                    ip = info[4][0]
                    if not ip.startswith("127.") and ip not in seen_ips:
                        seen_ips.add(ip)
                        urls.append({
                            "label": "Network (IP)",
                            "url": f"http://{ip}:{indigo_port}{path}",
                        })
            except Exception as e:
                self.logger.debug(f"Could not detect network IPs: {e}")

        except Exception as e:
            self.logger.debug(f"Could not detect hostname: {e}")

        try:
            reflector_url = indigo.server.getReflectorURL()
            if reflector_url:
                reflector_base = reflector_url.rstrip("/")
                urls.append({
                    "label": "Remote (Reflector)",
                    "url": f"{reflector_base}{path}",
                })
        except Exception as e:
            self.logger.debug(f"Could not detect Indigo Reflector URL: {e}")

        return urls

    def startup(self) -> None:
        self.logger.debug("startup called")

        indigo.devices.subscribeToChanges()
        indigo.variables.subscribeToChanges()

        self._init_config_and_agent()

        # Log web config URLs
        self.logger.info("🌐 Web Configuration Interface:")
        urls = self._get_web_config_urls()
        for url_info in urls:
            self.logger.info(f"   {url_info['label']}: {url_info['url']}")

        self.logger.debug("Plugin startup complete")

    def shutdown(self) -> None:
        self.logger.debug("shutdown called")
        if hasattr(self, "_agent") and self._agent is not None:
            self._agent.shutdown()

    def deviceUpdated(self, orig_dev, new_dev) -> None:
        indigo.PluginBase.deviceUpdated(self, orig_dev, new_dev)

        # Ignore our own plugin devices
        if new_dev.pluginId == "com.vtmikel.autofan":
            return

        # Build diff
        orig_dict = {}
        for k, v in orig_dev:
            orig_dict[k] = v

        new_dict = {}
        for k, v in new_dev:
            new_dict[k] = v

        diff = {
            k: new_dict[k]
            for k in orig_dict
            if k in new_dict and orig_dict[k] != new_dict[k]
        }

        if self._agent is not None:
            processed = self._agent.process_device_change(orig_dev, diff)
            for z in processed:
                z.sync_indigo_device()

    def variableUpdated(self, orig_var, new_var) -> None:
        indigo.PluginBase.variableUpdated(self, orig_var, new_var)

        if self._agent is not None:
            self._agent.process_variable_change(orig_var, new_var)

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if self._agent is None:
            return
        if not user_cancelled:
            self._log_non_events = bool(values_dict.get("log_non_events", False))
            self._agent.config.log_non_events = self._log_non_events

            self.log_level = int(values_dict.get("log_level", logging.INFO))
            self.logger.debug(f"{self.log_level=}")
            self.indigo_log_handler.setLevel(self.log_level)
            self.plugin_file_handler.setLevel(logging.DEBUG)

    def get_zone_list(self, filter="", values_dict=None, type_id="", target_id=0):
        menu_items = []
        for zone in self._agent.get_zones():
            menu_items.append((zone.name, zone.name))
        return menu_items

    def _init_config_and_agent(self):
        reloading = hasattr(self, "_config_mtime")
        if reloading:
            self.logger.warning(
                "🔄 Configuration reloaded; all locks and zone state has been reset"
            )

        empty_conf = "config_web_editor/config/auto_fan_conf_empty.json"
        config_dir = os.path.dirname(self._config_file_str)
        if not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)
        if not os.path.exists(self._config_file_str):
            shutil.copyfile(empty_conf, self._config_file_str)

        conf_path = os.path.abspath(self._config_file_str)
        self._config_path = conf_path
        self._config_mtime = os.path.getmtime(conf_path)

        config = AutoFanConfig(conf_path)
        config.log_non_events = self._log_non_events
        self._agent = AutoFanAgent(config)

        if not config.enabled:
            config_dev_name = config.indigo_dev.name if config.indigo_dev else "Unknown"
            config_dev_state = config.indigo_dev.onState if config.indigo_dev else False
            self.logger.info(
                f"Auto Fan plugin is currently DISABLED "
                f"(config device '{config_dev_name}' onState={config_dev_state}). "
                f"Enable the device to activate automatic fan control."
            )

        self._agent.process_all_zones()

    def _init_iws_web_handler(self):
        """Initialize the IWS web handler for the configuration interface."""
        try:
            from config_web_editor.config_editor import WebConfigEditor
            from config_web_editor.iws_web_handler import IWSWebHandler

            current_dir = os.getcwd()
            schema_file = os.path.join(current_dir, "config_web_editor/config/config_schema.json")
            backup_dir = os.path.join(os.path.dirname(self._config_file_str), "backups")
            auto_backup_dir = os.path.join(os.path.dirname(self._config_file_str), "auto_backups")

            config_editor = WebConfigEditor(
                self._config_file_str,
                schema_file,
                backup_dir,
                auto_backup_dir,
                flask_app=None,
            )

            config_editor.reload_config_callback = self._init_config_and_agent

            self._iws_web_handler = IWSWebHandler(
                config_editor=config_editor,
                plugin_id=self.pluginId,
            )

            config_editor.start_cache_refresher()
            self.logger.info("IWS Web Configuration Interface initialized")

        except Exception as e:
            self.logger.error(f"Failed to initialize IWS web handler: {e}")
            self.logger.exception(e)
            self._iws_web_handler = None

    def reset_zone_lock(self, action, dev, caller_waiting_for_result):
        self._agent.reset_locks(action.props.get("zone_list"))

    def reset_all_locks(self, action, dev, caller_waiting_for_result):
        self._agent.reset_locks()

    def print_locked_zones(self, action=None, dev=None, caller_waiting_for_result=None):
        self._agent.print_locked_zones()

    def print_zone_breakdowns(self, action=None, dev=None, caller_waiting_for_result=None):
        self._agent.print_zone_breakdowns()

    def change_zones_enabled(self, action, dev=None, caller_waiting_for_result=None):
        if self._agent is None:
            return

        action_type = action.pluginTypeId
        if action_type == "enable_all_zones":
            self._agent.enable_all_zones()
        elif action_type == "disable_all_zones":
            self._agent.disable_all_zones()
        elif action_type == "enable_zone":
            zone_name = action.props.get("zone_list")
            self._agent.enable_zone(zone_name)
        elif action_type == "disable_zone":
            zone_name = action.props.get("zone_list")
            self._agent.disable_zone(zone_name)

    def create_variable(self, action, dev=None, caller_waiting_for_result=None):
        self.logger.debug("Handling variable creation request")
        props_dict = dict(action.props)
        reply = indigo.Dict()
        if props_dict.get("incoming_request_method") == "POST":
            post_params = json.loads(props_dict.get("request_body", "{}"))
            var_name = post_params.get("var_name", "").strip()
            if not var_name:
                context = {"error": "var_name must be provided"}
                status = 400
            else:
                try:
                    newVar = indigo.variable.create(var_name, "true")
                    context = {"var_id": newVar.id}
                    status = 200
                except Exception as e:
                    self.logger.error(f"Failed to create variable '{var_name}': {e}")
                    context = {"error": str(e)}
                    status = 500
            reply["status"] = status
            reply["headers"] = indigo.Dict({"Content-Type": "application/json"})
            reply["content"] = json.dumps(context)
        return reply

    def actionControlDevice(self, action, dev):
        """Handle global config and zone device toggles."""
        if self._agent is None:
            return

        action_type = action.deviceAction

        if action_type == indigo.kDeviceAction.RequestStatus:
            return

        dev_type = dev.deviceTypeId
        if dev_type not in ("auto_fan_config", "auto_fan_zone"):
            return

        if action_type == indigo.kDeviceAction.Toggle:
            desired_state = not dev.onOffState
        elif action_type in (indigo.kDeviceAction.TurnOn, indigo.kDeviceAction.TurnOff):
            desired_state = action_type == indigo.kDeviceAction.TurnOn
        else:
            self.logger.warning(f"Unrecognized device action {action_type} for {dev.name}")
            return

        dev.updateStateOnServer("onOffState", desired_state)

        if dev_type == "auto_fan_config":
            self._agent.process_all_zones()
        else:
            zone_index = int(dev.pluginProps.get("zone_index", -1))
            zone = next(
                (z for z in self._agent.config.zones if z.zone_index == zone_index),
                None,
            )
            if zone:
                self._agent.process_zone(zone)
            else:
                self.logger.error(
                    f"actionControlDevice: Zone with index {zone_index} not found."
                )

    def _build_schema_state_definitions(self, dev, field_schemas):
        """Turn JSON-schema entries into getDeviceStateDictForXType definitions."""
        out = []
        for key, schema in field_schemas.items():
            if not schema.get("x-sync_to_indigo"):
                continue
            title = schema.get("title", key)
            ftype = schema.get("type", "string")
            if ftype == "boolean":
                sd = self.getDeviceStateDictForBoolTrueFalseType(key, title, title)
            elif ftype in ("integer", "number"):
                sd = self.getDeviceStateDictForNumberType(key, title, title)
            else:
                sd = self.getDeviceStateDictForStringType(key, title, title)
            out.append(sd)
        return out

    def _build_zone_runtime_state_definitions(self, dev):
        """Turn a zone's runtime-state entries into state-definitions."""
        if self._agent is None:
            return []

        zone = next(
            (z for z in self._agent.config.zones if z.indigo_dev and z.indigo_dev.id == dev.id),
            None,
        )
        if not zone:
            return []

        out = []
        for entry in zone.zone_indigo_device_runtime_states:
            key = entry["key"]
            label = entry.get("label", key)
            rtype = entry.get("type", "string")
            if rtype in ("boolean", "bool"):
                sd = self.getDeviceStateDictForBoolTrueFalseType(key, label, label)
            elif rtype in ("integer", "number", "numeric"):
                sd = self.getDeviceStateDictForNumberType(key, label, label)
            else:
                sd = self.getDeviceStateDictForStringType(key, label, label)
            out.append(sd)
        return out

    def getDeviceStateList(self, dev):
        states = super().getDeviceStateList(dev)

        if (
            dev.pluginId != "com.vtmikel.autofan"
            or getattr(self, "_agent", None) is None
        ):
            return states

        if dev.deviceTypeId == "auto_fan_config":
            if self._agent is not None:
                states.extend(
                    self._build_schema_state_definitions(
                        dev, self._agent.config.config_field_schemas
                    )
                )

        elif dev.deviceTypeId == "auto_fan_zone":
            if self._agent is not None:
                states.extend(
                    self._build_schema_state_definitions(
                        dev, self._agent.config.zone_field_schemas
                    )
                )
                states.extend(self._build_zone_runtime_state_definitions(dev))

        return states

    def handle_web_ui(self, action, dev=None, callerWaitingForResult=True):
        """Handle web UI requests through Indigo IWS."""
        if not self._iws_web_handler:
            self.logger.debug("Lazy initializing IWS web handler on first request")
            self._init_iws_web_handler()

        if not self._iws_web_handler:
            self.logger.error("IWS web handler failed to initialize")
            reply = indigo.Dict()
            reply["status"] = 503
            reply["headers"] = indigo.Dict({"Content-Type": "text/html; charset=utf-8"})
            reply["content"] = "<html><body><h1>503 Service Unavailable</h1><p>Web handler failed to initialize</p></body></html>"
            return reply

        method = (action.props.get("incoming_request_method") or "GET").upper()
        headers = dict(action.props.get("headers", {}))
        body_params = dict(action.props.get("body_params", {}))
        request_body = action.props.get("request_body", "")
        url_query_args = dict(action.props.get("url_query_args", {}))

        self.logger.debug(f"IWS Web UI: {method} request")
        self.logger.debug(f"URL query args: {url_query_args}")
        if method == "POST":
            self.logger.debug(f"POST body params: {body_params}")

        return self._iws_web_handler.handle_request(
            method, headers, body_params, url_query_args, request_body
        )

    def deviceStartComm(self, dev):
        self.logger.debug(f"deviceStartComm called for device {dev.id} ('{dev.name}')")
        dev.stateListOrDisplayStateIdChanged()
        if self._agent is not None:
            self._agent.refresh_indigo_device(dev.id)
        self.logger.debug(f"deviceStartComm complete for device {dev.id} ('{dev.name}')")
