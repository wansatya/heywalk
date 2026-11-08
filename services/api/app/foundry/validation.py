"""MJCF validation stage.

Static checks run in-process (XML, mesh references, inertia, joints,
actuators). The authoritative compile check runs the actual MuJoCo engine in
a short-lived subprocess: the API process never imports ``mujoco``, and a
broken model can never take the server down. A model that compiles is still
only structurally valid; physical accuracy is reported separately.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..simulator.robot_description import (
    RobotPackageError,
    parse_mjcf,
    parse_xml,
    safe_relative_asset_path,
)

VALIDATION_VERSION = "foundry-validation-0.1"
DEFAULT_TIMEOUT_S = 30.0

_COMPILE_SCRIPT = r"""
import json
import sys

result = {"ok": False}
try:
    import mujoco

    model = mujoco.MjModel.from_xml_path(sys.argv[1])
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    result.update(
        ok=True,
        nbody=int(model.nbody),
        njnt=int(model.njnt),
        ngeom=int(model.ngeom),
        nq=int(model.nq),
        nv=int(model.nv),
        nu=int(model.nu),
        nmesh=int(model.nmesh),
    )
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(result))
"""


def _iter_bodies(worldbody: ET.Element):
    stack = list(worldbody.findall("body"))
    while stack:
        body = stack.pop()
        yield body
        stack.extend(body.findall("body"))


def _static_checks(
    root: Path, xml: str
) -> tuple[dict[str, bool], list[str], list[str], dict[str, Any]]:
    warnings: list[str] = []
    errors: list[str] = []
    checks = {
        "xml_valid": False,
        "mesh_files_valid": False,
        "joint_configuration": False,
        "inertia_configuration": False,
        "actuator_configuration": False,
        "mujoco_compilation": False,
    }
    details: dict[str, Any] = {}
    try:
        element = parse_xml(xml, "robot.xml")
        checks["xml_valid"] = element.tag == "mujoco"
        if not checks["xml_valid"]:
            errors.append(f"Root element is <{element.tag}>, expected <mujoco>.")
    except RobotPackageError as exc:
        errors.append(str(exc))
        return checks, warnings, errors, details

    # Mesh references: every <mesh file> must resolve inside the package.
    asset = element.find("asset")
    mesh_entries = asset.findall("mesh") if asset is not None else []
    missing: list[str] = []
    unreadable: list[str] = []
    for entry in mesh_entries:
        raw = entry.get("file") or ""
        try:
            relative = safe_relative_asset_path(raw, "mesh file")
        except RobotPackageError as exc:
            missing.append(str(exc))
            continue
        path = (root / relative).resolve()
        if root.resolve() not in path.parents and path != root.resolve():
            missing.append(f"{raw} escapes the package.")
            continue
        if not path.is_file():
            missing.append(raw)
            continue
        try:
            if path.stat().st_size == 0:
                unreadable.append(raw)
        except OSError:
            unreadable.append(raw)
    checks["mesh_files_valid"] = bool(mesh_entries) and not missing and not unreadable
    details["mesh_count"] = len(mesh_entries)
    details["missing_meshes"] = missing
    details["unreadable_meshes"] = unreadable
    if missing:
        errors.append(f"Missing mesh file(s): {', '.join(missing)}.")
    if unreadable:
        errors.append(f"Unreadable mesh file(s): {', '.join(unreadable)}.")

    # Simulator compatibility + joint/actuator contract.
    try:
        info = parse_mjcf(xml, "robot.xml")
        checks["joint_configuration"] = True
        checks["actuator_configuration"] = True
        details["joints"] = [joint.to_dict() for joint in info.joints]
        details["body_count"] = info.body_count
        details["geom_count"] = info.geom_count
        if info.notes:
            warnings.extend(info.notes)
    except RobotPackageError as exc:
        errors.append(str(exc))

    # Inertial properties: every non-world body needs positive mass/inertia.
    worldbody = element.find("worldbody")
    bodies = list(_iter_bodies(worldbody)) if worldbody is not None else []
    bad_inertia: list[str] = []
    for body in bodies:
        inertial = body.find("inertial")
        name = body.get("name") or "unnamed"
        if inertial is None:
            bad_inertia.append(f"{name} (no <inertial>)")
            continue
        try:
            mass = float(inertial.get("mass", "0"))
            diag = [float(value) for value in (inertial.get("diaginertia") or "").split()]
        except ValueError:
            bad_inertia.append(f"{name} (non-numeric)")
            continue
        if mass <= 0 or len(diag) != 3 or any(value <= 0 for value in diag):
            bad_inertia.append(f"{name} (non-positive)")
    checks["inertia_configuration"] = not bad_inertia
    details["inertia_issues"] = bad_inertia
    if bad_inertia:
        errors.append(f"Invalid inertial properties: {', '.join(bad_inertia)}.")

    # Estimated values are a warning, never silently treated as measured.
    warnings.append(
        "Mass and inertia values are estimates derived from visible"
        " geometry; they are not measured physical properties."
    )
    if len(bodies) > 1:
        warnings.append(
            "The articulated hierarchy is inferred from the reference image,"
            " not from the robot's real mechanism."
        )
    return checks, warnings, errors, details


def _compile_check(root: Path, model_path: Path, timeout_s: float) -> dict[str, Any]:
    env = dict(os.environ)
    env.update(
        {
            "MUJOCO_GL": "disable",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _COMPILE_SCRIPT, str(model_path)],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(5.0, timeout_s),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "MuJoCo compilation timed out."}
    except OSError as exc:
        return {"ok": False, "error": f"Could not start MuJoCo: {exc}"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return {
            "ok": False,
            "error": f"MuJoCo exited with {completed.returncode}: {detail[:1000]}",
        }
    try:
        payload = json.loads((completed.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": "MuJoCo returned unreadable output."}
    return payload if isinstance(payload, dict) else {"ok": False}


def validate_mjcf(
    root: Path,
    xml: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    model_filename: str = "robot.xml",
) -> dict[str, Any]:
    """Run all validation checks and return the spec-shaped report."""
    root = root.resolve()
    model_path = root / model_filename
    if not model_path.is_file():
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.write_text(xml, encoding="utf-8")
    checks, warnings, errors, details = _static_checks(root, xml)
    compile_result = _compile_check(root, model_path, timeout_s)
    checks["mujoco_compilation"] = bool(compile_result.get("ok"))
    details["mujoco"] = compile_result
    if not checks["mujoco_compilation"]:
        errors.append(
            "MuJoCo could not compile the model:"
            f" {compile_result.get('error', 'unknown error')}"
        )
    critical = all(checks.values())
    if not critical:
        status = "error"
    elif warnings:
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "details": details,
        "validator_version": VALIDATION_VERSION,
        "note": (
            "A compiling model is not necessarily physically accurate or"
            " dynamically stable. Validation results are kept separate from"
            " physical accuracy."
        ),
    }


__all__ = ["VALIDATION_VERSION", "validate_mjcf"]
