import argparse
import json
import os
import dataclasses
import sys
from getpass import getpass
from os import path, makedirs
from typing import Optional, List

from winix import (
    WinixAccount,
    WinixDevice,
    AirPurifierDevice,
    DehumidifierDevice,
    AirConditionerDevice,
    WinixDeviceStub
)
from winix.auth import WinixAuthResponse, login, refresh

DEFAULT_CONFIG_PATH = "~/.config/winix/config.json"


class UserError(Exception):
    pass


class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        return (
            dataclasses.asdict(o) if dataclasses.is_dataclass(o) else super().default(o)
        )


class Configuration:
    exists: bool

    cognito: Optional[WinixAuthResponse]
    devices: List[WinixDeviceStub]

    def __init__(self, config_path: str):
        self.config_path = path.expanduser(config_path)
        self._load_from_disk()

    def device(self, selector: str) -> WinixDeviceStub:
        try:
            return [
                d
                for i, d in enumerate(self.devices)
                if selector.lower() in (str(i), d.mac.lower(), d.alias.lower())
            ][0]
        except IndexError:
            raise UserError(
                f'Could not find device matching "{selector}"! '
                f"You can use Index, MAC, or Alias for the selector. "
                f"View the list of available devices with `winixctl devices`."
            )

    def _load_from_disk(self):
        if path.exists(self.config_path):
            with open(self.config_path) as f:
                js = json.load(f)
                self.exists = True
                self.cognito = (
                    WinixAuthResponse(**js["cognito"])
                    if js.get("cognito") is not None
                    else None
                )
                self.devices = [WinixDeviceStub(**d) for d in js.get("devices", [])]
        else:
            self.exists = False
            self.cognito = None
            self.devices = []

    def save(self):
        makedirs(path.dirname(self.config_path), mode=0o755, exist_ok=True)
        with open(os.open(self.config_path, os.O_CREAT | os.O_WRONLY, 0o755), "w") as f:
            f.truncate()
            js = json.dumps(
                {
                    "cognito": self.cognito,
                    "devices": self.devices,
                },
                cls=JSONEncoder,
            )
            f.write(js)


class Cmd:
    def __init__(self, args: argparse.Namespace, config: Configuration):
        self.args = args
        self.config = config

    def get_device_cls(self):
        device_config = self.config.device(self.args.device_selector)
        device_id = device_config.id
        device_type = device_config.product_group

        if "Air" in device_type:
            return AirPurifierDevice(device_id)
        elif "Deh" in device_type:
            return DehumidifierDevice(device_id)

        # TODO: check device_type name
        return AirConditionerDevice(device_id)


