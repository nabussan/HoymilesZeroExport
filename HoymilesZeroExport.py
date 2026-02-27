# HoymilesZeroExport - https://github.com/reserve85/HoymilesZeroExport
# Copyright (C) 2023, Tobias Kraft

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# Bugfixes 1.104 02 (funktionskritisch):

# Battery-Priority-Block: headroom_batt == 0-Guard ergänzt (Division by Zero)
# WaitForAck (Ahoy + OpenDTU): ack = False vor der while-Schleife initialisiert (NameError bei timeout=0)
# 1.104 02powermeterWatts im Main-Loop: Initialisierung mit powermeter_target_point statt undefiniert aus Vorschleife

# Stilverbesserungen:

# from statistics import mean an den Dateianfang verschoben
# ApplyLimitsToSetpoint auf max/min-Einzeiler vereinfacht
#  GetMin/MaxWatt*-Funktionen auf sum()-Comprehensions vereinheitlicht
# f-strings statt String-Konkatenation im Logging
# Redundante Variablen entfernt (headroom statt doppelter max-min-Berechnung)

__author__ = "Tobias Kraft"
__version__ = "1.104 02: divide by zero fix plus optimizations"

import time
import json
import os
import logging
import sys
import argparse
import subprocess
from statistics import mean                          # FIX: moved from hot-path to top-level
from logging.handlers import TimedRotatingFileHandler
from configparser import ConfigParser
from pathlib import Path
from packaging import version
from requests.sessions import Session
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from dotenv import load_dotenv
from config_provider import ConfigFileConfigProvider, MqttHandler, ConfigProviderChain

load_dotenv()

session = Session()

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger()

parser = argparse.ArgumentParser()
parser.add_argument('-c', '--config', help='Override configuration file path')
args = parser.parse_args()


def replace_placeholders(config):
    """Ersetzt ${VARIABLE}-Platzhalter in der Config durch Umgebungsvariablen."""
    for section in config.sections():
        for option in config.options(section):
            value = config.get(section, option)
            if value.startswith('${') and value.endswith('}'):
                env_var = value[2:-1]
                replacement = os.getenv(env_var)
                if replacement is not None:
                    config.set(section, option, replacement)
                else:
                    raise ValueError(
                        f"Umgebungsvariable '{env_var}' nicht gefunden in [{section}] / {option}"
                    )


try:
    config = ConfigParser()
    baseconfig = str(Path.joinpath(Path(__file__).parent.resolve(), "HoymilesZeroExport_Config.ini"))
    if args.config:
        config.read([baseconfig, args.config])
    else:
        config.read(baseconfig)
        replace_placeholders(config)
    ENABLE_LOG_TO_FILE = config.getboolean('COMMON', 'ENABLE_LOG_TO_FILE')
    LOG_BACKUP_COUNT = config.getint('COMMON', 'LOG_BACKUP_COUNT')
except Exception as e:
    logger.info('Error on reading ENABLE_LOG_TO_FILE, set it to DISABLED')
    ENABLE_LOG_TO_FILE = False
    logger.error(e.message if hasattr(e, 'message') else e)

if ENABLE_LOG_TO_FILE:
    log_dir = Path.joinpath(Path(__file__).parent.resolve(), 'log')
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    rotating_file_handler = TimedRotatingFileHandler(
        filename=Path.joinpath(log_dir, 'log'),
        when='midnight',
        interval=2,
        backupCount=LOG_BACKUP_COUNT)
    rotating_file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-8s %(message)s'))
    logger.addHandler(rotating_file_handler)

logger.info('Log write to file: %s', ENABLE_LOG_TO_FILE)
logger.info('Python Version: ' + sys.version)

try:
    assert sys.version_info >= (3, 8)
except AssertionError:
    logger.error('Error: Python version too old. Requires >= 3.8. Please update.')
    sys.exit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def CastToInt(pValueToCast):
    try:
        return int(pValueToCast)
    except Exception:
        pass
    try:
        return int(float(pValueToCast))
    except Exception:
        logger.error("Exception at CastToInt")
        raise


def GetNumberArray(pExcludedPanels):
    result = []
    for number_str in pExcludedPanels.split(','):
        if number_str == '':
            continue
        result.append(int(number_str.strip()))
    return result


def extract_json_value(data, path):
    from jsonpath_ng import parse
    jsonpath_expr = parse(path)
    match = jsonpath_expr.find(data)
    if match:
        return int(float(match[0].value))
    raise ValueError("No match found for the JSON path")


# ---------------------------------------------------------------------------
# Core control functions
# ---------------------------------------------------------------------------

def SetLimit(pLimit):
    try:
        if not hasattr(SetLimit, "LastLimit"):
            SetLimit.LastLimit = CastToInt(0)
        if not hasattr(SetLimit, "LastLimitAck"):
            SetLimit.LastLimitAck = bool(False)

        if (SetLimit.LastLimit == CastToInt(pLimit)) and SetLimit.LastLimitAck:
            logger.info("Inverterlimit was already accepted at %s Watt", CastToInt(pLimit))
            CrossCheckLimit()
            return
        if (SetLimit.LastLimit == CastToInt(pLimit)) and not SetLimit.LastLimitAck:
            logger.info(
                "Inverterlimit %s Watt was previously not accepted, trying again...", CastToInt(pLimit)
            )

        logger.info("setting new limit to %s Watt", CastToInt(pLimit))
        SetLimit.LastLimit = CastToInt(pLimit)
        SetLimit.LastLimitAck = True

        reachable_inverters = [
            i for i in range(INVERTER_COUNT)
            if AVAILABLE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
        ]
        if not reachable_inverters:
            logger.warning("No reachable inverters – skipping limit set")
            return

        unreachable_inverters = [
            i for i in range(INVERTER_COUNT)
            if not (AVAILABLE[i] and HOY_BATTERY_GOOD_VOLTAGE[i])
        ]
        for i in unreachable_inverters:
            name = NAME[i] if NAME[i] != 'yet unknown' else 'yet unknown'
            logger.info("Inverter %s (%s) not reachable, excluded from limit calculation", i + 1, name)

        min_watt_reachable = sum(GetMinWatt(i) for i in reachable_inverters)
        if CastToInt(pLimit) <= min_watt_reachable:
            pLimit = min_watt_reachable
            PublishGlobalState("limit", min_watt_reachable)
        else:
            PublishGlobalState("limit", CastToInt(pLimit))

        RemainingLimit = CastToInt(pLimit) - min_watt_reachable

        # --- Non-battery inverters ---
        non_battery_reachable = [i for i in reachable_inverters if not HOY_BATTERY_MODE[i]]
        if non_battery_reachable:
            max_non_battery = sum(HOY_MAX_WATT[i] for i in non_battery_reachable)
            min_non_battery = sum(GetMinWatt(i) for i in non_battery_reachable)
            headroom = max_non_battery - min_non_battery

            if headroom == 0:
                logger.warning("No adjustable non-battery inverters – skipping non-battery section")
                nonBatteryInvertersLimit = 0
            else:
                nonBatteryInvertersLimit = min(RemainingLimit, headroom)
                for i in non_battery_reachable:
                    NewLimit = (
                        CastToInt(nonBatteryInvertersLimit * (HOY_MAX_WATT[i] - GetMinWatt(i)) / headroom)
                        + GetMinWatt(i)
                    )
                    NewLimit = ApplyLimitsToSetpointInverter(i, NewLimit)
                    if HOY_COMPENSATE_WATT_FACTOR[i] != 1:
                        logger.info(
                            'OpenDTU: Inverter "%s": compensate Limit from %s W to %s W',
                            NAME[i], CastToInt(NewLimit),
                            CastToInt(NewLimit * HOY_COMPENSATE_WATT_FACTOR[i])
                        )
                        NewLimit = CastToInt(NewLimit * HOY_COMPENSATE_WATT_FACTOR[i])
                        NewLimit = ApplyLimitsToMaxInverterLimits(i, NewLimit)

                    if NewLimit == CastToInt(CURRENT_LIMIT[i]) and LASTLIMITACKNOWLEDGED[i]:
                        logger.info('Inverter "%s": Already at %s Watt', NAME[i], CastToInt(NewLimit))
                        continue

                    LASTLIMITACKNOWLEDGED[i] = True
                    PublishInverterState(i, "limit", NewLimit)
                    DTU.SetLimit(i, NewLimit)
                    if not DTU.WaitForAck(i, SET_LIMIT_TIMEOUT_SECONDS):
                        SetLimit.LastLimitAck = False
                        LASTLIMITACKNOWLEDGED[i] = False

                RemainingLimit -= nonBatteryInvertersLimit

        # --- Battery inverters by priority ---
        battery_reachable = [i for i in reachable_inverters if HOY_BATTERY_MODE[i]]
        for j in range(1, 6):
            battery_same_prio = [
                i for i in battery_reachable
                if CONFIG_PROVIDER.get_battery_priority(i) == j
            ]
            if not battery_same_prio:
                continue

            max_battery_prio = sum(HOY_MAX_WATT[i] for i in battery_same_prio)
            min_battery_prio = sum(GetMinWatt(i) for i in battery_same_prio)
            headroom_batt = max_battery_prio - min_battery_prio

            # FIX: guard against division by zero in battery priority block
            if headroom_batt == 0:
                logger.warning(
                    "No adjustable battery inverters with priority %s – skipping", j
                )
                continue

            LimitPrio = min(RemainingLimit, headroom_batt)

            for i in battery_same_prio:
                if not HOY_BATTERY_GOOD_VOLTAGE[i]:
                    continue
                NewLimit = (
                    CastToInt(LimitPrio * (HOY_MAX_WATT[i] - GetMinWatt(i)) / headroom_batt)
                    + GetMinWatt(i)
                )
                NewLimit = ApplyLimitsToSetpointInverter(i, NewLimit)
                if HOY_COMPENSATE_WATT_FACTOR[i] != 1:
                    logger.info(
                        'OpenDTU: Inverter "%s": compensate Limit from %s W to %s W',
                        NAME[i], CastToInt(NewLimit),
                        CastToInt(NewLimit * HOY_COMPENSATE_WATT_FACTOR[i])
                    )
                    NewLimit = CastToInt(NewLimit * HOY_COMPENSATE_WATT_FACTOR[i])
                    NewLimit = ApplyLimitsToMaxInverterLimits(i, NewLimit)

                if NewLimit == CastToInt(CURRENT_LIMIT[i]) and LASTLIMITACKNOWLEDGED[i]:
                    logger.info('Inverter "%s": Already at %s Watt', NAME[i], CastToInt(NewLimit))
                    continue

                LASTLIMITACKNOWLEDGED[i] = True
                PublishInverterState(i, "limit", NewLimit)
                DTU.SetLimit(i, NewLimit)
                if not DTU.WaitForAck(i, SET_LIMIT_TIMEOUT_SECONDS):
                    SetLimit.LastLimitAck = False
                    LASTLIMITACKNOWLEDGED[i] = False

            RemainingLimit -= LimitPrio

    except Exception as e:
        logger.error("Exception at SetLimit: %s", e)
        SetLimit.LastLimitAck = False
        raise


