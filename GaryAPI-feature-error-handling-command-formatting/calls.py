from requests import get, post
from config import base_conf
from argparse import ArgumentParser

parser = ArgumentParser()

parser.add_argument(
    "-c",
    "--commands",
    help="send a specific amount for command",
    nargs="*",
    required=False,
)
try:
    args = parser.parse_args()
    print("SENDING COMMANDS:\n", args.commands)
    ip_address = base_conf["ip_address"]
    commands = {"commands": args.commands}
    req = post(f"{ip_address}/api/commands", json=commands)
    print(req)
    print(str(req.content))
except Exception as e:
    print(e)
