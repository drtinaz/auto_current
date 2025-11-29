#!/usr/bin/env python3

import dbus
import dbus.exceptions
import logging
import os
import sys
import configparser
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
from ve_utils import wrap_dbus_value

# Logging setup
logger = logging.getLogger()

for handler in logger.handlers[:]:
    logger.removeHandler(handler)

formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)

logger.addHandler(console_handler)
logger.setLevel(logging.INFO) # Default to DEBUG for better visibility

#logging.basicConfig(level=logging.INFO)
logging.info("Starting Generator Derating Monitor with file logging.")

# D-Bus service names and paths
VEBUS_SERVICE_BASE = "com.victronenergy.vebus"
GENERATOR_SERVICE_BASE = "com.victronenergy.generator"
TEMPERATURE_SERVICE_BASE = "com.victronenergy.temperature"
SETTINGS_SERVICE_NAME = "com.victronenergy.settings"
GPS_SERVICE_BASE = "com.victronenergy.gps"
DIGITAL_INPUT_SERVICE_BASE = "com.victronenergy.digitalinput"
SYSTEM_SERVICE = "com.victronenergy.system"

ALTITUDE_PATH = "/Altitude"
AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH = "/Ac/ActiveIn/CurrentLimit"
TEMPERATURE_PATH = "/Temperature"
CUSTOM_NAME_PATH = "/CustomName"
STATE_PATH = "/State"
PRODUCT_NAME_PATH = "/ProductName"
BUS_ITEM_INTERFACE = "com.victronenergy.BusItem"
GENERATOR_CURRENT_LIMIT_PATH = "/Settings/TransferSwitch/GeneratorCurrentLimit"

# Transfer switch state values
GENERATOR_ON_VALUE = (12, 3)
SHORE_POWER_ON_VALUE = (13, 2)

# Gen Auto Current State Values
GEN_AUTO_CURRENT_OFF = 2
GEN_AUTO_CURRENT_ON = 3

# CORRECTED: Configuration file path
CONFIG_FILE_PATH = '/data/apps/auto_current/config.ini'