def ResetInverterData(pInverterId):
    for target_object in [SetLimit, GetHoymilesPanelMinVoltage]:
        for attribute in ["LastLimit", "LastLimitAck"]:
            if hasattr(target_object, attribute):
                delattr(target_object, attribute)
        for key, value in [("LastPowerStatus", False), ("SamePowerStatusCnt", 0)]:
            if hasattr(target_object, key):
                getattr(target_object, key)[pInverterId] = value

    LASTLIMITACKNOWLEDGED[pInverterId] = False
    HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId] = []
    CURRENT_LIMIT[pInverterId] = -1
    HOY_BATTERY_GOOD_VOLTAGE[pInverterId] = True
    TEMPERATURE[pInverterId] = '--- degC'


def GetHoymilesAvailable():
    try:
        result = False
        for i in range(INVERTER_COUNT):
            try:
                WasAvail = AVAILABLE[i]
                AVAILABLE[i] = ENABLED[i] and DTU.GetAvailable(i)
                if AVAILABLE[i]:
                    result = True
                    if not WasAvail:
                        ResetInverterData(i)
                        GetHoymilesInfo()
            except Exception as e:
                AVAILABLE[i] = False
                logger.error("Exception at GetHoymilesAvailable, Inverter %s (%s) not reachable", i, NAME[i])
                logger.error(e.message if hasattr(e, 'message') else e)
        return result
    except Exception:
        logger.error('Exception at GetHoymilesAvailable')
        raise


def GetHoymilesInfo():
    try:
        for i in range(INVERTER_COUNT):
            try:
                if not AVAILABLE[i]:
                    continue
                DTU.GetInfo(i)
            except Exception as e:
                logger.error('Exception at GetHoymilesInfo, Inverter "%s" not reachable', NAME[i])
                logger.error(e.message if hasattr(e, 'message') else e)
    except Exception:
        logger.error("Exception at GetHoymilesInfo")
        raise


def GetHoymilesPanelMinVoltage(pInverterId):
    try:
        if not AVAILABLE[pInverterId]:
            return 0
        HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId].append(DTU.GetPanelMinVoltage(pInverterId))
        if len(HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId]) > HOY_BATTERY_AVERAGE_CNT[pInverterId]:
            HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId].pop(0)
        avg = mean(HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST[pInverterId])
        logger.info('Average min-panel voltage, inverter "%s": %s Volt', NAME[pInverterId], avg)
        return avg
    except Exception:
        logger.error("Exception at GetHoymilesPanelMinVoltage, Inverter %s not reachable", pInverterId)
        raise


def SetHoymilesPowerStatus(pInverterId, pActive):
    try:
        if not AVAILABLE[pInverterId]:
            return
        if SET_POWERSTATUS_CNT > 0:
            if not hasattr(SetHoymilesPowerStatus, "LastPowerStatus"):
                SetHoymilesPowerStatus.LastPowerStatus = [False] * INVERTER_COUNT
            if not hasattr(SetHoymilesPowerStatus, "SamePowerStatusCnt"):
                SetHoymilesPowerStatus.SamePowerStatusCnt = [0] * INVERTER_COUNT
            if SetHoymilesPowerStatus.LastPowerStatus[pInverterId] == pActive:
                SetHoymilesPowerStatus.SamePowerStatusCnt[pInverterId] += 1
            else:
                SetHoymilesPowerStatus.LastPowerStatus[pInverterId] = pActive
                SetHoymilesPowerStatus.SamePowerStatusCnt[pInverterId] = 0
            if SetHoymilesPowerStatus.SamePowerStatusCnt[pInverterId] > SET_POWERSTATUS_CNT:
                label = "ON" if pActive else "OFF"
                logger.info("Retry Counter exceeded: Inverter PowerStatus already %s", label)
                return
        DTU.SetPowerStatus(pInverterId, pActive)
        time.sleep(SET_POWER_STATUS_DELAY_IN_SECONDS)
    except Exception:
        logger.error("Exception at SetHoymilesPowerStatus")
        raise


def GetCheckBattery():
    try:
        result = False
        for i in range(INVERTER_COUNT):
            try:
                if not AVAILABLE[i]:
                    continue
                if not HOY_BATTERY_MODE[i]:
                    result = True
                    continue
                minVoltage = GetHoymilesPanelMinVoltage(i)
                if minVoltage <= HOY_BATTERY_THRESHOLD_OFF_LIMIT_IN_V[i]:
                    SetHoymilesPowerStatus(i, False)
                    HOY_BATTERY_GOOD_VOLTAGE[i] = False
                    HOY_MAX_WATT[i] = CONFIG_PROVIDER.get_reduce_wattage(i)
                elif minVoltage <= HOY_BATTERY_THRESHOLD_REDUCE_LIMIT_IN_V[i]:
                    if HOY_MAX_WATT[i] != CONFIG_PROVIDER.get_reduce_wattage(i):
                        HOY_MAX_WATT[i] = CONFIG_PROVIDER.get_reduce_wattage(i)
                        SetLimit.LastLimit = -1
                elif minVoltage >= HOY_BATTERY_THRESHOLD_ON_LIMIT_IN_V[i]:
                    SetHoymilesPowerStatus(i, True)
                    if not HOY_BATTERY_GOOD_VOLTAGE[i]:
                        DTU.SetLimit(i, GetMinWatt(i))
                        DTU.WaitForAck(i, SET_LIMIT_TIMEOUT_SECONDS)
                        SetLimit.LastLimit = -1
                    HOY_BATTERY_GOOD_VOLTAGE[i] = True
                    if (minVoltage >= HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V[i]) and \
                            (HOY_MAX_WATT[i] != CONFIG_PROVIDER.get_normal_wattage(i)):
                        HOY_MAX_WATT[i] = CONFIG_PROVIDER.get_normal_wattage(i)
                        SetLimit.LastLimit = -1
                elif minVoltage >= HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V[i]:
                    if HOY_MAX_WATT[i] != CONFIG_PROVIDER.get_normal_wattage(i):
                        HOY_MAX_WATT[i] = CONFIG_PROVIDER.get_normal_wattage(i)
                        SetLimit.LastLimit = -1
                if HOY_BATTERY_GOOD_VOLTAGE[i]:
                    result = True
            except Exception:
                logger.error("Exception at CheckBattery, Inverter %s not reachable", i)
        return result
    except Exception:
        logger.error("Exception at CheckBattery")
        raise


def GetHoymilesTemperature():
    try:
        for i in range(INVERTER_COUNT):
            try:
                DTU.GetTemperature(i)
            except Exception:
                logger.error("Exception at GetHoymilesTemperature, Inverter %s not reachable", i)
    except Exception:
        logger.error("Exception at GetHoymilesTemperature")
        raise


def GetHoymilesActualPower():
    try:
        try:
            Watts = abs(INTERMEDIATE_POWERMETER.GetPowermeterWatts())
            logger.info("intermediate meter %s: %s Watt", INTERMEDIATE_POWERMETER.__class__.__name__, Watts)
            return Watts
        except Exception as e:
            logger.error("Exception at GetHoymilesActualPower: %s", e)
            logger.error("Falling back to DTU power reading")
            Watts = DTU.GetPowermeterWatts()
            logger.info("intermediate meter %s: %s Watt", DTU.__class__.__name__, Watts)
            return Watts
    except Exception:
        logger.error("Exception at GetHoymilesActualPower")
        if SET_INVERTER_TO_MIN_ON_POWERMETER_ERROR:
            SetLimit(0)
        raise


def GetPowermeterWatts():
    try:
        Watts = POWERMETER.GetPowermeterWatts()
        logger.info("powermeter %s: %s Watt", POWERMETER.__class__.__name__, Watts)
        return Watts
    except Exception:
        logger.error("Exception at GetPowermeterWatts")
        if SET_INVERTER_TO_MIN_ON_POWERMETER_ERROR:
            SetLimit(0)
        raise


def GetMinWatt(pInverter: int):
    return int(HOY_INVERTER_WATT[pInverter] * CONFIG_PROVIDER.get_min_wattage_in_percent(pInverter) / 100)