class LoginCmd(Cmd):
    parser_args = {
        "name": "login",
        "help": "Authenticate Winix account",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument("--username", help="Username (email)", required=False)
        parser.add_argument("--password", help="Password", required=False)
        parser.add_argument(
            "--refresh",
            dest="refresh",
            action="store_true",
            help="Refresh the Winix Cognito token instead of logging in",
        )

    def execute(self):
        if getattr(self.args, "refresh"):
            return self._refresh()
        else:
            return self._login()

    def _login(self):
        print(
            "You need to signup for a Winix account & associate your device in the phone app before using this."
        )
        username = getattr(self.args, "username") or input("Username (email): ")
        password = getattr(self.args, "password") or getpass("Password: ")

        self.config.cognito = login(username, password)
        account = WinixAccount(self.config.cognito.access_token)
        account.register_user(username)
        account.check_access_token()
        self.config.devices = account.get_device_info_list()
        self.config.save()
        print("Ok")

    def _refresh(self):
        self.config.cognito = refresh(
            user_id=self.config.cognito.user_id,
            refresh_token=self.config.cognito.refresh_token,
        )
        WinixAccount(self.config.cognito.access_token).check_access_token()
        self.config.save()
        print("Ok")


class DevicesCmd(Cmd):
    parser_args = {
        "name": "devices",
        "help": "List registered Winix devices",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument(
            "--expose", action="store_true", help="Expose sensitive Device ID string"
        )

    def execute(self):
        expose = getattr(self.args, "expose", False)
        print(f"{len(self.config.devices)} devices:")

        for i, device in enumerate(self.config.devices):
            hidden_deviceid = "_".join(
                [
                    device.id.split("_")[0],
                    "*" * len(device.id.split("_", 1)[1]),
                ]
            )

            fields = (
                ("Device ID", device.id if expose else hidden_deviceid + " (hidden)"),
                ("Mac", device.mac),
                ("Alias", device.alias),
                ("Location", device.location_code),
            )

            label = " (default)" if i == 0 else ""
            print(f"Device#{i}{label} ".ljust(50, "-"))

            for f, v in fields:
                print(f"{f:>15} : {v}")

            print("")

        print("Missing a device? You might need to run refresh.")


class FanCmd(Cmd):
    parser_args = {
        "name": "fan",
        "help": "Fan speed (airflow) controls [AIR, DEH]",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument(
            "level",
            help="Fan level",
            choices=["low", "medium", "high", "turbo", "sleep"],
        )

    def execute(self):
        # TODO(Hunter): Support getting the fan state instead of only being able to set it
        self.get_device_cls().control("airflow", self.args.level)
        print("ok")


class PowerCmd(Cmd):
    parser_args = {
        "name": "power",
        "help": "Power controls [AIR, DEH]",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument("state", help="Power state", choices=["on", "off"])

    def execute(self):
        self.get_device_cls().control(self.parser_args["name"], self.args.state)
        print("ok")


class ModeCmd(Cmd):
    parser_args = {
        "name": "mode",
        "help": "Mode controls [AIR, DEH]",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument(
            "state",
            help="Mode state",
            choices=[
                "auto",
                "manual",
                "clothes",
                "shoes",
                "quiet",
                "continuous"
            ]
        )

    def execute(self):
        self.get_device_cls().control(self.parser_args["name"], self.args.state)
        print("ok")


class PlasmawaveCmd(Cmd):
    parser_args = {
        "name": "plasmawave",
        "help": "Plasmawave controls [AIR]",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument("state", help="Plasmawave state", choices=["on", "off"])

    def execute(self):
        self.get_device_cls().control("plasma", self.args.state)
        print("ok")


class HumidityCmd(Cmd):
    parser_args = {
        "name": "humidity",
        "help": "Target humidity controls [DEH]",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument("percent", help="Target humidity in range 35~70%%",
                            type=int)

    def execute(self):
        self.get_device_cls().control(self.parser_args["name"], str(self.args.percent))
        print("ok")


class ChildLockCmd(Cmd):
    parser_args = {
        "name": "child-lock",
        "help": "Child lock controls [DEH]",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument("state", help="Child lock state", choices=["on", "off"])

    def execute(self):
        self.get_device_cls().control("child_lock", self.args.state)
        print("ok")


class UVSanitizeCmd(Cmd):
    parser_args = {
        "name": "uv-sanitize",
        "help": "UV sanitize controls [DEH]",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument("state", help="UV sanitize state", choices=["on", "off"])

    def execute(self):
        self.get_device_cls().control("uv_sanitize", self.args.state)
        print("ok")


class TimerCmd(Cmd):
    parser_args = {
        "name": "timer",
        "help": "timer controls [DEH]",
    }

    @classmethod
    def add_parser(cls, parser):
        parser.add_argument("hour", help="Set timer in range 1~12 hour",
                            type=int)

    def execute(self):
        self.get_device_cls().control(self.parser_args["name"], str(self.args.hour))
        print("ok")


class RefreshCmd(Cmd):
    parser_args = {
        "name": "refresh",
        "help": "Refresh account device metadata",
    }

    @classmethod
    def add_parser(cls, parser):
        pass

    def execute(self):
        account = WinixAccount(self.config.cognito.access_token)
        self.config.devices = account.get_device_info_list()
        self.config.save()
        print("Ok")


class StateCmd(Cmd):
    parser_args = {
        "name": "getstate",
        "help": "Get device state",
    }

    @classmethod
    def add_parser(cls, parser):
        pass

    def execute(self):
        status = self.get_device_cls().get_state()
        for f, v in status.items():
            print(f"{f:>16} : {v}")


def main():
    parser = argparse.ArgumentParser(description="Winix C545 Air Purifier Control")
    parser.add_argument(
        "--device",
        "-D",
        help="Device Index/Mac/Alias to use",
        default="0",
        dest="device_selector",
    )
    subparsers = parser.add_subparsers(dest="cmd")

    commands = {
        cls.parser_args["name"]: cls
        for cls in (
            LoginCmd,
            RefreshCmd,
            DevicesCmd,
            StateCmd,
            FanCmd,
            PowerCmd,
            ModeCmd,
            PlasmawaveCmd,
            HumidityCmd,
            ChildLockCmd,
            UVSanitizeCmd,
            TimerCmd,
        )
    }

    for cls in commands.values():
        sub = subparsers.add_parser(**cls.parser_args)
        cls.add_parser(sub)

    args = parser.parse_args()
    cmd = args.cmd

    if cmd is None:
        parser.print_help()
        return

    cls = commands[cmd]
    try:
        cls(args, config=Configuration(DEFAULT_CONFIG_PATH)).execute()
    except UserError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