class GeneratorDeratingMonitor:
    def __init__(self):
        self.bus = dbus.SystemBus()

        # Load settings from the config file
        self._load_and_set_config()

        self.vebus_service = None
        self.outdoor_temp_service_name = None
        self.generator_temp_service_name = None
        self.gps_service_name = None
        self.transfer_switch_service = None
        self.settings_service_name = SETTINGS_SERVICE_NAME
        self.gen_auto_current_service = None
        self.gen_auto_current_state = None
        self.previous_gen_auto_current_state = None
        self.initial_derated_output_logged = False
        self.initial_altitude = None
        self.initial_outdoor_temp = None
        self.initial_generator_temp = None
        self.previous_ac_current_limit = None
        self.previous_generator_current_limit_setting = None
        self.outdoor_temp_fahrenheit = self.DEFAULT_OUTDOOR_TEMP_F
        self.altitude_feet = self.DEFAULT_ALTITUDE_FEET
        self.generator_temp_fahrenheit = self.DEFAULT_GENERATOR_TEMP_F
        self.service_discovery_retries = 1
        self.service_discovery_delay = 5
        self.altitude_warning_logged = False
        self.altitude_value_logged_after_warning = False

        GLib.timeout_add_seconds(5, self._delayed_initialization)
        
    def _load_and_set_config(self):
        """Loads settings from config file, with hardcoded defaults as fallback."""
        config = configparser.ConfigParser()
        
        # Set default values first
        self.BASE_TEMPERATURE_THRESHOLD_F = 77.0
        self.TEMP_COEFFICIENT = 0.00055
        self.ALTITUDE_COEFFICIENT = 0.00003
        self.BASE_GENERATOR_OUTPUT_AMPS = 60.5
        self.OUTPUT_BUFFER = 0.9
        self.HIGH_GENTEMP_THRESHOLD_F = 220.0
        self.MEDIUM_GENTEMP_THRESHOLD_F = 212.0
        self.HIGH_GENTEMP_REDUCTION = 0.85
        self.MEDIUM_GENTEMP_REDUCTION = 0.90
        self.DEFAULT_ALTITUDE_FEET = 1000.0
        self.DEFAULT_GENERATOR_TEMP_F = 180.0
        self.DEFAULT_OUTDOOR_TEMP_F = 77.0

        if not os.path.exists(CONFIG_FILE_PATH):
            logging.warning(f"Config file not found at {CONFIG_FILE_PATH}. Using default settings.")
            return

        try:
            config.read(CONFIG_FILE_PATH)
            logging.info(f"Successfully loaded settings from {CONFIG_FILE_PATH}")
            
            # Read DeratingConstants
            self.BASE_TEMPERATURE_THRESHOLD_F = config.getfloat('DeratingConstants', 'BaseTemperatureThresholdF', fallback=self.BASE_TEMPERATURE_THRESHOLD_F)
            self.TEMP_COEFFICIENT = config.getfloat('DeratingConstants', 'TempCoefficient', fallback=self.TEMP_COEFFICIENT)
            self.ALTITUDE_COEFFICIENT = config.getfloat('DeratingConstants', 'AltitudeCoefficient', fallback=self.ALTITUDE_COEFFICIENT)
            self.BASE_GENERATOR_OUTPUT_AMPS = config.getfloat('DeratingConstants', 'BaseGeneratorOutputAmps', fallback=self.BASE_GENERATOR_OUTPUT_AMPS)
            self.OUTPUT_BUFFER = config.getfloat('DeratingConstants', 'OutputBuffer', fallback=self.OUTPUT_BUFFER)
            self.HIGH_GENTEMP_THRESHOLD_F = config.getfloat('DeratingConstants', 'HighGenTempThresholdF', fallback=self.HIGH_GENTEMP_THRESHOLD_F)
            self.MEDIUM_GENTEMP_THRESHOLD_F = config.getfloat('DeratingConstants', 'MediumGenTempThresholdF', fallback=self.MEDIUM_GENTEMP_THRESHOLD_F)
            self.HIGH_GENTEMP_REDUCTION = config.getfloat('DeratingConstants', 'HighGenTempReduction', fallback=self.HIGH_GENTEMP_REDUCTION)
            self.MEDIUM_GENTEMP_REDUCTION = config.getfloat('DeratingConstants', 'MediumGenTempReduction', fallback=self.MEDIUM_GENTEMP_REDUCTION)
            
            # Read DefaultSensorValues
            self.DEFAULT_ALTITUDE_FEET = config.getfloat('DefaultSensorValues', 'DefaultAltitudeFeet', fallback=self.DEFAULT_ALTITUDE_FEET)
            self.DEFAULT_GENERATOR_TEMP_F = config.getfloat('DefaultSensorValues', 'DefaultGeneratorTempF', fallback=self.DEFAULT_GENERATOR_TEMP_F)
            self.DEFAULT_OUTDOOR_TEMP_F = config.getfloat('DefaultSensorValues', 'DefaultOutdoorTempF', fallback=self.DEFAULT_OUTDOOR_TEMP_F)

        except (configparser.Error, ValueError) as e:
            logging.error(f"Error reading config file {CONFIG_FILE_PATH}: {e}. Using default settings.")

    def _find_service_once(self, find_function, service_name_attribute, service_description):
        """Attempts to find a service once and logs the result."""
        find_function()
        if getattr(self, service_name_attribute):
            logging.info(f"Found {service_description}: {getattr(self, service_name_attribute)}")
            return True
        else:
            logging.warning(f"Could not find {service_description}. Will retry in periodic monitoring.")
            return False

    def _delayed_initialization(self):
        # Initial attempts to find services (without extensive retries here)
        self._find_service_once(self._find_vebus_service, 'vebus_service', 'VE.Bus service')
        self._find_service_once(self._find_outdoor_temperature_service, 'outdoor_temp_service_name', 'outdoor temperature service')
        self._find_service_once(self._find_generator_temperature_service, 'generator_temp_service_name', 'generator temperature service')
        self._find_service_once(self._find_gps_service_internal, 'gps_service_name', 'GPS service')
        self._find_service_once(self._find_transfer_switch_input_internal, 'transfer_switch_service', 'transfer switch input service')
        self._find_service_once(self._find_gen_auto_current_input_internal, 'gen_auto_current_service', "'Gen Auto Current' input service")

        self._read_initial_values()
        GLib.timeout_add(5000, self._periodic_monitoring)
        return GLib.SOURCE_REMOVE

    def _read_initial_values(self):
        self._update_outdoor_temperature(log_update=False, log_initial=True)
        self._update_altitude(log_update=False, log_initial=True)
        self._update_generator_temperature(log_update=False, log_initial=True)
        self._update_gen_auto_current_state(initial_read=True)
        # Initial read of the generator current limit setting
        current_limit = self._get_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH)
        if current_limit is not None:
            self.previous_generator_current_limit_setting = round(float(current_limit), 1)
            logging.info(f"Initial Generator Current Limit setting: {self.previous_generator_current_limit_setting:.1f} Amps")

        # Initial read of the AC active input current limit
        ac_limit = self._get_dbus_value(self.vebus_service, AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH)
        if ac_limit is not None:
            self.previous_ac_current_limit = round(float(ac_limit), 1)
            logging.info(f"Initial VE.Bus AC Active Input Current Limit: {self.previous_ac_current_limit:.1f} Amps")

    def _find_service(self, service_base):
        services = [name for name in self.bus.list_names() if name.startswith(service_base)]
        return services[0] if services else None

    def _find_vebus_service(self):
        self.vebus_service = self._find_service(VEBUS_SERVICE_BASE)

    def _get_dbus_value(self, service_name, path):
        if not service_name: # Added check
            return None
        try:
            obj = self.bus.get_object(service_name, path)
            interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
            return interface.GetValue()
        except dbus.exceptions.DBusException as e: # More specific exception
            logging.error(f"D-Bus error getting value from {service_name}{path}: {e}")
            return None
        except Exception as e: # Catch other unexpected errors
            logging.error(f"Unexpected error getting value from {service_name}{path}: {e}")
            return None

    def _set_dbus_value(self, service_name, path, value):
        if not service_name: # Added check
            logging.warning(f"Attempted to set D-Bus value for {path} but service_name is None.")
            return
        try:
            obj = self.bus.get_object(service_name, path)
            interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
            interface.SetValue(wrap_dbus_value(value))
        except dbus.exceptions.DBusException as e: # More specific exception
            logging.error(f"D-Bus error setting value for {service_name}{path} to {value}: {e}")
        except Exception as e: # Catch other unexpected errors
            logging.error(f"Unexpected error setting value for {service_name}{path} to {value}: {e}")

    def _find_outdoor_temperature_service(self):
        self.outdoor_temp_service_name = None # Reset before search
        temperature_services = [name for name in self.bus.list_names() if name.startswith(TEMPERATURE_SERVICE_BASE)]
        for service_name in temperature_services:
            try:
                obj = self.bus.get_object(service_name, CUSTOM_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                custom_name = interface.GetValue()
                logging.debug(f"Checking service: {service_name}, CustomName: '{custom_name}' for outdoor temperature.")
                if custom_name and "Outdoor" in custom_name:
                    self.outdoor_temp_service_name = service_name
                    return
            except dbus.exceptions.DBusException as e:
                logging.debug(f"D-Bus error checking CustomName for {service_name}: {e}")
            except Exception as e:
                logging.debug(f"Unexpected error checking CustomName for {service_name}: {e}")

    def _find_generator_temperature_service(self):
        self.generator_temp_service_name = None # Reset before search
        temperature_services = [name for name in self.bus.list_names() if name.startswith(TEMPERATURE_SERVICE_BASE)]
        for service_name in temperature_services:
            try:
                obj = self.bus.get_object(service_name, CUSTOM_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                custom_name = interface.GetValue()
                logging.debug(f"Checking service: {service_name}, CustomName: '{custom_name}' for generator temperature.")
                if custom_name and any(keyword in custom_name for keyword in ["gen", "Gen", "generator", "Generator"]):
                    self.generator_temp_service_name = service_name
                    return
            except dbus.exceptions.DBusException as e:
                logging.debug(f"D-Bus error checking CustomName for {service_name}: {e}")
            except Exception as e:
                logging.debug(f"Unexpected error checking CustomName for {service_name}: {e}")

            try:
                obj = self.bus.get_object(service_name, PRODUCT_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                product_name = interface.GetValue()
                logging.debug(f"Checking service: {service_name}, ProductName: '{product_name}' for generator temperature.")
                if product_name and any(keyword in product_name for keyword in ["gen", "Gen", "generator", "Generator"]):
                    self.generator_temp_service_name = service_name
                    return
            except dbus.exceptions.DBusException as e:
                logging.debug(f"D-Bus error checking ProductName for {service_name}: {e}")
            except Exception as e:
                logging.debug(f"Unexpected error checking ProductName for {service_name}: {e}")

    def _find_gps_service_internal(self): # Renamed to internal
        self.gps_service_name = self._find_service(GPS_SERVICE_BASE)

    def _find_transfer_switch_input_internal(self): # Renamed to internal
        self.transfer_switch_service = None # Reset before search
        service_names = [name for name in self.bus.list_names() if name.startswith(DIGITAL_INPUT_SERVICE_BASE)]
        for service_name in service_names:
            try:
                obj = self.bus.get_object(service_name, PRODUCT_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                product_name = interface.GetValue()
                logging.debug(f"Checking service: {service_name}, ProductName: '{product_name}' for transfer switch.")
                if product_name and ("Transfer Switch" in product_name or "transfer switch" in product_name):
                    self.transfer_switch_service = service_name
                    return
            except dbus.exceptions.DBusException as e:
                logging.debug(f"D-Bus error checking product name for {service_name}: {e}")
            except Exception as e:
                logging.debug(f"Unexpected error checking product name for {service_name}: {e}")

    def _find_gen_auto_current_input_internal(self): # Renamed to internal
        self.gen_auto_current_service = None # Reset before search
        service_names = [name for name in self.bus.list_names() if name.startswith(DIGITAL_INPUT_SERVICE_BASE)]
        for service_name in service_names:
            try:
                obj = self.bus.get_object(service_name, PRODUCT_NAME_PATH)
                interface = dbus.Interface(obj, BUS_ITEM_INTERFACE)
                product_name = interface.GetValue()
                logging.debug(f"Checking service: {service_name}, ProductName: '{product_name}' for Gen Auto Current.")
                if product_name and ("Gen Auto Current" in product_name or "gen auto current" in product_name):
                    self.gen_auto_current_service = service_name
                    return
            except dbus.exceptions.DBusException as e:
                logging.debug(f"D-Bus error checking product name for {service_name}: {e}")
            except Exception as e:
                logging.debug(f"Unexpected error checking product name for {service_name}: {e}")

    def _update_outdoor_temperature(self, log_update=True, log_initial=False):
        if self.outdoor_temp_service_name:
            temp_celsius = self._get_dbus_value(self.outdoor_temp_service_name, TEMPERATURE_PATH)
            if temp_celsius is not None:
                self.outdoor_temp_fahrenheit = (temp_celsius * 9/5) + 32
                if log_initial and self.initial_outdoor_temp is None:
                    self.initial_outdoor_temp = self.outdoor_temp_fahrenheit
                    logging.info(f"Initial Outdoor Temperature: {self.initial_outdoor_temp:.2f} F")
                elif log_update:
                    logging.debug(f"Updated outdoor temperature: {self.outdoor_temp_fahrenheit:.2f} F")
            else:
                logging.debug("Could not retrieve outdoor temperature from D-Bus. Service might be gone or path invalid.")
        else:
            logging.debug("Outdoor temperature service not found. Using default value.")

    def _update_altitude(self, log_update=True, log_initial=False):
        if self.gps_service_name:
            altitude_raw = self._get_dbus_value(self.gps_service_name, ALTITUDE_PATH)
            altitude_meters = None # Initialize to None

            if altitude_raw is not None:
                try:
                    if isinstance(altitude_raw, dbus.Array):
                        if altitude_raw:
                            altitude_meters = float(altitude_raw[0])
                        else:
                            if not self.altitude_warning_logged:
                                logging.warning("Received empty dbus.Array for altitude. Using previous or default altitude.")
                                self.altitude_warning_logged = True
                            self.altitude_value_logged_after_warning = False # Reset flag for next valid value
                    else:
                        altitude_meters = float(altitude_raw)

                    if altitude_meters is not None:
                        self.altitude_feet = altitude_meters * 3.28084
                        if log_initial and self.initial_altitude is None:
                            self.initial_altitude = self.altitude_feet
                            logging.info(f"Initial Altitude: {self.initial_altitude:.2f} feet")
                        elif log_update:
                            if self.altitude_warning_logged or not self.altitude_value_logged_after_warning:
                                logging.info(f"Updated altitude: {self.altitude_feet:.2f} feet")
                                self.altitude_warning_logged = False # Reset warning flag
                                self.altitude_value_logged_after_warning = True # Set flag to prevent continuous info logs
                            else:
                                logging.debug(f"Updated altitude: {self.altitude_feet:.2f} feet")
                except (ValueError, TypeError) as e:
                    if not self.altitude_warning_logged:
                        logging.warning(f"Error converting altitude_raw '{altitude_raw}' to float: {e}. Using previous or default altitude.")
                        self.altitude_warning_logged = True
                    self.altitude_value_logged_after_warning = False # Reset flag for next valid value
            else:
                if not self.altitude_warning_logged:
                    logging.warning("Could not retrieve altitude from D-Bus. Service might be gone or path invalid. Using previous or default altitude.")
                    self.altitude_warning_logged = True
                self.altitude_value_logged_after_warning = False
        else:
            logging.debug("GPS service not found for altitude. Using default value.")
            self.altitude_value_logged_after_warning = False

    def _update_generator_temperature(self, log_update=True, log_initial=False):
        if self.generator_temp_service_name:
            temp_celsius = self._get_dbus_value(self.generator_temp_service_name, TEMPERATURE_PATH)
            if temp_celsius is not None:
                self.generator_temp_fahrenheit = (temp_celsius * 9/5) + 32
                if log_initial and self.initial_generator_temp is None:
                    self.initial_generator_temp = self.generator_temp_fahrenheit
                    logging.info(f"Initial Generator Temperature: {self.initial_generator_temp:.2f} F")
                elif log_update and self.generator_temp_fahrenheit > 212.0:
                    logging.debug(f"Generator temperature above threshold: {self.generator_temp_fahrenheit:.2f} F")
                elif log_update:
                    logging.debug(f"Generator temperature: {self.generator_temp_fahrenheit:.2f} F (below threshold)")
            else:
                logging.debug("Could not retrieve generator temperature from D-Bus. Service might be gone or path invalid.")
        else:
            logging.debug("Generator temperature service not found. Using default value.")

    def _update_gen_auto_current_state(self, initial_read=False):
        if self.gen_auto_current_service:
            state = self._get_dbus_value(self.gen_auto_current_service, STATE_PATH)
            if state is not None:
                state = int(state)
                if initial_read:
                    self.gen_auto_current_state = state
                    self.previous_gen_auto_current_state = state
                    logging.info(f"Initial 'Gen Auto Current' state: {self.gen_auto_current_state} (ON: {GEN_AUTO_CURRENT_ON}, OFF: {GEN_AUTO_CURRENT_OFF})")
                elif state != self.previous_gen_auto_current_state:
                    self.previous_gen_auto_current_state = self.gen_auto_current_state
                    self.gen_auto_current_state = state
                    logging.info(f"'Gen Auto Current' state changed to: {self.gen_auto_current_state} (ON: {GEN_AUTO_CURRENT_ON}, OFF: {GEN_AUTO_CURRENT_OFF})")
                else:
                    self.gen_auto_current_state = state
                    logging.debug(f"'Gen Auto Current' state remains: {self.gen_auto_current_state} (ON: {GEN_AUTO_CURRENT_ON}, OFF: {GEN_AUTO_CURRENT_OFF})")
            else:
                logging.debug("Could not retrieve 'Gen Auto Current' state from D-Bus.")
        else:
            logging.debug("'Gen Auto Current' input service not found. Cannot read state.")


    def _is_generator_running(self):
        if self.transfer_switch_service:
            state = self._get_dbus_value(self.transfer_switch_service, STATE_PATH)
            return state in GENERATOR_ON_VALUE
        return False

    def calculate_derating_factor(self, temperature_fahrenheit, altitude_feet, generator_temperature_fahrenheit):
        temperature_multiplier = 1.0
        altitude_multiplier = 1.0
        generator_temp_multiplier = 1.0

        if temperature_fahrenheit is not None:
            if temperature_fahrenheit > self.BASE_TEMPERATURE_THRESHOLD_F:
                temperature_multiplier = 1.0 - ((temperature_fahrenheit - self.BASE_TEMPERATURE_THRESHOLD_F) * self.TEMP_COEFFICIENT)
                temperature_multiplier = max(0.0, temperature_multiplier)

        if altitude_feet is not None:
            altitude_multiplier = 1.0 - (altitude_feet * self.ALTITUDE_COEFFICIENT)
            altitude_multiplier = max(0.0, altitude_multiplier)

        if generator_temperature_fahrenheit is not None:
            if generator_temperature_fahrenheit >= self.HIGH_GENTEMP_THRESHOLD_F:
                generator_temp_multiplier = self.HIGH_GENTEMP_REDUCTION
            elif generator_temperature_fahrenheit >= self.MEDIUM_GENTEMP_THRESHOLD_F:
                generator_temp_multiplier = self.MEDIUM_GENTEMP_REDUCTION

        return temperature_multiplier * altitude_multiplier * generator_temp_multiplier * self.OUTPUT_BUFFER

    def _perform_derating(self):
        if self.outdoor_temp_fahrenheit is not None and self.altitude_feet is not None and self.generator_temp_fahrenheit is not None:
            derating_factor = self.calculate_derating_factor(
                self.outdoor_temp_fahrenheit, self.altitude_feet, self.generator_temp_fahrenheit
            )
            derated_output_amps = self.BASE_GENERATOR_OUTPUT_AMPS * derating_factor
            rounded_output = round(derated_output_amps, 1)

            current_generator_limit_setting = self._get_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH)

            if not self.initial_derated_output_logged:
                self._set_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH, rounded_output)
                logging.info(f"Initial Transfer Switch Generator Current Limit set to: {rounded_output:.1f} Amps (due to auto derating)")
                self.initial_derated_output_logged = True
            elif current_generator_limit_setting is None or abs(current_generator_limit_setting - rounded_output) > 0.01:
                self._set_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH, rounded_output)
                logging.debug(f"Transfer Switch Generator Current Limit updated to: {rounded_output:.1f} Amps (due to auto derating)")
            else:
                logging.debug(f"Transfer Switch Generator Current Limit remains: {rounded_output:.1f} Amps")

        else:
            logging.warning("Not all temperature or altitude data available for derating. Skipping calculation.")

    def _sync_generator_limit_to_ac_input(self):
        if self.vebus_service and self._is_generator_running():
            current_generator_limit_setting = self._get_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH)
            if current_generator_limit_setting is not None:
                rounded_gen_limit = round(float(current_generator_limit_setting), 1)

                if self.previous_generator_current_limit_setting is None or abs(self.previous_generator_current_limit_setting - rounded_gen_limit) > 0.01:
                    self._set_dbus_value(self.vebus_service, AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH, rounded_gen_limit)
                    logging.debug(f"Generator running: Synced VE.Bus AC Active Input Current Limit to Generator Current Limit ({rounded_gen_limit:.1f} Amps).")
                    self.previous_ac_current_limit = rounded_gen_limit
                    self.previous_generator_current_limit_setting = rounded_gen_limit
                else:
                    logging.debug(f"Generator running: VE.Bus AC Active Input Current Limit already matches Generator Current Limit ({rounded_gen_limit:.1f} Amps).")
            else:
                logging.warning("Could not retrieve Generator Current Limit setting. Cannot sync to AC input.")
        elif self.vebus_service:
            logging.debug("Generator not running, AC Active Input Current Limit not synced from generator current limit setting.")


    def _sync_generator_limit_from_ac_input(self):
        if self.vebus_service and self._is_generator_running():
            current_ac_limit = self._get_dbus_value(self.vebus_service, AC_ACTIVE_INPUT_CURRENT_LIMIT_PATH)
            if current_ac_limit is not None:
                rounded_ac_limit = round(float(current_ac_limit), 1)

                if self.previous_ac_current_limit is None or abs(rounded_ac_limit - self.previous_ac_current_limit) > 0.01:
                    current_gen_limit = self._get_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH)
                    if current_gen_limit is None or abs(current_gen_limit - rounded_ac_limit) > 0.01:
                        self._set_dbus_value(self.settings_service_name, GENERATOR_CURRENT_LIMIT_PATH, rounded_ac_limit)
                        logging.info(f"Generator running and Active AC Current Limit has been manually changed: Synced Generator Current Limit to VE.Bus AC Active Input Current Limit ({rounded_ac_limit:.1f} Amps).")
                        self.previous_generator_current_limit_setting = rounded_ac_limit

                    self.previous_ac_current_limit = rounded_ac_limit
                else:
                    logging.debug(f"Generator running: VE.Bus AC Active Input Current Limit ({rounded_ac_limit:.1f} Amps) has not changed.")
            else:
                logging.warning("Could not retrieve VE.Bus AC Active Input Current Limit. Cannot sync to generator current limit.")
        elif self.vebus_service:
            if not self._is_generator_running():
                logging.debug("Generator not running, AC Active Input Current Limit not synced to generator current limit.")
            elif self.gen_auto_current_state == GEN_AUTO_CURRENT_ON:
                logging.debug(f"'Gen Auto Current' is ON ({GEN_AUTO_CURRENT_ON}), AC Active Input Current Limit not synced to generator current limit.")

    def _periodic_monitoring(self):
        if not self.vebus_service:
            self._find_service_once(self._find_vebus_service, 'vebus_service', 'VE.Bus service')
        if not self.outdoor_temp_service_name:
            self._find_service_once(self._find_outdoor_temperature_service, 'outdoor_temp_service_name', 'outdoor temperature service')
        if not self.generator_temp_service_name:
            self._find_service_once(self._find_generator_temperature_service, 'generator_temp_service_name', 'generator temperature service')
        if not self.gps_service_name:
            self._find_service_once(self._find_gps_service_internal, 'gps_service_name', 'GPS service')
        if not self.transfer_switch_service:
            self._find_service_once(self._find_transfer_switch_input_internal, 'transfer_switch_service', 'transfer switch input service')
        if not self.gen_auto_current_service:
            self._find_service_once(self._find_gen_auto_current_input_internal, 'gen_auto_current_service', "'Gen Auto Current' input service")

        self._update_outdoor_temperature()
        self._update_altitude()
        self._update_generator_temperature()
        self._update_gen_auto_current_state()

        self._sync_generator_limit_to_ac_input()

        if self._is_generator_running() and self.gen_auto_current_state == GEN_AUTO_CURRENT_OFF:
             self._sync_generator_limit_from_ac_input()
        else:
             logging.debug(f"Generator not running or 'Gen Auto Current' is ON ({self.gen_auto_current_state}). Skipping sync from AC input.")

        if self.gen_auto_current_state == GEN_AUTO_CURRENT_ON:
            self._perform_derating()
        else:
            logging.debug(f"Gen Auto Current state is not ON ({GEN_AUTO_CURRENT_ON}). Current state: {self.gen_auto_current_state}")

        return True

def main():
    DBusGMainLoop(set_as_default=True)
    GeneratorDeratingMonitor()
    mainloop = GLib.MainLoop()
    mainloop.run()

if __name__ == "__main__":
    main()