def CutLimitToProduction(pSetpoint):
    if pSetpoint != GetMaxWattFromAllInverters():
        ActualPower = GetHoymilesActualPower()
        ceiling = ActualPower + (GetMaxWattFromAllInverters() * MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER / 100)
        if pSetpoint > ceiling:
            pSetpoint = CastToInt(ceiling)
            logger.info(
                'Cut limit to %s Watt (exceeded %s%% of live-production)',
                CastToInt(pSetpoint), MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER
            )
    return CastToInt(pSetpoint)


def ApplyLimitsToSetpoint(pSetpoint):
    return max(GetMinWattFromAllInverters(), min(GetMaxWattFromAllInverters(), pSetpoint))


def ApplyLimitsToSetpointInverter(pInverter, pSetpoint):
    return max(GetMinWatt(pInverter), min(HOY_MAX_WATT[pInverter], pSetpoint))


def ApplyLimitsToMaxInverterLimits(pInverter, pSetpoint):
    return max(GetMinWatt(pInverter), min(HOY_INVERTER_WATT[pInverter], pSetpoint))


def CrossCheckLimit():
    try:
        for i in range(INVERTER_COUNT):
            if AVAILABLE[i]:
                DTULimitInW = DTU.GetActualLimitInW(i)
                tolerance = HOY_INVERTER_WATT[i] * 0.05
                if not (CURRENT_LIMIT[i] - tolerance < DTULimitInW < CURRENT_LIMIT[i] + tolerance):
                    logger.info(
                        'CrossCheckLimit: DTU (%.1f) <> SetLimit (%.1f). Resending.',
                        DTULimitInW, CURRENT_LIMIT[i]
                    )
                    DTU.SetLimit(i, CURRENT_LIMIT[i])
    except Exception:
        logger.error("Exception at CrossCheckLimit")
        raise


def GetMaxWattFromAllInverters():
    return sum(
        HOY_MAX_WATT[i] for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
    )


def GetMaxInverterWattFromAllInverters():
    return sum(
        HOY_INVERTER_WATT[i] for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
    )


def GetMaxWattFromAllNonBatteryInverters():
    return sum(
        HOY_MAX_WATT[i] for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and not HOY_BATTERY_MODE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
    )


def GetMinWattFromAllInverters():
    return sum(
        GetMinWatt(i) for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
    )


def GetMinWattFromAllNonBatteryInverters():
    return sum(
        GetMinWatt(i) for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and not HOY_BATTERY_MODE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
    )


def GetMinWattFromAllBatteryInverters():
    return sum(
        GetMinWatt(i) for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and HOY_BATTERY_MODE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
    )


def GetMinWattFromAllBatteryInvertersWithSamePriority(pPriority):
    return sum(
        GetMinWatt(i) for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and HOY_BATTERY_MODE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
        and CONFIG_PROVIDER.get_battery_priority(i) == pPriority
    )


def GetMaxWattFromAllBatteryInvertersSamePrio(pPriority):
    return sum(
        HOY_MAX_WATT[i] for i in range(INVERTER_COUNT)
        if AVAILABLE[i] and HOY_BATTERY_GOOD_VOLTAGE[i] and HOY_BATTERY_MODE[i]
        and CONFIG_PROVIDER.get_battery_priority(i) == pPriority
    )


# ---------------------------------------------------------------------------
# MQTT publishing helpers
# ---------------------------------------------------------------------------

def PublishConfigState():
    if MQTT is None:
        return
    MQTT.publish_state("on_grid_usage_jump_to_limit_percent", CONFIG_PROVIDER.on_grid_usage_jump_to_limit_percent())
    MQTT.publish_state("on_grid_feed_fast_limit_decrease", CONFIG_PROVIDER.on_grid_feed_fast_limit_decrease())
    MQTT.publish_state("powermeter_target_point", CONFIG_PROVIDER.get_powermeter_target_point())
    MQTT.publish_state("powermeter_max_point", CONFIG_PROVIDER.get_powermeter_max_point())
    MQTT.publish_state("powermeter_min_point", CONFIG_PROVIDER.get_powermeter_min_point())
    MQTT.publish_state("powermeter_tolerance", CONFIG_PROVIDER.get_powermeter_tolerance())
    MQTT.publish_state("inverter_count", INVERTER_COUNT)
    for i in range(INVERTER_COUNT):
        MQTT.publish_inverter_state(i, "min_watt_in_percent", CONFIG_PROVIDER.get_min_wattage_in_percent(i))
        MQTT.publish_inverter_state(i, "normal_watt", CONFIG_PROVIDER.get_normal_wattage(i))
        MQTT.publish_inverter_state(i, "reduce_watt", CONFIG_PROVIDER.get_reduce_wattage(i))
        MQTT.publish_inverter_state(i, "battery_priority", CONFIG_PROVIDER.get_battery_priority(i))


def PublishGlobalState(state_name, state_value):
    if MQTT is not None:
        MQTT.publish_state(state_name, state_value)


def PublishInverterState(inverter_idx, state_name, state_value):
    if MQTT is not None:
        MQTT.publish_inverter_state(inverter_idx, state_name, state_value)


# ---------------------------------------------------------------------------
# Powermeter base class
# ---------------------------------------------------------------------------

class Powermeter:
    def GetPowermeterWatts(self) -> int:
        raise NotImplementedError()


# ---------------------------------------------------------------------------
# Powermeter implementations
# ---------------------------------------------------------------------------

class Tasmota(Powermeter):
    def __init__(self, ip, user, password, json_status, json_payload_mqtt_prefix,
                 json_power_mqtt_label, json_power_input_mqtt_label,
                 json_power_output_mqtt_label, json_power_calculate):
        self.ip = ip
        self.user = user
        self.password = password
        self.json_status = json_status
        self.json_payload_mqtt_prefix = json_payload_mqtt_prefix
        self.json_power_mqtt_label = json_power_mqtt_label
        self.json_power_input_mqtt_label = json_power_input_mqtt_label
        self.json_power_output_mqtt_label = json_power_output_mqtt_label
        self.json_power_calculate = json_power_calculate

    def GetJson(self, path):
        return session.get(f'http://{self.ip}{path}', timeout=10).json()

    def GetPowermeterWatts(self):
        if not self.user:
            data = self.GetJson('/cm?cmnd=status%2010')
        else:
            data = self.GetJson(f'/cm?user={self.user}&password={self.password}&cmnd=status%2010')
        prefix = data[self.json_status][self.json_payload_mqtt_prefix]
        if not self.json_power_calculate:
            return CastToInt(prefix[self.json_power_mqtt_label])
        return CastToInt(prefix[self.json_power_input_mqtt_label] - prefix[self.json_power_output_mqtt_label])


class Shelly(Powermeter):
    def __init__(self, ip, user, password, emeterindex):
        self.ip = ip
        self.user = user
        self.password = password
        self.emeterindex = emeterindex

    def GetJson(self, path):
        return session.get(
            f'http://{self.ip}{path}',
            headers={"content-type": "application/json"},
            auth=(self.user, self.password),
            timeout=10
        ).json()

    def GetRpcJson(self, path):
        return session.get(
            f'http://{self.ip}/rpc{path}',
            headers={"content-type": "application/json"},
            auth=HTTPDigestAuth(self.user, self.password),
            timeout=10
        ).json()

    def GetPowermeterWatts(self) -> int:
        raise NotImplementedError()


class Shelly1PM(Shelly):
    def GetPowermeterWatts(self):
        return CastToInt(self.GetJson('/status')['meters'][0]['power'])


class ShellyPlus1PM(Shelly):
    def GetPowermeterWatts(self):
        return CastToInt(self.GetRpcJson('/Switch.GetStatus?id=0')['apower'])


class ShellyEM(Shelly):
    def GetPowermeterWatts(self):
        if self.emeterindex:
            return CastToInt(self.GetJson(f'/emeter/{self.emeterindex}')['power'])
        return sum(CastToInt(e['power']) for e in self.GetJson('/status')['emeters'])


class Shelly3EM(Shelly):
    def GetPowermeterWatts(self):
        return CastToInt(self.GetJson('/status')['total_power'])


class Shelly3EMPro(Shelly):
    def GetPowermeterWatts(self):
        return CastToInt(self.GetRpcJson('/EM.GetStatus?id=0')['total_act_power'])


class ESPHome(Powermeter):
    def __init__(self, ip, port, domain, id):
        self.ip = ip
        self.port = port
        self.domain = domain
        self.id = id

    def GetPowermeterWatts(self):
        data = session.get(f'http://{self.ip}:{self.port}/{self.domain}/{self.id}', timeout=10).json()
        return CastToInt(data['value'])


class Shrdzm(Powermeter):
    def __init__(self, ip, user, password):
        self.ip = ip
        self.user = user
        self.password = password

    def GetPowermeterWatts(self):
        data = session.get(
            f'http://{self.ip}/getLastData?user={self.user}&password={self.password}', timeout=10
        ).json()
        return CastToInt(CastToInt(data['1.7.0']) - CastToInt(data['2.7.0']))


class Emlog(Powermeter):
    def __init__(self, ip, meterindex, json_power_calculate):
        self.ip = ip
        self.meterindex = meterindex
        self.json_power_calculate = json_power_calculate

    def GetPowermeterWatts(self):
        data = session.get(
            f'http://{self.ip}/pages/getinformation.php?heute&meterindex={self.meterindex}', timeout=10
        ).json()
        if not self.json_power_calculate:
            return CastToInt(data['Leistung170'])
        return CastToInt(data['Leistung170'] - data['Leistung270'])


