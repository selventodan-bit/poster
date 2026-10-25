"""Opt-in current-user Task Scheduler logon task. No Windows password or SCM."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET

from .launcher import command, process_options
from .models import DispatcherError
from .security import windows_sid

# Fixed code only; workspace, XML and task identity are data on stdin, never PowerShell source.
SCRIPT = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
$p=[Console]::In.ReadToEnd() | ConvertFrom-Json
$s=New-Object -ComObject Schedule.Service
$s.Connect()
$f=$s.GetFolder('\')
if ($p.operation -eq 'enable') {
  $null=$f.RegisterTask($p.name,$p.xml,6,$null,$null,3,$null)
}
$t=$null
foreach ($candidate in $f.GetTasks(1)) {
  if ($candidate.Name -eq $p.name) {$t=$candidate;break}
}
if ($p.operation -eq 'disable' -and $null -ne $t) {
  $f.DeleteTask($p.name,0)
  $t=$null
}
if ($null -eq $t) {
  @{enabled=$false;exists=$false;xml=''} | ConvertTo-Json -Compress
  exit 0
}
@{enabled=[bool]$t.Enabled;exists=$true;xml=[string]$t.Xml} | ConvertTo-Json -Compress
"""


def task_xml(executable, root, sid):
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    task = ET.Element(f"{{{ns}}}Task", version="1.2")

    def add(parent, name, text=None, **attrs):
        value = ET.SubElement(parent, f"{{{ns}}}{name}", attrs)
        value.text = text
        return value

    trigger = add(add(task, "Triggers"), "LogonTrigger")
    add(trigger, "Enabled", "true")
    add(trigger, "UserId", sid)
    principal = add(add(task, "Principals"), "Principal", id="CurrentUser")
    add(principal, "UserId", sid)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    for name, text in [
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("ExecutionTimeLimit", "PT0S"),
        ("Enabled", "true"),
    ]:
        add(settings, name, text)
    actions = add(task, "Actions", Context="CurrentUser")
    action = add(actions, "Exec")
    add(action, "Command", str(executable))
    add(action, "Arguments", subprocess.list2cmdline(["--workspace", str(root)]))
    add(action, "WorkingDirectory", str(root))
    return ET.tostring(task, encoding="unicode")


def configure(root, *, enabled=None):
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise DispatcherError("AUTOSTART_UNSUPPORTED")
    if enabled is not None and type(enabled) is not bool:
        raise DispatcherError("PARAMETERS_INVALID")
    executable = command("service")[0]  # Validates manifest and SHA256, no Python fallback.
    sid = windows_sid()
    name = "PosterDispatcher-" + hashlib.sha256((sid + str(root)).encode()).hexdigest()[:24]
    operation = "status" if enabled is None else "enable" if enabled else "disable"
    try:
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
            input=json.dumps(dict(name=name, operation=operation, xml=task_xml(executable, root, sid))),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            **process_options(),
        )
        if result.returncode:
            raise ValueError()
        value = json.loads(result.stdout.lstrip("\ufeff"))
        matches = False
        if value["exists"]:
            xml = ET.fromstring(value["xml"])
            ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
            actual = xml.findtext("t:Actions/t:Exec/t:Command", namespaces=ns)
            arguments = xml.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns)
            matches = actual == executable and arguments == subprocess.list2cmdline(
                ["--workspace", str(root)]
            )
        return dict(enabled=value["enabled"], exists=value["exists"], matches=matches, task=name)
    except Exception as exc:
        raise DispatcherError("AUTOSTART_FAILED") from exc
