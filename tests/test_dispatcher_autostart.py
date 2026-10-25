"""Portable XML contract and platform boundary; actual registration is frozen Windows E2E."""

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from poster.dispatcher.autostart import configure, task_xml
from poster.dispatcher.models import DispatcherError


def test_logon_xml_is_current_user_least_privilege_and_quotes_unicode_paths():
    exe = Path("C:/Приложение с пробелом/PosterService.exe")
    root = Path("C:/User & test/Dispatcher")
    xml = task_xml(exe, root, "S-1-5-21-test")
    tree = ET.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert tree.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns) == "InteractiveToken"
    assert tree.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns) == "LeastPrivilege"
    assert tree.findtext("t:Actions/t:Exec/t:Command", namespaces=ns) == str(exe)
    assert tree.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns) == '--workspace "' + str(root) + '"'
    assert tree.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=ns) == "PT0S"
    assert "Password" not in xml


def test_source_never_modifies_windows_logon_settings(tmp_path):
    with pytest.raises(DispatcherError) as error:
        configure(tmp_path, enabled=True)
    assert error.value.code == "AUTOSTART_UNSUPPORTED"