class IoBroker(Powermeter):
    def __init__(self, ip, port, current_power_alias, power_calculate, power_input_alias, power_output_alias):
        self.ip = ip
        self.port = port
        self.current_power_alias = current_power_alias
        self.power_calculate = power_calculate
        self.power_input_alias = power_input_alias
        self.power_output_alias = power_output_alias

    def GetJson(self, path):
        return session.get(f'http://{self.ip}:{self.port}{path}', timeout=10).json()

    def GetPowermeterWatts(self):
        if not self.power_calculate:
            data = self.GetJson(f'/getBulk/{self.current_power_alias}')
            for item in data:
                if item['id'] == self.current_power_alias:
                    return CastToInt(item['val'])
        data = self.GetJson(f'/getBulk/{self.power_input_alias},{self.power_output_alias}')
        input_val = output_val = 0
        for item in data:
            if item['id'] == self.power_input_alias:
                input_val = CastToInt(item['val'])
            if item['id'] == self.power_output_alias:
                output_val = CastToInt(item['val'])
        return CastToInt(input_val - output_val)


class HomeAssistant(Powermeter):
    def __init__(self, ip, port, use_https, access_token, current_power_entity,
                 power_calculate, power_input_alias, power_output_alias):
        self.ip = ip
        self.port = port
        self.use_https = use_https
        self.access_token = access_token
        self.current_power_entity = current_power_entity
        self.power_calculate = power_calculate
        self.power_input_alias = power_input_alias
        self.power_output_alias = power_output_alias

    def GetJson(self, path):
        scheme = "https" if self.use_https else "http"
        headers = {"Authorization": f"Bearer {self.access_token}", "content-type": "application/json"}
        return session.get(f"{scheme}://{self.ip}:{self.port}{path}", headers=headers, timeout=10).json()

    def GetPowermeterWatts(self):
        if not self.power_calculate:
            return CastToInt(self.GetJson(f"/api/states/{self.current_power_entity}")['state'])
        input_val = CastToInt(self.GetJson(f"/api/states/{self.power_input_alias}")['state'])
        output_val = CastToInt(self.GetJson(f"/api/states/{self.power_output_alias}")['state'])
        return CastToInt(input_val - output_val)


class VZLogger(Powermeter):
    def __init__(self, ip, port, uuid):
        self.ip = ip
        self.port = port
        self.uuid = uuid

    def GetPowermeterWatts(self):
        data = session.get(f"http://{self.ip}:{self.port}/{self.uuid}", timeout=10).json()
        return CastToInt(data['data'][0]['tuples'][0][1])


class AmisReader(Powermeter):
    def __init__(self, ip):
        self.ip = ip

    def GetPowermeterWatts(self):
        return CastToInt(session.get(f'http://{self.ip}/rest', timeout=10).json()['saldo'])


class DebugReader(Powermeter):
    def GetPowermeterWatts(self):
        return CastToInt(input("Enter Powermeter Watts: "))


class MqttPowermeter(Powermeter):
    def __init__(self, broker, port, topic_incoming, json_path_incoming=None,
                 topic_outgoing=None, json_path_outgoing=None,
                 username=None, password=None):
        self.broker = broker
        self.port = port
        self.topic_incoming = topic_incoming
        self.json_path_incoming = json_path_incoming
        self.topic_outgoing = topic_outgoing
        self.json_path_outgoing = json_path_outgoing
        self.username = username
        self.password = password
        self.value_incoming = None
        self.value_outgoing = None

        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.username and self.password:
            self.client.username_pw_set(self.username, self.password)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.connect(self.broker, self.port)
        self.client.loop_start()

    def on_connect(self, client, userdata, flags, reason_code, properties):
        logger.info("MQTT connected with result code %s", reason_code)
        client.subscribe(self.topic_incoming)
        logger.info("MQTT subscribed to %s", self.topic_incoming)
        if self.topic_outgoing and self.topic_outgoing != self.topic_incoming:
            client.subscribe(self.topic_outgoing)
            logger.info("MQTT subscribed to %s", self.topic_outgoing)

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode()
        try:
            data = json.loads(payload)
            if msg.topic == self.topic_incoming:
                self.value_incoming = (
                    extract_json_value(data, self.json_path_incoming)
                    if self.json_path_incoming else int(float(payload))
                )
                logger.info('MQTT incoming power: %s Watt', self.value_incoming)
            elif msg.topic == self.topic_outgoing:
                self.value_outgoing = (
                    extract_json_value(data, self.json_path_outgoing)
                    if self.json_path_outgoing else int(float(payload))
                )
                logger.info('MQTT outgoing power: %s Watt', self.value_outgoing)
        except json.JSONDecodeError:
            logger.error("MQTT: Failed to decode JSON payload")

    def wait_for_message(self, message_type, timeout=5):
        start = time.time()
        while True:
            if message_type == "incoming" and self.value_incoming is not None:
                break
            if message_type == "outgoing" and self.value_outgoing is not None:
                break
            if time.time() - start > timeout:
                raise TimeoutError(f"Timeout waiting for MQTT {message_type} message")
            time.sleep(1)

    def GetPowermeterWatts(self):
        if self.value_incoming is None:
            self.wait_for_message("incoming")
        if self.topic_outgoing and self.value_outgoing is None:
            self.wait_for_message("outgoing")
        return self.value_incoming - (self.value_outgoing if self.value_outgoing is not None else 0)


# ---------------------------------------------------------------------------
# DTU base class + implementations
# ---------------------------------------------------------------------------

class DTU(Powermeter):
    def __init__(self, inverter_count):
        self.inverter_count = inverter_count

    def GetACPower(self, pInverterId):
        raise NotImplementedError()

    def GetPowermeterWatts(self):
        return sum(
            self.GetACPower(i) for i in range(self.inverter_count)
            if AVAILABLE[i] and HOY_BATTERY_GOOD_VOLTAGE[i]
        )

    def CheckMinVersion(self): raise NotImplementedError()
    def GetAvailable(self, pInverterId): raise NotImplementedError()
    def GetActualLimitInW(self, pInverterId): raise NotImplementedError()
    def GetInfo(self, pInverterId): raise NotImplementedError()
    def GetTemperature(self, pInverterId): raise NotImplementedError()
    def GetPanelMinVoltage(self, pInverterId): raise NotImplementedError()
    def WaitForAck(self, pInverterId, pTimeoutInS): raise NotImplementedError()
    def SetLimit(self, pInverterId, pLimit): raise NotImplementedError()
    def SetPowerStatus(self, pInverterId, pActive): raise NotImplementedError()


class AhoyDTU(DTU):
    def __init__(self, inverter_count, ip, password):
        super().__init__(inverter_count)
        self.ip = ip
        self.password = password
        self.Token = ''

    def GetJson(self, path):
        data = None
        for _ in range(3):
            data = session.get(f'http://{self.ip}{path}', timeout=10).json()
            if data is not None:
                break
        return data

    def GetResponseJson(self, path, obj):
        return session.post(f'http://{self.ip}{path}', json=obj, timeout=10).json()

    def GetACPower(self, pInverterId):
        live = self.GetJson('/api/live')
        idx = live["ch0_fld_names"].index("P_AC")
        inv = self.GetJson(f'/api/inverter/id/{pInverterId}')
        return CastToInt(inv["ch"][0][idx])

    def CheckMinVersion(self):
        MinVersion = '0.8.80'
        data = self.GetJson('/api/system')
        try:
            ver = str(data["version"])
        except Exception:
            ver = str(data["generic"]["version"])
        logger.info('Ahoy: Current Version: %s', ver)
        if version.parse(ver) < version.parse(MinVersion):
            logger.error('Error: AHOY version too old! Minimum: %s', MinVersion)
            quit()

    def GetAvailable(self, pInverterId):
        data = self.GetJson('/api/index')
        available = bool(data["inverter"][pInverterId]["is_avail"])
        logger.info('Ahoy: Inverter "%s" Available: %s', NAME[pInverterId], available)
        return available

    def GetActualLimitInW(self, pInverterId):
        data = self.GetJson(f'/api/inverter/id/{pInverterId}')
        return HOY_INVERTER_WATT[pInverterId] * float(data['power_limit_read']) / 100

    def GetInfo(self, pInverterId):
        live = self.GetJson('/api/live')
        temp_idx = live["ch0_fld_names"].index("Temp")
        inv = self.GetJson(f'/api/inverter/id/{pInverterId}')
        SERIAL_NUMBER[pInverterId] = str(inv['serial'])
        NAME[pInverterId] = str(inv['name'])
        TEMPERATURE[pInverterId] = str(inv["ch"][0][temp_idx]) + ' degC'
        logger.info('Ahoy: Inverter "%s" / S/N "%s" / temp %s',
                    NAME[pInverterId], SERIAL_NUMBER[pInverterId], TEMPERATURE[pInverterId])

    def GetTemperature(self, pInverterId):
        live = self.GetJson('/api/live')
        temp_idx = live["ch0_fld_names"].index("Temp")
        inv = self.GetJson(f'/api/inverter/id/{pInverterId}')
        TEMPERATURE[pInverterId] = str(inv["ch"][0][temp_idx]) + ' degC'
        logger.info('Ahoy: Inverter "%s" temperature: %s', NAME[pInverterId], TEMPERATURE[pInverterId])

    def GetPanelMinVoltage(self, pInverterId):
        live = self.GetJson('/api/live')
        vdc_idx = live["fld_names"].index("U_DC")
        inv = self.GetJson(f'/api/inverter/id/{pInverterId}')
        excluded = GetNumberArray(HOY_BATTERY_IGNORE_PANELS[pInverterId])
        voltages = [
            float(inv['ch'][i][vdc_idx])
            for i in range(1, len(inv['ch']))
            if i not in excluded
        ]
        minVdc = min((v for v in voltages if v > 5), default=0)
        HOY_PANEL_VOLTAGE_LIST[pInverterId].append(minVdc)
        if len(HOY_PANEL_VOLTAGE_LIST[pInverterId]) > 5:
            HOY_PANEL_VOLTAGE_LIST[pInverterId].pop(0)
        max_value = max(HOY_PANEL_VOLTAGE_LIST[pInverterId], default=0)
        logger.info('Lowest panel voltage inverter "%s": %s Volt', NAME[pInverterId], max_value)
        return max_value

    def WaitForAck(self, pInverterId, pTimeoutInS):
        try:
            ack = False   # FIX: initialize before loop
            timeout_start = time.time()
            while time.time() < timeout_start + pTimeoutInS:
                time.sleep(0.5)
                data = self.GetJson(f'/api/inverter/id/{pInverterId}')
                ack = bool(data['power_limit_ack'])
                if ack:
                    break
            if ack:
                logger.info('Ahoy: Inverter "%s": Limit acknowledged', NAME[pInverterId])
            else:
                logger.info('Ahoy: Inverter "%s": Limit timeout!', NAME[pInverterId])
            return ack
        except Exception as e:
            logger.error('Ahoy: Inverter "%s" WaitForAck: "%s"',
                         NAME[pInverterId], e.message if hasattr(e, 'message') else e)
            return False

    def SetLimit(self, pInverterId, pLimit):
        logger.info('Ahoy: Inverter "%s": %s W → %s W',
                    NAME[pInverterId], CastToInt(CURRENT_LIMIT[pInverterId]), CastToInt(pLimit))
        obj = {'cmd': 'limit_nonpersistent_absolute', 'val': pLimit, "id": pInverterId, "token": self.Token}
        resp = self.GetResponseJson('/api/ctrl', obj)
        if not resp["success"] and resp.get("error") == "ERR_PROTECTED":
            self.Authenticate()
            self.SetLimit(pInverterId, pLimit)
            return
        if not resp["success"]:
            raise Exception("Error: SetLimitAhoy Request error")
        CURRENT_LIMIT[pInverterId] = pLimit

    def SetPowerStatus(self, pInverterId, pActive):
        label = "on" if pActive else "off"
        logger.info('Ahoy: Inverter "%s": Turn %s', NAME[pInverterId], label)
        obj = {'cmd': 'power', 'val': CastToInt(pActive is True), "id": pInverterId, "token": self.Token}
        resp = self.GetResponseJson('/api/ctrl', obj)
        if not resp["success"] and resp.get("error") == "ERR_PROTECTED":
            self.Authenticate()
            self.SetPowerStatus(pInverterId, pActive)
            return
        if not resp["success"]:
            raise Exception("Error: SetPowerStatus Request error")

    def Authenticate(self):
        logger.info('Ahoy: Authenticating...')
        resp = self.GetResponseJson('/api/ctrl', {'auth': self.password})
        if not resp["success"]:
            raise Exception("Error: Authenticate Request error")
        self.Token = resp["token"]
        logger.info('Ahoy: Token received: %s', self.Token)


class OpenDTU(DTU):
    def __init__(self, inverter_count, ip, user, password):
        super().__init__(inverter_count)
        self.ip = ip
        self.user = user
        self.password = password

    def GetJson(self, path):
        return session.get(
            f'http://{self.ip}{path}',
            auth=HTTPBasicAuth(self.user, self.password),
            timeout=10
        ).json()

    def GetResponseJson(self, path, sendStr):
        return session.post(
            f'http://{self.ip}{path}',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            data=sendStr,
            auth=HTTPBasicAuth(self.user, self.password),
            timeout=10
        ).json()

    def GetACPower(self, pInverterId):
        data = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        return CastToInt(data['inverters'][0]['AC']['0']['Power']['v'])

    def CheckMinVersion(self):
        MinVersion = 'v24.2.12'
        data = self.GetJson('/api/system/status')
        ver = str(data["git_hash"]).replace("-Database", "")
        logger.info('OpenDTU: Current Version: %s', ver)
        if version.parse(ver) < version.parse(MinVersion):
            logger.error('Error: OpenDTU version too old! Minimum: %s', MinVersion)
            quit()

    def GetAvailable(self, pInverterId):
        data = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        reachable = bool(data['inverters'][0]["reachable"])
        logger.info('OpenDTU: Inverter "%s" reachable: %s', NAME[pInverterId], reachable)
        return reachable

    def GetActualLimitInW(self, pInverterId):
        data = self.GetJson('/api/limit/status')
        return HOY_INVERTER_WATT[pInverterId] * float(data[SERIAL_NUMBER[pInverterId]]['limit_relative']) / 100

    def GetInfo(self, pInverterId):
        if SERIAL_NUMBER[pInverterId] == '':
            data = self.GetJson('/api/livedata/status')
            SERIAL_NUMBER[pInverterId] = str(data['inverters'][pInverterId]['serial'])
        data = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        TEMPERATURE[pInverterId] = str(round(float(data['inverters'][0]['INV']['0']['Temperature']['v']), 1)) + ' degC'
        NAME[pInverterId] = str(data['inverters'][0]['name'])
        logger.info('OpenDTU: Inverter "%s" / S/N "%s" / temp %s',
                    NAME[pInverterId], SERIAL_NUMBER[pInverterId], TEMPERATURE[pInverterId])

    def GetTemperature(self, pInverterId):
        data = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        TEMPERATURE[pInverterId] = str(round(float(data['inverters'][0]['INV']['0']['Temperature']['v']), 1)) + ' degC'
        logger.info('OpenDTU: Inverter "%s" temperature: %s', NAME[pInverterId], TEMPERATURE[pInverterId])

    def GetPanelMinVoltage(self, pInverterId):
        data = self.GetJson(f'/api/livedata/status?inv={SERIAL_NUMBER[pInverterId]}')
        excluded = GetNumberArray(HOY_BATTERY_IGNORE_PANELS[pInverterId])
        dc = data['inverters'][0]['DC']
        voltages = [
            float(dc[str(i)]['Voltage']['v'])
            for i in range(len(dc))
            if i not in excluded
        ]
        minVdc = min((v for v in voltages if v > 5), default=0)
        HOY_PANEL_VOLTAGE_LIST[pInverterId].append(minVdc)
        if len(HOY_PANEL_VOLTAGE_LIST[pInverterId]) > 5:
            HOY_PANEL_VOLTAGE_LIST[pInverterId].pop(0)
        return max(HOY_PANEL_VOLTAGE_LIST[pInverterId], default=0)

    def WaitForAck(self, pInverterId, pTimeoutInS):
        try:
            ack = False   # FIX: initialize before loop
            timeout_start = time.time()
            while time.time() < timeout_start + pTimeoutInS:
                time.sleep(0.5)
                data = self.GetJson('/api/limit/status')
                ack = (data[SERIAL_NUMBER[pInverterId]]['limit_set_status'] == 'Ok')
                if ack:
                    break
            if ack:
                logger.info('OpenDTU: Inverter "%s": Limit acknowledged', NAME[pInverterId])
            else:
                logger.info('OpenDTU: Inverter "%s": Limit timeout!', NAME[pInverterId])
            return ack
        except Exception as e:
            logger.error('OpenDTU: Inverter "%s" WaitForAck: "%s"',
                         NAME[pInverterId], e.message if hasattr(e, 'message') else e)
            return False

    def SetLimit(self, pInverterId, pLimit):
        logger.info('OpenDTU: Inverter "%s": %s W → %s W',
                    NAME[pInverterId], CastToInt(CURRENT_LIMIT[pInverterId]), CastToInt(pLimit))
        rel = CastToInt(pLimit / HOY_INVERTER_WATT[pInverterId] * 100)
        sendStr = f'data={{"serial":"{SERIAL_NUMBER[pInverterId]}", "limit_type":1, "limit_value":{rel}}}'
        resp = self.GetResponseJson('/api/limit/config', sendStr)
        if resp['type'] != 'success':
            raise Exception(f"Error: SetLimit error: {resp['message']}")
        CURRENT_LIMIT[pInverterId] = pLimit

    def SetPowerStatus(self, pInverterId, pActive):
        label = "on" if pActive else "off"
        logger.info('OpenDTU: Inverter "%s": Turn %s', NAME[pInverterId], label)
        sendStr = f'data={{"serial":"{SERIAL_NUMBER[pInverterId]}", "power":{json.dumps(pActive)}}}'
        resp = self.GetResponseJson('/api/power/config', sendStr)
        if resp['type'] != 'success':
            raise Exception(f"Error: SetPowerStatus error: {resp['message']}")


class DebugDTU(DTU):
    def __init__(self, inverter_count):
        super().__init__(inverter_count)

    def GetACPower(self, pInverterId):
        return CastToInt(input("Current AC-Power: "))

    def CheckMinVersion(self): pass

    def GetAvailable(self, pInverterId):
        logger.info('Debug: Inverter "%s" Available: True', NAME[pInverterId])
        return True

    def GetActualLimitInW(self, pInverterId):
        return CastToInt(input("Current InverterLimit: "))

    def GetInfo(self, pInverterId):
        SERIAL_NUMBER[pInverterId] = str(pInverterId)
        NAME[pInverterId] = str(pInverterId)
        TEMPERATURE[pInverterId] = '0 degC'
        logger.info('Debug: Inverter "%s" / S/N "%s" / temp %s',
                    NAME[pInverterId], SERIAL_NUMBER[pInverterId], TEMPERATURE[pInverterId])

    def GetTemperature(self, pInverterId):
        TEMPERATURE[pInverterId] = 0
        logger.info('Debug: Inverter "%s" temperature: 0', NAME[pInverterId])

    def GetPanelMinVoltage(self, pInverterId):
        logger.info('Lowest panel voltage inverter "%s": 90 Volt', NAME[pInverterId])
        return 90

    def WaitForAck(self, pInverterId, pTimeoutInS):
        return True

    def SetLimit(self, pInverterId, pLimit):
        logger.info('Debug: Inverter "%s": %s W → %s W',
                    NAME[pInverterId], CastToInt(CURRENT_LIMIT[pInverterId]), CastToInt(pLimit))
        CURRENT_LIMIT[pInverterId] = pLimit

    def SetPowerStatus(self, pInverterId, pActive):
        logger.info('Debug: Inverter "%s": Turn %s', NAME[pInverterId], "on" if pActive else "off")


class Script(Powermeter):
    def __init__(self, file, ip, user, password):
        self.file = file
        self.ip = ip
        self.user = user
        self.password = password

    def GetPowermeterWatts(self):
        return CastToInt(subprocess.check_output([self.file, self.ip, self.user, self.password]))


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def CreatePowermeter() -> Powermeter:
    shelly_ip = config.get('SHELLY', 'SHELLY_IP')
    shelly_user = config.get('SHELLY', 'SHELLY_USER')
    shelly_pass = config.get('SHELLY', 'SHELLY_PASS')
    shelly_emeterindex = config.get('SHELLY', 'EMETER_INDEX')

    sel = 'SELECT_POWERMETER'
    if config.getboolean(sel, 'USE_SHELLY_EM'):
        return ShellyEM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean(sel, 'USE_SHELLY_3EM'):
        return Shelly3EM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean(sel, 'USE_SHELLY_3EM_PRO'):
        return Shelly3EMPro(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean(sel, 'USE_TASMOTA'):
        return Tasmota(
            config.get('TASMOTA', 'TASMOTA_IP'),
            config.get('TASMOTA', 'TASMOTA_USER'),
            config.get('TASMOTA', 'TASMOTA_PASS'),
            config.get('TASMOTA', 'TASMOTA_JSON_STATUS'),
            config.get('TASMOTA', 'TASMOTA_JSON_PAYLOAD_MQTT_PREFIX'),
            config.get('TASMOTA', 'TASMOTA_JSON_POWER_MQTT_LABEL'),
            config.get('TASMOTA', 'TASMOTA_JSON_POWER_INPUT_MQTT_LABEL'),
            config.get('TASMOTA', 'TASMOTA_JSON_POWER_OUTPUT_MQTT_LABEL'),
            config.getboolean('TASMOTA', 'TASMOTA_JSON_POWER_CALCULATE', fallback=False)
        )
    elif config.getboolean(sel, 'USE_SHRDZM'):
        return Shrdzm(config.get('SHRDZM', 'SHRDZM_IP'),
                      config.get('SHRDZM', 'SHRDZM_USER'),
                      config.get('SHRDZM', 'SHRDZM_PASS'))
    elif config.getboolean(sel, 'USE_EMLOG'):
        return Emlog(config.get('EMLOG', 'EMLOG_IP'),
                     config.get('EMLOG', 'EMLOG_METERINDEX'),
                     config.getboolean('EMLOG', 'EMLOG_JSON_POWER_CALCULATE', fallback=False))
    elif config.getboolean(sel, 'USE_IOBROKER'):
        return IoBroker(
            config.get('IOBROKER', 'IOBROKER_IP'),
            config.get('IOBROKER', 'IOBROKER_PORT'),
            config.get('IOBROKER', 'IOBROKER_CURRENT_POWER_ALIAS'),
            config.getboolean('IOBROKER', 'IOBROKER_POWER_CALCULATE'),
            config.get('IOBROKER', 'IOBROKER_POWER_INPUT_ALIAS'),
            config.get('IOBROKER', 'IOBROKER_POWER_OUTPUT_ALIAS')
        )
    elif config.getboolean(sel, 'USE_HOMEASSISTANT'):
        return HomeAssistant(
            config.get('HOMEASSISTANT', 'HA_IP'),
            config.get('HOMEASSISTANT', 'HA_PORT'),
            config.getboolean('HOMEASSISTANT', 'HA_HTTPS', fallback=False),
            config.get('HOMEASSISTANT', 'HA_ACCESSTOKEN'),
            config.get('HOMEASSISTANT', 'HA_CURRENT_POWER_ENTITY'),
            config.getboolean('HOMEASSISTANT', 'HA_POWER_CALCULATE'),
            config.get('HOMEASSISTANT', 'HA_POWER_INPUT_ALIAS'),
            config.get('HOMEASSISTANT', 'HA_POWER_OUTPUT_ALIAS')
        )
    elif config.getboolean(sel, 'USE_VZLOGGER'):
        return VZLogger(config.get('VZLOGGER', 'VZL_IP'),
                        config.get('VZLOGGER', 'VZL_PORT'),
                        config.get('VZLOGGER', 'VZL_UUID'))
    elif config.getboolean(sel, 'USE_SCRIPT'):
        return Script(config.get('SCRIPT', 'SCRIPT_FILE'),
                      config.get('SCRIPT', 'SCRIPT_IP'),
                      config.get('SCRIPT', 'SCRIPT_USER'),
                      config.get('SCRIPT', 'SCRIPT_PASS'))
    elif config.getboolean(sel, 'USE_AMIS_READER'):
        return AmisReader(config.get('AMIS_READER', 'AMIS_READER_IP'))
    elif config.getboolean(sel, 'USE_MQTT'):
        return MqttPowermeter(
            config.get('MQTT_POWERMETER', 'MQTT_BROKER',
                       fallback=config.get("MQTT_CONFIG", "MQTT_BROKER", fallback=None)),
            config.getint('MQTT_POWERMETER', 'MQTT_PORT',
                          fallback=config.getint("MQTT_CONFIG", "MQTT_PORT", fallback=1883)),
            config.get('MQTT_POWERMETER', 'MQTT_TOPIC_INCOMING'),
            config.get('MQTT_POWERMETER', 'MQTT_JSON_PATH_INCOMING', fallback=None),
            config.get('MQTT_POWERMETER', 'MQTT_TOPIC_OUTGOING', fallback=None),
            config.get('MQTT_POWERMETER', 'MQTT_JSON_PATH_OUTGOING', fallback=None),
            config.get('MQTT_POWERMETER', 'MQTT_USERNAME',
                       fallback=config.get('MQTT_CONFIG', 'MQTT_USERNAME', fallback=None)),
            config.get('MQTT_POWERMETER', 'MQTT_PASSWORD',
                       fallback=config.get('MQTT_CONFIG', 'MQTT_PASSWORD', fallback=None))
        )
    elif config.getboolean(sel, 'USE_DEBUG_READER'):
        return DebugReader()
    else:
        raise Exception("Error: no powermeter defined!")


def CreateIntermediatePowermeter(dtu: DTU) -> Powermeter:
    shelly_ip = config.get('INTERMEDIATE_SHELLY', 'SHELLY_IP_INTERMEDIATE')
    shelly_user = config.get('INTERMEDIATE_SHELLY', 'SHELLY_USER_INTERMEDIATE')
    shelly_pass = config.get('INTERMEDIATE_SHELLY', 'SHELLY_PASS_INTERMEDIATE')
    shelly_emeterindex = config.get('INTERMEDIATE_SHELLY', 'EMETER_INDEX')

    sel = 'SELECT_INTERMEDIATE_METER'
    if config.getboolean(sel, 'USE_TASMOTA_INTERMEDIATE'):
        return Tasmota(
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_USER_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_PASS_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_STATUS_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_PAYLOAD_MQTT_PREFIX_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_POWER_MQTT_LABEL_INTERMEDIATE'),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_POWER_INPUT_MQTT_LABEL_INTERMEDIATE', fallback=None),
            config.get('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_POWER_OUTPUT_MQTT_LABEL_INTERMEDIATE', fallback=None),
            config.getboolean('INTERMEDIATE_TASMOTA', 'TASMOTA_JSON_POWER_CALCULATE_INTERMEDIATE', fallback=False)
        )
    elif config.getboolean(sel, 'USE_SHELLY_EM_INTERMEDIATE'):
        return ShellyEM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean(sel, 'USE_SHELLY_3EM_INTERMEDIATE'):
        return Shelly3EM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean(sel, 'USE_SHELLY_3EM_PRO_INTERMEDIATE'):
        return Shelly3EMPro(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean(sel, 'USE_SHELLY_1PM_INTERMEDIATE'):
        return Shelly1PM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean(sel, 'USE_SHELLY_PLUS_1PM_INTERMEDIATE'):
        return ShellyPlus1PM(shelly_ip, shelly_user, shelly_pass, shelly_emeterindex)
    elif config.getboolean(sel, 'USE_ESPHOME_INTERMEDIATE'):
        return ESPHome(
            config.get('INTERMEDIATE_ESPHOME', 'ESPHOME_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_ESPHOME', 'ESPHOME_PORT_INTERMEDIATE', fallback='80'),
            config.get('INTERMEDIATE_ESPHOME', 'ESPHOME_DOMAIN_INTERMEDIATE'),
            config.get('INTERMEDIATE_ESPHOME', 'ESPHOME_ID_INTERMEDIATE')
        )
    elif config.getboolean(sel, 'USE_SHRDZM_INTERMEDIATE'):
        return Shrdzm(config.get('INTERMEDIATE_SHRDZM', 'SHRDZM_IP_INTERMEDIATE'),
                      config.get('INTERMEDIATE_SHRDZM', 'SHRDZM_USER_INTERMEDIATE'),
                      config.get('INTERMEDIATE_SHRDZM', 'SHRDZM_PASS_INTERMEDIATE'))
    elif config.getboolean(sel, 'USE_EMLOG_INTERMEDIATE'):
        return Emlog(config.get('INTERMEDIATE_EMLOG', 'EMLOG_IP_INTERMEDIATE'),
                     config.get('INTERMEDIATE_EMLOG', 'EMLOG_METERINDEX_INTERMEDIATE'),
                     config.getboolean('INTERMEDIATE_EMLOG', 'EMLOG_JSON_POWER_CALCULATE', fallback=False))
    elif config.getboolean(sel, 'USE_IOBROKER_INTERMEDIATE'):
        return IoBroker(
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_PORT_INTERMEDIATE'),
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_CURRENT_POWER_ALIAS_INTERMEDIATE'),
            config.getboolean('INTERMEDIATE_IOBROKER', 'IOBROKER_POWER_CALCULATE', fallback=False),
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_POWER_INPUT_ALIAS_INTERMEDIATE', fallback=None),
            config.get('INTERMEDIATE_IOBROKER', 'IOBROKER_POWER_OUTPUT_ALIAS_INTERMEDIATE', fallback=None)
        )
    elif config.getboolean(sel, 'USE_HOMEASSISTANT_INTERMEDIATE'):
        return HomeAssistant(
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_IP_INTERMEDIATE'),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_PORT_INTERMEDIATE'),
            config.getboolean('INTERMEDIATE_HOMEASSISTANT', 'HA_HTTPS_INTERMEDIATE', fallback=False),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_ACCESSTOKEN_INTERMEDIATE'),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_CURRENT_POWER_ENTITY_INTERMEDIATE'),
            config.getboolean('INTERMEDIATE_HOMEASSISTANT', 'HA_POWER_CALCULATE_INTERMEDIATE', fallback=False),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_POWER_INPUT_ALIAS_INTERMEDIATE', fallback=None),
            config.get('INTERMEDIATE_HOMEASSISTANT', 'HA_POWER_OUTPUT_ALIAS_INTERMEDIATE', fallback=None)
        )
    elif config.getboolean(sel, 'USE_VZLOGGER_INTERMEDIATE'):
        return VZLogger(config.get('INTERMEDIATE_VZLOGGER', 'VZL_IP_INTERMEDIATE'),
                        config.get('INTERMEDIATE_VZLOGGER', 'VZL_PORT_INTERMEDIATE'),
                        config.get('INTERMEDIATE_VZLOGGER', 'VZL_UUID_INTERMEDIATE'))
    elif config.getboolean(sel, 'USE_SCRIPT_INTERMEDIATE'):
        return Script(config.get('INTERMEDIATE_SCRIPT', 'SCRIPT_FILE_INTERMEDIATE'),
                      config.get('INTERMEDIATE_SCRIPT', 'SCRIPT_IP_INTERMEDIATE'),
                      config.get('INTERMEDIATE_SCRIPT', 'SCRIPT_USER_INTERMEDIATE'),
                      config.get('INTERMEDIATE_SCRIPT', 'SCRIPT_PASS_INTERMEDIATE'))
    elif config.getboolean(sel, 'USE_MQTT_INTERMEDIATE'):
        return MqttPowermeter(
            config.get('INTERMEDIATE_MQTT', 'MQTT_BROKER',
                       fallback=config.get("MQTT_CONFIG", "MQTT_BROKER", fallback=None)),
            config.getint('INTERMEDIATE_MQTT', 'MQTT_PORT',
                          fallback=config.getint("MQTT_CONFIG", "MQTT_PORT", fallback=1883)),
            config.get('INTERMEDIATE_MQTT', 'MQTT_TOPIC_INCOMING'),
            config.get('INTERMEDIATE_MQTT', 'MQTT_JSON_PATH_INCOMING', fallback=None),
            config.get('INTERMEDIATE_MQTT', 'MQTT_TOPIC_OUTGOING', fallback=None),
            config.get('INTERMEDIATE_MQTT', 'MQTT_JSON_PATH_OUTGOING', fallback=None),
            config.get('INTERMEDIATE_MQTT', 'MQTT_USERNAME',
                       fallback=config.get("MQTT_CONFIG", "MQTT_USERNAME", fallback=None)),
            config.get('INTERMEDIATE_MQTT', 'MQTT_PASSWORD',
                       fallback=config.get("MQTT_CONFIG", "MQTT_PASSWORD", fallback=None))
        )
    elif config.getboolean(sel, 'USE_AMIS_READER_INTERMEDIATE'):
        return AmisReader(config.get('INTERMEDIATE_AMIS_READER', 'AMIS_READER_IP_INTERMEDIATE'))
    elif config.getboolean(sel, 'USE_DEBUG_READER_INTERMEDIATE'):
        return DebugReader()
    else:
        return dtu


def CreateDTU() -> DTU:
    inverter_count = config.getint('COMMON', 'INVERTER_COUNT')
    if config.getboolean('SELECT_DTU', 'USE_AHOY'):
        return AhoyDTU(inverter_count,
                       config.get('AHOY_DTU', 'AHOY_IP'),
                       config.get('AHOY_DTU', 'AHOY_PASS', fallback=''))
    elif config.getboolean('SELECT_DTU', 'USE_OPENDTU'):
        return OpenDTU(inverter_count,
                       config.get('OPEN_DTU', 'OPENDTU_IP'),
                       config.get('OPEN_DTU', 'OPENDTU_USER'),
                       config.get('OPEN_DTU', 'OPENDTU_PASS'))
    elif config.getboolean('SELECT_DTU', 'USE_DEBUG'):
        return DebugDTU(inverter_count)
    else:
        raise Exception("Error: no DTU defined!")


# ---------------------------------------------------------------------------
# Startup / global state init
# ---------------------------------------------------------------------------

logger.info("Author: %s / Script Version: %s", __author__, __version__)
logger.info("Config: %s", str(Path.joinpath(Path(__file__).parent.resolve(), "HoymilesZeroExport_Config.ini")))
if args.config:
    logger.info("Additional config: %s", args.config)

VERSION = config.get('VERSION', 'VERSION')
logger.info("Config file V %s", VERSION)

MAX_RETRIES = config.getint('COMMON', 'MAX_RETRIES', fallback=3)
RETRY_STATUS_CODES = config.get('COMMON', 'RETRY_STATUS_CODES', fallback='500,502,503,504')
RETRY_BACKOFF_FACTOR = config.getfloat('COMMON', 'RETRY_BACKOFF_FACTOR', fallback=0.1)
retry = Retry(
    total=MAX_RETRIES,
    backoff_factor=RETRY_BACKOFF_FACTOR,
    status_forcelist=[int(s) for s in RETRY_STATUS_CODES.split(',')],
    allowed_methods={"GET", "POST"}
)
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)

DTU = CreateDTU()
POWERMETER = CreatePowermeter()
INTERMEDIATE_POWERMETER = CreateIntermediatePowermeter(DTU)

INVERTER_COUNT = config.getint('COMMON', 'INVERTER_COUNT')
LOOP_INTERVAL_IN_SECONDS = config.getint('COMMON', 'LOOP_INTERVAL_IN_SECONDS')
SET_LIMIT_TIMEOUT_SECONDS = config.getint('COMMON', 'SET_LIMIT_TIMEOUT_SECONDS')
SET_POWER_STATUS_DELAY_IN_SECONDS = config.getint('COMMON', 'SET_POWER_STATUS_DELAY_IN_SECONDS')
POLL_INTERVAL_IN_SECONDS = config.getint('COMMON', 'POLL_INTERVAL_IN_SECONDS')
MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER = config.getint('COMMON', 'MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER')
SET_POWERSTATUS_CNT = config.getint('COMMON', 'SET_POWERSTATUS_CNT')
SLOW_APPROX_FACTOR_IN_PERCENT = config.getint('COMMON', 'SLOW_APPROX_FACTOR_IN_PERCENT')
LOG_TEMPERATURE = config.getboolean('COMMON', 'LOG_TEMPERATURE')
SET_INVERTER_TO_MIN_ON_POWERMETER_ERROR = config.getboolean('COMMON', 'SET_INVERTER_TO_MIN_ON_POWERMETER_ERROR', fallback=False)

SERIAL_NUMBER = []
ENABLED = []
NAME = []
TEMPERATURE = []
HOY_MAX_WATT = []
HOY_INVERTER_WATT = []
CURRENT_LIMIT = []
AVAILABLE = []
LASTLIMITACKNOWLEDGED = []
HOY_BATTERY_GOOD_VOLTAGE = []
HOY_COMPENSATE_WATT_FACTOR = []
HOY_BATTERY_MODE = []
HOY_BATTERY_THRESHOLD_OFF_LIMIT_IN_V = []
HOY_BATTERY_THRESHOLD_REDUCE_LIMIT_IN_V = []
HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V = []
HOY_BATTERY_THRESHOLD_ON_LIMIT_IN_V = []
HOY_BATTERY_IGNORE_PANELS = []
HOY_PANEL_VOLTAGE_LIST = []
HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST = []
HOY_BATTERY_AVERAGE_CNT = []

for i in range(INVERTER_COUNT):
    SERIAL_NUMBER.append(config.get(f'INVERTER_{i+1}', 'SERIAL_NUMBER', fallback=''))
    ENABLED.append(config.getboolean(f'INVERTER_{i+1}', 'ENABLED', fallback=True))
    NAME.append('yet unknown')
    TEMPERATURE.append('--- degC')
    HOY_MAX_WATT.append(config.getint(f'INVERTER_{i+1}', 'HOY_MAX_WATT'))
    inv_watt_raw = config.get(f'INVERTER_{i+1}', 'HOY_INVERTER_WATT')
    HOY_INVERTER_WATT.append(config.getint(f'INVERTER_{i+1}', 'HOY_INVERTER_WATT') if inv_watt_raw else HOY_MAX_WATT[i])
    CURRENT_LIMIT.append(-1)
    AVAILABLE.append(False)
    LASTLIMITACKNOWLEDGED.append(False)
    HOY_BATTERY_GOOD_VOLTAGE.append(True)
    HOY_BATTERY_MODE.append(config.getboolean(f'INVERTER_{i+1}', 'HOY_BATTERY_MODE'))
    HOY_BATTERY_THRESHOLD_OFF_LIMIT_IN_V.append(config.getfloat(f'INVERTER_{i+1}', 'HOY_BATTERY_THRESHOLD_OFF_LIMIT_IN_V'))
    HOY_BATTERY_THRESHOLD_REDUCE_LIMIT_IN_V.append(config.getfloat(f'INVERTER_{i+1}', 'HOY_BATTERY_THRESHOLD_REDUCE_LIMIT_IN_V'))
    HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V.append(config.getfloat(f'INVERTER_{i+1}', 'HOY_BATTERY_THRESHOLD_NORMAL_LIMIT_IN_V'))
    HOY_BATTERY_THRESHOLD_ON_LIMIT_IN_V.append(config.getfloat(f'INVERTER_{i+1}', 'HOY_BATTERY_THRESHOLD_ON_LIMIT_IN_V'))
    HOY_COMPENSATE_WATT_FACTOR.append(config.getfloat(f'INVERTER_{i+1}', 'HOY_COMPENSATE_WATT_FACTOR'))
    HOY_BATTERY_IGNORE_PANELS.append(config.get(f'INVERTER_{i+1}', 'HOY_BATTERY_IGNORE_PANELS'))
    HOY_PANEL_VOLTAGE_LIST.append([])
    HOY_PANEL_MIN_VOLTAGE_HISTORY_LIST.append([])
    HOY_BATTERY_AVERAGE_CNT.append(config.getint(f'INVERTER_{i+1}', 'HOY_BATTERY_AVERAGE_CNT', fallback=1))

SLOW_APPROX_LIMIT = CastToInt(
    GetMaxWattFromAllInverters() * config.getint('COMMON', 'SLOW_APPROX_LIMIT_IN_PERCENT') / 100
)

CONFIG_PROVIDER = ConfigFileConfigProvider(config)
MQTT = None
if config.has_section("MQTT_CONFIG"):
    broker = config.get("MQTT_CONFIG", "MQTT_BROKER")
    port = config.getint("MQTT_CONFIG", "MQTT_PORT", fallback=1883)
    client_id = config.get("MQTT_CONFIG", "MQTT_CLIENT_ID", fallback="HoymilesZeroExport")
    username = config.get("MQTT_CONFIG", "MQTT_USERNAME", fallback=None)
    password = config.get("MQTT_CONFIG", "MQTT_PASSWORD", fallback=None)
    topic_prefix = config.get("MQTT_CONFIG", "MQTT_SET_TOPIC", fallback="zeropower")
    log_level_val = config.get("MQTT_CONFIG", "MQTT_LOG_LEVEL", fallback=None)
    mqtt_log_level = logging.getLevelName(log_level_val) if log_level_val else None
    MQTT = MqttHandler(broker, port, client_id, username, password, topic_prefix, mqtt_log_level)

    if mqtt_log_level is not None:
        class MqttLogHandler(logging.Handler):
            def emit(self, record):
                MQTT.publish_log_record(record)
        logger.addHandler(MqttLogHandler())

    CONFIG_PROVIDER = ConfigProviderChain([MQTT, CONFIG_PROVIDER])


# ---------------------------------------------------------------------------
# Init sequence
# ---------------------------------------------------------------------------

try:
    logger.info("---Init---")
    newLimitSetpoint = 0
    DTU.CheckMinVersion()
    if GetHoymilesAvailable():
        for i in range(INVERTER_COUNT):
            SetHoymilesPowerStatus(i, True)
        newLimitSetpoint = GetMinWattFromAllInverters()
        SetLimit(newLimitSetpoint)
        GetHoymilesActualPower()
        GetCheckBattery()
    GetPowermeterWatts()
except Exception as e:
    logger.error(e.message if hasattr(e, 'message') else e)
    time.sleep(LOOP_INTERVAL_IN_SECONDS)

logger.info("---Start Zero Export---")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

while True:
    CONFIG_PROVIDER.update()
    PublishConfigState()

    on_grid_usage_jump_to_limit_percent = CONFIG_PROVIDER.on_grid_usage_jump_to_limit_percent()
    on_grid_feed_fast_limit_decrease = CONFIG_PROVIDER.on_grid_feed_fast_limit_decrease()
    powermeter_target_point = CONFIG_PROVIDER.get_powermeter_target_point()
    powermeter_max_point = CONFIG_PROVIDER.get_powermeter_max_point()
    powermeter_min_point = CONFIG_PROVIDER.get_powermeter_min_point()
    powermeter_tolerance = CONFIG_PROVIDER.get_powermeter_tolerance()

    if powermeter_max_point < (powermeter_target_point + powermeter_tolerance):
        powermeter_max_point = powermeter_target_point + powermeter_tolerance + 50
        logger.info(
            'Warning: POWERMETER_MAX_POINT < TARGET + TOLERANCE. Auto-set to %s', powermeter_max_point
        )

    try:
        PreviousLimitSetpoint = newLimitSetpoint
        # FIX: initialize powermeterWatts so it's never stale from a previous cycle
        powermeterWatts = powermeter_target_point

        if GetHoymilesAvailable() and GetCheckBattery():
            if LOG_TEMPERATURE:
                GetHoymilesTemperature()

            for x in range(CastToInt(LOOP_INTERVAL_IN_SECONDS / POLL_INTERVAL_IN_SECONDS)):
                powermeterWatts = GetPowermeterWatts()

                if powermeterWatts > powermeter_max_point:
                    if on_grid_usage_jump_to_limit_percent > 0:
                        newLimitSetpoint = CastToInt(
                            GetMaxInverterWattFromAllInverters() * on_grid_usage_jump_to_limit_percent / 100
                        )
                        if (newLimitSetpoint <= PreviousLimitSetpoint) and (on_grid_usage_jump_to_limit_percent != 100):
                            newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    else:
                        newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    newLimitSetpoint = ApplyLimitsToSetpoint(newLimitSetpoint)
                    SetLimit(newLimitSetpoint)
                    remaining = CastToInt((LOOP_INTERVAL_IN_SECONDS / POLL_INTERVAL_IN_SECONDS - x) * POLL_INTERVAL_IN_SECONDS)
                    if remaining > 0:
                        time.sleep(remaining)
                    break

                elif (powermeterWatts < powermeter_min_point) and on_grid_feed_fast_limit_decrease:
                    newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    newLimitSetpoint = ApplyLimitsToSetpoint(newLimitSetpoint)
                    SetLimit(newLimitSetpoint)
                    remaining = CastToInt((LOOP_INTERVAL_IN_SECONDS / POLL_INTERVAL_IN_SECONDS - x) * POLL_INTERVAL_IN_SECONDS)
                    if remaining > 0:
                        time.sleep(remaining)
                    break

                else:
                    time.sleep(POLL_INTERVAL_IN_SECONDS)

            if MAX_DIFFERENCE_BETWEEN_LIMIT_AND_OUTPUTPOWER != 100:
                CutLimit = CutLimitToProduction(newLimitSetpoint)
                if CutLimit != newLimitSetpoint:
                    newLimitSetpoint = CutLimit
                    PreviousLimitSetpoint = newLimitSetpoint

            if powermeterWatts > powermeter_max_point:
                continue

            # producing too much → reduce limit
            if powermeterWatts < (powermeter_target_point - powermeter_tolerance):
                if PreviousLimitSetpoint >= GetMaxWattFromAllInverters():
                    hoymilesActualPower = GetHoymilesActualPower()
                    newLimitSetpoint = hoymilesActualPower + powermeterWatts - powermeter_target_point
                    LimitDifference = abs(hoymilesActualPower - newLimitSetpoint)
                    if LimitDifference > SLOW_APPROX_LIMIT:
                        newLimitSetpoint = newLimitSetpoint + (LimitDifference * SLOW_APPROX_FACTOR_IN_PERCENT / 100)
                    if newLimitSetpoint > hoymilesActualPower:
                        newLimitSetpoint = hoymilesActualPower
                    logger.info("overproducing: reduce limit based on actual power")
                else:
                    newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    LimitDifference = abs(PreviousLimitSetpoint - newLimitSetpoint)
                    if LimitDifference > SLOW_APPROX_LIMIT:
                        logger.info("overproducing: reduce limit by approximation")
                        newLimitSetpoint = newLimitSetpoint + (LimitDifference * SLOW_APPROX_FACTOR_IN_PERCENT / 100)
                    else:
                        logger.info("overproducing: reduce limit based on previous setpoint")

            # producing too little → increase limit
            elif powermeterWatts > (powermeter_target_point + powermeter_tolerance):
                if PreviousLimitSetpoint < GetMaxWattFromAllInverters():
                    newLimitSetpoint = PreviousLimitSetpoint + powermeterWatts - powermeter_target_point
                    logger.info("Not enough production: increasing limit")
                else:
                    logger.info("Not enough production: limit already at maximum")

            newLimitSetpoint = ApplyLimitsToSetpoint(newLimitSetpoint)
            SetLimit(newLimitSetpoint)

        else:
            if hasattr(SetLimit, "LastLimit"):
                SetLimit.LastLimit = -1
            time.sleep(LOOP_INTERVAL_IN_SECONDS)

    except Exception as e:
        logger.error(e.message if hasattr(e, 'message') else e)
        time.sleep(LOOP_INTERVAL_IN_SECONDS)