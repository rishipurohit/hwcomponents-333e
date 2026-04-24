from logging import Logger
import math
import glob
import csv
import os
import fcntl
import subprocess
import re
from typing import Callable, Optional
from hwcomponents import ComponentModel, action
import csv
import hashlib
import yaml as _yaml


def _clean_tmp_dir():
    temp_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cacti_inputs_outputs"
    )
    os.makedirs(temp_dir, exist_ok=True)
    files = sorted(
        [f for f in glob.glob(os.path.join(temp_dir, "*")) if not f.endswith(".lock")],
        key=os.path.getctime,
        reverse=True,
    )
    if len(files) > 200:
        for file in files[200:]:
            try:
                os.remove(file)
            except OSError:
                pass
    return temp_dir


def _get_cacti_dir(logger: Logger) -> str:
    for to_try in ["cacti/cacti", "cacti"]:
        p = os.path.join(os.path.dirname(__file__), to_try)
        if os.path.exists(p) and os.path.isfile(p):
            return os.path.dirname(os.path.abspath(p))
    raise FileNotFoundError("CACTI executable not found")


def _get_msxac_dir(logger: Logger) -> str:
    """
    Locate the MemSysExplorer ArrayCharacterization directory (containing the `msxac`
    binary, `sample_configs/`, and `sample_cells/`). Checked in order:
      1. $MSX_AC_DIR
      2. A few likely sibling layouts relative to this file.
    """
    env = os.environ.get("MSX_AC_DIR")
    if env and os.path.isdir(env):
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "..", "MemSysExplorer", "tech", "ArrayCharacterization"),
        os.path.join(here, "..", "..", "..", "MemSysExplorer", "tech", "ArrayCharacterization"),
        os.path.join(here, "..", "MemSysExplorer", "tech", "ArrayCharacterization"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    raise FileNotFoundError(
        "Could not locate MemSysExplorer's ArrayCharacterization directory. "
        "Set the MSX_AC_DIR environment variable to point at it."
    )


class _MSXACMemory(ComponentModel):
    """
    Base class for memory models that call MemSysExplorer's ArrayCharacterization
    tool (`msxac`, the NVSim/DESTINY-extended characterizer) instead of DESTINY.

    DESTINY only supports 1T1C eDRAM cells, which is insufficient for modeling the
    3T eDRAM / 333-eDRAM devices targeted by this plug-in. msxac natively supports
    3T eDRAM cells with mixed-Vt, mixed-device-type peripherals / read path /
    write path configurations.

    The tool is invoked once per unique (cell, tech, capacity, ...) combination;
    a tech YAML is written into a temp subdirectory of the AC dir and the result
    YAML path is parsed out of msxac's stdout.
    """

    def __init__(
        self,
        tech_node: float,
        capacity_bytes: int,
        width_bits: int,
        cell_file: str,
        design_target: str = "cache",
        optimization_target: str = "WriteEDP",
        process_node: Optional[int] = None,
        device_roadmap: str = "HP",
        process_node_r: Optional[int] = None,
        device_roadmap_r: Optional[str] = None,
        process_node_w: Optional[int] = None,
        device_roadmap_w: Optional[str] = None,
        associativity: int = 8,
        temperature: int = 300,
        retention_time_us: int = 40,
    ):
        self.tech_node = float(tech_node) * 1e9  # nm
        self.capacity_bytes = capacity_bytes
        self.width_bits = width_bits
        self.cell_file = cell_file
        self.design_target = design_target
        self.optimization_target = optimization_target
        self.process_node = (
            int(process_node) if process_node is not None else max(7, int(self.tech_node))
        )
        self.device_roadmap = device_roadmap
        self.process_node_r = process_node_r
        self.device_roadmap_r = device_roadmap_r
        self.process_node_w = process_node_w
        self.device_roadmap_w = device_roadmap_w
        self.associativity = associativity
        self.temperature = temperature
        self.retention_time_us = retention_time_us

        self.read_energy: float = None
        self.write_energy: float = None
        self.destiny_leak_power: float = None
        self.destiny_area: float = None
        self.read_latency: float = None
        self.write_latency: float = None

        self._called_msxac = False
        self._call_msxac()

        self._customize_destiny_outputs()

        super().__init__(area=self.destiny_area, leak_power=self.destiny_leak_power)

    def _customize_destiny_outputs(self):
        """Hook for subclasses to tweak msxac outputs (e.g., monolithic 3D scaling)."""
        pass

    def _call_msxac(self):
        if self._called_msxac:
            return
        self._called_msxac = True

        ac_dir = _get_msxac_dir(self.logger)
        msxac_exec = os.path.join(ac_dir, "msxac")
        if not os.path.exists(msxac_exec):
            raise FileNotFoundError(
                f"msxac binary not found at {msxac_exec}. Build it with `make` in the "
                f"MemSysExplorer ArrayCharacterization directory, or set MSX_AC_DIR."
            )

        # Pick capacity unit that msxac accepts (KB or MB).
        capacity_kb = max(self.capacity_bytes // 1024, 1)
        if capacity_kb >= 1024 and capacity_kb % 1024 == 0:
            cap_value = int(capacity_kb // 1024)
            cap_unit = "MB"
        else:
            cap_value = int(capacity_kb)
            cap_unit = "KB"

        tech_cfg = {
            "MemoryCellInputFile": self.cell_file,
            "ProcessNode": self.process_node,
            "DeviceRoadmap": self.device_roadmap,
        }
        if self.process_node_r is not None:
            tech_cfg["ProcessNodeR"] = int(self.process_node_r)
            tech_cfg["DeviceRoadmapR"] = self.device_roadmap_r or "HP"
        if self.process_node_w is not None:
            tech_cfg["ProcessNodeW"] = int(self.process_node_w)
            tech_cfg["DeviceRoadmapW"] = self.device_roadmap_w or "HP"

        tech_cfg.update({
            "DesignTarget": self.design_target,
            "OptimizationTarget": self.optimization_target,
            "EnablePruning": "Yes",
            "Capacity": {"Value": cap_value, "Unit": cap_unit},
            "WordWidth": self.width_bits,
            "LocalWire": {
                "Type": "LocalAggressive",
                "RepeaterType": "RepeatedNone",
                "UseLowSwing": "No",
            },
            "GlobalWire": {
                "Type": "GlobalAggressive",
                "RepeaterType": "RepeatedNone",
                "UseLowSwing": "No",
            },
            "Routing": "H-tree",
            "InternalSensing": True,
            "Temperature": self.temperature,
            "RetentionTime": self.retention_time_us,
            "BufferDesignOptimization": "latency",
        })
        if self.design_target == "cache":
            tech_cfg["CacheAccessMode"] = "Normal"
            tech_cfg["Associativity"] = self.associativity

        # Hash a preliminary dump to get a deterministic output prefix.
        prelim = _yaml.safe_dump(tech_cfg, sort_keys=False)
        input_name = "hwc_" + hashlib.sha256(prelim.encode()).hexdigest()[:16]
        tech_cfg["OutputFilePrefix"] = input_name

        cfg_text = _yaml.safe_dump(tech_cfg, sort_keys=False)

        # Write the tech YAML under ac_dir so relative cell paths resolve correctly.
        tmp_dir = os.path.join(ac_dir, "hwc_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        tech_yaml_path = os.path.join(tmp_dir, input_name + ".yaml")
        log_path = os.path.join(tmp_dir, input_name + "_msxac.log")
        with open(tech_yaml_path, "w") as f:
            f.write(cfg_text)

        self.logger.info(f"Running msxac on {tech_yaml_path}")

        try:
            with open(log_path, "w") as out:
                rc = subprocess.call(
                    ["./msxac", tech_yaml_path],
                    cwd=ac_dir,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            with open(log_path, "r") as out:
                stdout = out.read()

            match = re.search(r"Results written to ([^\s]+\.yaml)", stdout)
            if not match:
                raise Exception(
                    f"msxac (rc={rc}) did not emit a result YAML path. "
                    f"See {log_path} for full output."
                )
            result_yaml = match.group(1)
            if not os.path.isabs(result_yaml):
                result_yaml = os.path.join(ac_dir, result_yaml)
            with open(result_yaml, "r") as f:
                result = _yaml.safe_load(f)

            if "CacheDesign" in result:
                cache = result["CacheDesign"]
                self.destiny_area = float(cache["Area"]["Total_mm2"]) * 1e-6  # mm^2 -> m^2
                self.read_latency = float(cache["Timing"]["CacheHitLatency_ns"]) * 1e-9
                self.write_latency = float(cache["Timing"]["CacheWriteLatency_ns"]) * 1e-9
                self.read_energy = float(cache["Power"]["CacheHitDynamicEnergy_nJ"]) * 1e-9
                self.write_energy = float(cache["Power"]["CacheWriteDynamicEnergy_nJ"]) * 1e-9
                self.destiny_leak_power = float(cache["Power"]["CacheTotalLeakagePower_mW"]) * 1e-3
            elif "Results" in result:
                res = result["Results"]
                self.destiny_area = float(res["Area"]["Total"]["Area_mm2"]) * 1e-6
                self.read_latency = float(res["Timing"]["Read"]["Latency_ns"]) * 1e-9
                self.read_energy = float(res["Power"]["Read"]["DynamicEnergy_pJ"]) * 1e-12
                self.destiny_leak_power = float(res["Power"]["Leakage_mW"]) * 1e-3
                if "Write" in res["Timing"]:
                    self.write_latency = float(res["Timing"]["Write"]["Latency_ns"]) * 1e-9
                    self.write_energy = float(res["Power"]["Write"]["DynamicEnergy_pJ"]) * 1e-12
                elif "Set" in res["Timing"]:
                    self.write_latency = float(res["Timing"]["Set"]["Latency_ns"]) * 1e-9
                    self.write_energy = float(res["Power"]["Set"]["DynamicEnergy_pJ"]) * 1e-12
            else:
                raise Exception(
                    f"Unrecognised msxac result YAML schema in {result_yaml}"
                )

            self.logger.info(
                f"msxac returned area={self.destiny_area} m^2, "
                f"read_lat={self.read_latency}s, write_lat={self.write_latency}s, "
                f"read_eng={self.read_energy}J, write_eng={self.write_energy}J, "
                f"leak={self.destiny_leak_power}W"
            )

        except Exception as e:
            self.logger.warning(f"Error calling msxac: {e}")
            for attr in (
                "read_energy",
                "write_energy",
                "destiny_leak_power",
                "destiny_area",
                "read_latency",
                "write_latency",
            ):
                if getattr(self, attr) is None:
                    setattr(self, attr, 0.0)


class _DestinyMemory(ComponentModel):
    """
    Base class for memory models utilizing DESTINY.
    """
    def __init__(
        self,
        tech_node: float,
        capacity_bytes: int,
        width_bits: int,
        design_target: str = "RAM",
        mem_cell_type: str = "eDRAM",
        cell_area_f2: float = 33.1,
        optimization_target: str = "WriteEDP",
        additional_cfg: str = "",
        additional_cell: str = ""
    ):
        self.tech_node = float(tech_node) * 1e9 # nm
        self.capacity_bytes = capacity_bytes
        self.width_bits = width_bits
        self.design_target = design_target
        self.mem_cell_type = mem_cell_type
        self.cell_area_f2 = cell_area_f2
        self.optimization_target = optimization_target
        self.additional_cfg = additional_cfg
        self.additional_cell = additional_cell
        
        self.read_energy: float = None
        self.write_energy: float = None
        self.destiny_leak_power: float = None
        self.destiny_area: float = None
        self.read_latency: float = None
        self.write_latency: float = None
        
        self._called_destiny = False
        self._call_destiny()
        
        self._customize_destiny_outputs()

        super().__init__(area=self.destiny_area, leak_power=self.destiny_leak_power)

    def _customize_destiny_outputs(self):
        pass

    def _call_destiny(self):
        if self._called_destiny:
            return
        self._called_destiny = True
        
        sn = min(max(int(self.tech_node), 22), 130)
        capacity_kb = max(self.capacity_bytes / 1024, 1)
        
        cfg_content = f"""
-CacheAccessMode: Normal
-Associativity (for cache only): 8

-DesignTarget: {self.design_target}
-ProcessNode: {sn}
-Capacity (KB): {int(capacity_kb)}
-WordWidth (bit): {self.width_bits}

-DeviceRoadmap: HP

-LocalWireType: LocalAggressive
-LocalWireRepeaterType: RepeatedNone
-LocalWireUseLowSwing: No

-GlobalWireType: GlobalAggressive
-GlobalWireRepeaterType: RepeatedNone
-GlobalWireUseLowSwing: No

-Routing: H-tree

-InternalSensing: true

-MemoryCellInputFile: temp_destiny.cell

-Temperature (K): 300
-RetentionTime (us): 40

-OptimizationTarget: WriteEDP

-EnablePruning: Yes

-BufferDesignOptimization: latency

-StackedDieCount: 1
{self.additional_cfg}
"""
        
        cell_content = f"""
-MemCellType: {self.mem_cell_type}
-CellArea (F^2): {self.cell_area_f2}

-CellAspectRatio: 2.39

-ReadMode: voltage

-CellAspectRatio: 2.39

-ReadMode: voltage

-AccessType: CMOS
-AccessCMOSWidth (F): 1.31

// value from paper
-DRAMCellCapacitance (F): 180e-18
-ResetVoltage (V): vdd
-SetVoltage (V): vdd

-MinSenseVoltage (mV): 10
"""
        temp_dir = _clean_tmp_dir()
        input_name = hashlib.sha256((cfg_content + cell_content).encode()).hexdigest()
        
        cfg_path = os.path.join(temp_dir, input_name + ".cfg")
        cell_path = os.path.join(temp_dir, "temp_destiny.cell")
        out_path = os.path.join(temp_dir, input_name + "_out.log")
        
        try:
            with open(cfg_path, "w") as f:
                f.write(cfg_content.strip() + "\n")
            with open(cell_path, "w") as f:
                f.write(cell_content.strip() + "\n")
                
            destiny_dir = os.path.join(os.path.dirname(__file__), "destiny_3d_cache")
            destiny_exec = os.path.join(destiny_dir, "destiny")
            
            self.logger.info(f"Running DESTINY: {destiny_exec} {input_name}.cfg")
            
            with open(out_path, "w") as out:
                result = subprocess.call(
                    [destiny_exec, input_name + ".cfg"],
                    cwd=temp_dir,
                    stdout=out,
                    stderr=subprocess.STDOUT
                )
            
            if not os.path.exists(out_path):
                raise Exception("DESTINY output file not found")
                
            with open(out_path, "r") as out:
                output_str = out.read()
                
            area_m = re.search(r"-\s*Total Area\s*=\s*([0-9.]+)(mm\^2|um\^2)", output_str)
            if area_m:
                val = float(area_m.group(1))
                unit = area_m.group(2)
                self.destiny_area = val * 1e-6 if unit == "mm^2" else val * 1e-12
            else:
                self.destiny_area = 0
            
            rlat_m = re.search(r"-\s*(?:Cache Hit|Read) Latency\s*=\s*([0-9.]+)(ns|ps)", output_str)
            if rlat_m:
                val = float(rlat_m.group(1))
                unit = rlat_m.group(2)
                self.read_latency = val * 1e-9 if unit == "ns" else val * 1e-12
            else:
                self.read_latency = 0
                
            wlat_m = re.search(r"-\s*(?:Cache Write|Write) Latency\s*=\s*([0-9.]+)(ns|ps)", output_str)
            if wlat_m:
                val = float(wlat_m.group(1))
                unit = wlat_m.group(2)
                self.write_latency = val * 1e-9 if unit == "ns" else val * 1e-12
            else:
                self.write_latency = 0
                
            reng_m = re.search(r"-\s*(?:Cache Hit|Read) Dynamic Energy\s*=\s*([0-9.]+)(nJ|pJ)", output_str)
            if reng_m:
                val = float(reng_m.group(1))
                unit = reng_m.group(2)
                self.read_energy = val * 1e-9 if unit == "nJ" else val * 1e-12
            else:
                self.read_energy = 0
                
            weng_m = re.search(r"-\s*(?:Cache Write|Write) Dynamic Energy\s*=\s*([0-9.]+)(nJ|pJ)", output_str)
            if weng_m:
                val = float(weng_m.group(1))
                unit = weng_m.group(2)
                self.write_energy = val * 1e-9 if unit == "nJ" else val * 1e-12
            else:
                self.write_energy = 0
                
            leak_m = re.search(r"-\s*(?:Cache Total |)Leakage Power\s*=\s*([0-9.]+)(mW|uW)", output_str)
            if leak_m:
                val = float(leak_m.group(1))
                unit = leak_m.group(2)
                self.destiny_leak_power = val * 1e-3 if unit == "mW" else val * 1e-6
            else:
                self.destiny_leak_power = 0

            self.logger.info(f"DESTINY returned read lat {self.read_latency}, write lat {self.write_latency}")
            self.logger.info(f"DESTINY returned read eng {self.read_energy}, write eng {self.write_energy}")
            self.logger.info(f"DESTINY returned leak {self.destiny_leak_power}, area {self.destiny_area}")
            
        except Exception as e:
            self.logger.warning(f"Error calling DESTINY: {e}")

class _DRAM(_DestinyMemory):
    priority = 0.3

    def __init__(
        self,
        size: int,
        width: int = 128,
        type: str = "DRAM",
        tech_node: float = 22e-9
    ):
        capacity_bytes = size // 8
        super().__init__(
            tech_node=tech_node,
            capacity_bytes=capacity_bytes,
            width_bits=width,
            design_target="RAM",
            mem_cell_type="DRAM",
            cell_area_f2=6.0,
            optimization_target="ReadLatency"
        )
        self.type = type
        self.width = width

    @action(bits_per_action="width")
    def read(self) -> tuple[float, float]:
        return self.read_energy, self.read_latency

    @action(bits_per_action="width")
    def write(self) -> tuple[float, float]:
        return self.write_energy, self.write_latency

class LPDDR4(_DRAM):
    component_name = ["DRAMLPDDR4", "DRAM_LPDDR4", "LPDDR4"]
    def __init__(self, size: int, width: int = 64):
        super().__init__(size=size, width=width, type="LPDDR4", tech_node=14e-9)
    def _customize_destiny_outputs(self):
        self.read_energy = 8 * 1e-12 * self.width_bits
        self.write_energy = self.read_energy
        self.read_latency = 1.0 / (50 * 1024 * 1024)
        self.write_latency = self.read_latency

class LPDDR(_DRAM):
    component_name = ["DRAMLPDDR", "DRAM_LPDDR", "LPDDR"]
    def __init__(self, size: int, width: int = 64):
        super().__init__(size=size, width=width, type="LPDDR", tech_node=22e-9)
    def _customize_destiny_outputs(self):
        self.read_energy = 40 * 1e-12 * self.width_bits
        self.write_energy = self.read_energy
        self.read_latency = 1.0 / (6.25 * 1024 * 1024)
        self.write_latency = self.read_latency

class DDR3(_DRAM):
    component_name = ["DRAMDDR3", "DRAM_DDR3", "DDR3"]
    def __init__(self, size: int, width: int = 64):
        super().__init__(size=size, width=width, type="DDR3", tech_node=22e-9)
    def _customize_destiny_outputs(self):
        self.read_energy = 70 * 1e-12 * self.width_bits
        self.write_energy = self.read_energy
        self.read_latency = 1.0 / (33.3 * 1024 * 1024)
        self.write_latency = self.read_latency

class HBM2(_DRAM):
    component_name = ["DRAMHBM2", "DRAM_HBM2", "HBM2"]
    def __init__(self, size: int, width: int = 2048):
        super().__init__(size=size, width=width, type="HBM2", tech_node=14e-9)
    def _customize_destiny_outputs(self):
        self.read_energy = 6.25 * 1e-12 * self.width_bits
        self.write_energy = self.read_energy
        self.read_latency = 1.0 / (2.4 * 1024 * 1024 * 1024)
        self.write_latency = self.read_latency

class HBM3(_DRAM):
    component_name = ["DRAMHBM3", "DRAM_HBM3", "HBM3"]
    def __init__(self, size: int, width: int = 2048):
        super().__init__(size=size, width=width, type="HBM3", tech_node=14e-9)
    def _customize_destiny_outputs(self):
        self.read_energy = 4.05 * 1e-12 * self.width_bits
        self.write_energy = self.read_energy
        self.read_latency = 1.0 / (6.4 * 8 * 1024 * 1024 * 1024)
        self.write_latency = self.read_latency

class HBM4(_DRAM):
    component_name = ["DRAMHBM4", "DRAM_HBM4", "HBM4"]
    def __init__(self, size: int, width: int = 4096):
        super().__init__(size=size, width=width, type="HBM4", tech_node=7e-9)
    def _customize_destiny_outputs(self):
        self.read_energy = 3.2 * 1e-12 * self.width_bits
        self.write_energy = self.read_energy
        self.read_latency = 1.0 / (8 * 8 * 1024 * 1024 * 1024)
        self.write_latency = self.read_latency



def _interp_call(
    logger: Logger,
    param_name: str,
    callfunc: Callable,
    param: float,
    param_lo: float,
    param_hi: float,
    interp_point_calculator: Callable = None,
    **kwargs,
):
    if param_lo == param_hi:
        return callfunc(param_lo, **kwargs)
    if interp_point_calculator is not None:
        interp_point = interp_point_calculator(param, param_lo, param_hi)
    else:
        interp_point = (param - param_lo) / (param_hi - param_lo)

    logger.info(f"Interpolating {param_name} between {param_lo} and {param_hi}.")

    return tuple(
        (1 - interp_point) * l + interp_point * h
        for l, h in zip(callfunc(param_lo, **kwargs), callfunc(param_hi, **kwargs))
    )


class _Memory(ComponentModel):
    """
    Base class for all memory models.
    """

    def __init__(
        self,
        cache_type: str,
        tech_node: float,  # Must be 22-180nm
        width: int | None = None,  # Must be >=32 < CHANGES BY NUMBER OF BANKS !?!? >
        depth: int | None = None,  # Must be >=64 < CHANGES BY NUMBER OF BANKS !?!? >
        size: int | None = None,
        n_rw_ports: int = 1,  # Must be power of 2, >=1
        n_banks=1,  # Must be power of 2, >=1
        associativity: int = 1,  # Weird stuff with this one
        tag_size: Optional[int] = None,
    ):
        if width is None:
            if size is None:
                raise ValueError("Either width or size must be provided.")
            if depth is None:
                width = max(16, math.ceil(math.sqrt(size)))
                self.logger.info(f"Calculated width: {width} from sqrt({size=})")
            else:
                width = size / depth
                self.logger.info(f"Calculated width: {width} from {size=} / {depth=}")

        # Size and width are now known
        depth = self.resolve_multiple_ways_to_calculate_value(
            "depth",
            ("depth", lambda depth: depth, {"depth": depth}),
            (
                "size / width",
                lambda size, width: size / width,
                {"size": size, "width": width},
            ),
        )

        assert math.isclose(
            size, depth * width, rel_tol=1e-6
        ), f"Size {size} != depth {depth} * width {width}"

        self.logger.info(f"Calculated depth: {depth}")

        self.cache_type = cache_type
        self.width = width  # self.assert_int("width", width)
        self.depth = depth  # self.assert_int("depth", depth)
        self.size = depth * width  # self.assert_int("size", self.depth * self.width)
        self.n_rw_ports = self.assert_int("n_rw_ports", n_rw_ports)
        self.n_banks = self.assert_int("n_banks", n_banks)
        self.associativity = self.assert_int("associativity", associativity)
        if self.associativity > 1:
            self.tag_size = self.assert_int("tag_size", tag_size)
        else:
            self.tag_size = 0

        self.tech_node = float(tech_node) * 1e9  # nm -> m

        self.read_energy: float = None
        self.write_energy: float = None
        self.cacti_leak_power: float = None
        self.cacti_area: float = None
        self.cycle_period: float = None
        self._called_cacti = False

        self._interpolate_and_call_cacti()

        super().__init__(leak_power=self.cacti_leak_power, area=self.cacti_area)

    def _get_latency_per_bit(self):
        return self.cycle_period / (self.width * self.n_rw_ports * self.n_banks)

    def log_bandwidth(self):
        bw = self.width * self.n_rw_ports * self.n_banks
        self.logger.info(f"Cache bandwidth: {bw/8} bytes/cycle")
        self.logger.info(f"Cache bandwidth: {bw/self.cycle_period} bits/second")

    def _interp_size(self, tech_node: float):
        # n_banks must be a power of two
        scaled_n_banks = 2 ** math.ceil(math.log2(self.n_banks))
        bankscale = self.n_banks / scaled_n_banks
        # Width must be >32, rounded to an 8
        scaled_width = max(math.ceil(self.width / 8) * 8, 32 * self.associativity)
        widthscale = self.width / scaled_width
        # Depth must be >64 * n_banks
        scaled_depth = max(self.depth, 64 * self.n_banks)
        depthscale = self.depth / scaled_depth

        (
            read_energy,
            write_energy,
            leak_power,
            area,
            cycle_period,
        ) = self._call_cacti(
            scaled_width * scaled_depth // 8,
            self.n_rw_ports,
            scaled_width // 8,
            tech_node / 1000,
            scaled_n_banks,
            self.tag_size,
            self.associativity,
        )

        # Found these empirically by testing different inputs with CACTI
        read_energy *= (widthscale * 0.7 + 0.3) * (depthscale ** (1.56 / 2))
        write_energy *= (widthscale * 0.7 + 0.3) * (depthscale ** (1.56 / 2))
        leak_power *= widthscale * depthscale
        area *= widthscale * depthscale
        cycle_period *= 1  # Dosn't scale strongly with anything
        return (
            read_energy,
            write_energy,
            leak_power,
            area,
            cycle_period,
        )

    def _interp_tech_node(self):
        supported_technologies = [22, 32, 45, 65, 90]
        # Interpolate. Below 16, interpolate energy with square root scaling (IDRS 2022),
        # area with linear scaling.
        # https://fuse.wikichip.org/news/7343/iedm-2022-did-we-just-witness-the-death-of-sram/
        if self.tech_node < min(supported_technologies) or self.tech_node > max(
            supported_technologies
        ):
            scale = self.tech_node / min(supported_technologies)
            (
                read_energy,
                write_energy,
                leak_power,
                area,
                cycle_period,
            ) = self._interp_size(min(supported_technologies))
            read_energy *= scale**0.5
            write_energy *= scale**0.5
            area *= scale
            cycle_period *= scale
            # B. Parvais et al., "The device architecture dilemma for CMOS
            # technologies: Opportunities & challenges of finFET over planar
            # MOSFET," 2009 International Symposium on VLSI tech_node, Systems,
            # and Applications, Hsinchu, Taiwan, 2009, pp. 80-81, doi:
            # 10.1109/VTSA.2009.5159300.
            # finfets have approx. 21% less leakage power
            if self.tech_node < min(supported_technologies):
                leak_power *= scale**0.5 * 0.79
            return (
                read_energy,
                write_energy,
                leak_power,
                area,
                cycle_period,
            )
        # Beyond 180nm, squared scaling
        if self.tech_node > max(supported_technologies):
            return tuple(
                v * (self.tech_node / max(supported_technologies)) ** 2
                for v in self._interp_size(max(supported_technologies))
            )

        else:
            return _interp_call(
                self.logger,
                "tech_node",
                self._interp_size,
                self.tech_node,
                max(s for s in supported_technologies if s <= self.tech_node),
                min(s for s in supported_technologies if s >= self.tech_node),
            )

    def _interpolate_and_call_cacti(self):
        if self._called_cacti:
            return
        self._called_cacti = True
        if self.read_energy is not None:
            self.log_bandwidth()
            return
        (
            self.read_energy,
            self.write_energy,
            self.cacti_leak_power,
            self.cacti_area,
            self.cycle_period,
        ) = self._interp_tech_node()

        self.logger.info(
            f"CACTI returned read energy {self.read_energy} for {self.width} bits"
        )
        self.logger.info(
            f"CACTI returned write energy {self.write_energy} for {self.width} bits"
        )
        self.logger.info(f"CACTI returned leak power {self.cacti_leak_power}")
        self.logger.info(f"CACTI returned area {self.cacti_area}")
        self.logger.info(f"CACTI returned cycle period {self.cycle_period}")

        self.log_bandwidth()

    def _call_cacti(
        self,
        cache_size: int,
        n_rw_ports: int,
        block_size: int,
        tech_node_um: float,
        n_banks: int,
        tag_size: int,
        associativity: int,
    ):
        self.logger.info(
            f"Calling CACTI with {cache_size=} {n_rw_ports=} {block_size=} "
            f"{tech_node_um=} {n_banks=} {tag_size=} {associativity=}"
        )

        cfg = open(
            os.path.join(os.path.dirname(__file__), "default_cfg.cfg")
        ).readlines()
        cfg.append(
            "\n############## User-Specified Hardware Attributes ##############\n"
        )
        cfg.append(f"-size (bytes) {cache_size}\n")
        cfg.append(f"-read-write port  {n_rw_ports}\n")
        cfg.append(f"-block size (bytes) {block_size}\n")
        cfg.append(f"-technology (u) {tech_node_um}\n")
        cfg.append(f"-output/input bus width  {block_size * 8}\n")
        cfg.append(f"-UCA bank {n_banks}\n")
        cfg.append(f'-cache type "{self.cache_type}"\n')
        cfg.append(f"-tag size (b) {tag_size}\n")
        cfg.append(f"-associativity {associativity}\n")

        # Generate a unique name for the input file using Python's hash function
        temp_dir = _clean_tmp_dir()
        input_name = hashlib.sha256("".join(cfg).encode()).hexdigest()
        input_path = os.path.join(temp_dir, input_name)
        output_path = os.path.join(temp_dir, input_name + "cacti.log")
        output_path_csv = os.path.join(temp_dir, input_name + ".out")
        lock_path = os.path.join(temp_dir, input_name + ".lock")

        def read_csv_results(output_path_csv):
            with open(output_path_csv, "r") as f:
                csv_results = csv.DictReader(f.readlines())
                row = list(csv_results)[-1]
                return (
                    float(row[" Dynamic read energy (nJ)"]) * 1e-9,
                    float(row[" Dynamic write energy (nJ)"]) * 1e-9,
                    float(row[" Standby leakage per bank(mW)"]) * 1e-3 * self.n_banks,
                    float(row[" Area (mm2)"]) * 1e-6,
                    float(row[" Random cycle time (ns)"]) * 1e-9,
                )

        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                if os.path.exists(output_path_csv):
                    try:
                        return read_csv_results(output_path_csv)
                    except Exception as e:
                        self.logger.warning(
                            f"Error reading CACTI output file {output_path_csv}: {e}"
                        )
                        pass

                with open(input_path, "w") as f:
                    f.write("".join(cfg))

                self.logger.info(f"Calling CACTI with input path {input_path}")
                self.logger.info(f"CACTI output will be written to {output_path}")

                cacti_dir = _get_cacti_dir(self.logger)

                exec_list = ["./cacti", "-infile", input_path]
                self.logger.info(
                    f"Calling: cd {cacti_dir} ; {' '.join(exec_list)} >> {output_path} 2>&1"
                )
                with open(output_path, "w") as out:
                    result = subprocess.call(
                        exec_list,
                        cwd=cacti_dir,
                        stdout=out,
                        stderr=subprocess.STDOUT,
                    )

                if result != 0 or not os.path.exists(output_path_csv):
                    raise Exception(
                        f"CACTI failed with exit code {result}. Please check {output_path} for CACTI output. "
                        f"Run command: cd {cacti_dir} ; {' '.join(exec_list)} >> {output_path} 2>&1"
                    )

                return read_csv_results(output_path_csv)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


class SRAM(_Memory):
    """
    SRAM model using CACTI.

    Parameters
    ----------
        tech_node: The technology node of the SRAM in meters.
        width: The width of the read and write ports in bits. This is the number of bits
            that are accssed by any one read/write. Total size = width * depth.
        depth: The number of entries in the SRAM, each with `width` bits. Total size =
            width * depth. Either this or depth must be provided, but not both.
        size: The total size of the SRAM in bits. If provided, depth will be calculated
            as size / width. Either this or depth must be provided, but not both.
        n_rw_ports: The number of read/write ports. Bandwidth will increase with more
            ports.
        n_banks: The number of banks. Bandwidth will increase with more banks.

    Attributes
    ----------
        component_name: set to "sram"
        priority: set to 0.8
        tech_node: The technology node of the SRAM in meters.
        width: The width of the read and write ports in bits. This is the number of bits
            that are accssed by any one read/write. Total size = width * depth.
        depth: The number of entries in the SRAM, each with `width` bits. Total size =
            width * depth.
        size: The total size of the SRAM in bits.
        n_rw_ports: The number of read/write ports. Bandwidth will increase with more
            ports.
        n_banks: The number of banks. Bandwidth will increase with more banks.
    """

    component_name = ["SRAM", "sram"]
    priority = 0.3

    def __init__(
        self,
        tech_node: float,
        width: int | None = None,
        depth: int | None = None,
        size: int | None = None,
        n_rw_ports: int = 1,
        n_banks=1,
    ):
        super().__init__(
            cache_type="ram",
            tech_node=tech_node,
            width=width,
            depth=depth,
            size=size,
            n_rw_ports=n_rw_ports,
            n_banks=n_banks,
        )

    @action(bits_per_action="width")
    def read(self) -> tuple[float, float]:
        """
        Returns the energy and latency for one SRAM read.

        Parameters
        ----------
        bits_per_action : int
            The number of bits to read.

        Returns
        -------
            (energy, latency): Tuple in (Joules, seconds).
        """
        self._interpolate_and_call_cacti()
        return self.read_energy, self._get_latency_per_bit() * self.width

    @action(bits_per_action="width")
    def write(self) -> tuple[float, float]:
        """
        Returns the energy and latency for one SRAM write.

        Parameters
        ----------
        bits_per_action : int
            The number of bits to write.

        Returns
        -------
            (energy, latency): Tuple in (Joules, seconds).
        """
        self._interpolate_and_call_cacti()
        return self.write_energy, self._get_latency_per_bit() * self.width


class Cache(_Memory):
    """
    Cache model using CACTI.

    Parameters
    ----------
        tech_node: float
            The technology node of the cache in meters.
        width: int
            The width of the read and write ports in bits. This is the number of bits
            that are accssed by any one read/write. Total size = width * depth.
        depth: int
            The number of entries in the cache, each with `width` bits. Total size =
            width * depth.
        size: The total size of the cache in bits. If provided, depth will be calculated
            as size / width. Either this or depth must be provided, but not both.
        n_rw_ports: int
            The number of read/write ports. Bandwidth will increase with more ports.
        n_banks: int
            The number of banks. Bandwidth will increase with more banks.
        associativity: int
            The associativity of the cache. This is the number of sets in the cache.
            Bandwidth will increase with more associativity.
        tag_size: int
            The number of bits of the tag used to check for cache misses and hits.

    Attributes
    ----------
        component_name: str
            set to "cache"
        priority: float
            set to 0.8
        tech_node: float
            The technology node of the cache in meters.
        width: int
            The width of the read and write ports in bits. This is the number of bits
            that are accssed by any one read/write. Total size = width * depth.
        depth: The number of entries in the cache, each with `width` bits. Total size =
            width * depth.
        size: The total size of the cache in bits.
        n_rw_ports: The number of read/write ports (each port supporting one read or
            one write per cycle). Bandwidth will increase with more ports.
        n_banks: The number of banks. Bandwidth will increase with more banks.
        associativity: int
            The associativity of the cache. This is the number of sets in the cache.
            Bandwidth will increase with more associativity.
        tag_size: int
            The number of bits of the tag used to check for cache misses and hits.
    """

    component_name = "cache"
    priority = 0.3

    def __init__(
        self,
        tech_node: float,
        width: int | None = None,
        depth: int | None = None,
        size: int | None = None,
        n_rw_ports: int = 1,
        n_banks: int = 1,
        associativity: int = 1,
        tag_size: Optional[int] = None,
    ):
        super().__init__(
            cache_type="cache",
            tech_node=tech_node,
            width=width,
            depth=depth,
            size=size,
            n_rw_ports=n_rw_ports,
            n_banks=n_banks,
            associativity=associativity,
            tag_size=tag_size,
        )

    @action(bits_per_action="width")
    def read(self) -> tuple[float, float]:
        """
        Returns the energy and latency for one cache read.

        Parameters
        ----------
        bits_per_action : int
            The number of bits to read.

        Returns
        -------
            (energy, latency): Tuple in (Joules, seconds).
        """
        self._interpolate_and_call_cacti()
        return self.read_energy, self._get_latency_per_bit() * self.width

    @action(bits_per_action="width")
    def write(self) -> tuple[float, float]:
        """
        Returns the energy and latency for one cache write.

        Parameters
        ----------
        bits_per_action : int
            The number of bits to write.

        Returns
        -------
            (energy, latency): Tuple in (Joules, seconds).
        """
        self._interpolate_and_call_cacti()
        return self.write_energy, self._get_latency_per_bit() * self.width

class EDRAM_3DCache(_MSXACMemory):
    """
    Standard 3T eDRAM cache modelled via MemSysExplorer's msxac (NVSim-extended).

    DESTINY, which this plug-in previously used, only ships a 1T1C eDRAM cell. The
    targets of this plug-in are 3T eDRAMs, so we invoke msxac with the calibrated
    3T eDRAM cell from MemSysExplorer's sample_cells/sample_edram3ts/.
    """
    component_name = ["EDRAM_3DCache", "3D_eDRAM"]
    priority = 0.8

    def __init__(self, size: int, width: int = 128, tech_node: float = 28e-9):
        capacity_bytes = size // 8

        super().__init__(
            tech_node=tech_node,
            capacity_bytes=capacity_bytes,
            width_bits=width,
            cell_file="sample_cells/sample_edram3ts/sample_eDRAM3T_28nm_cell.yaml",
            design_target="cache",
            optimization_target="WriteEDP",
            process_node=28,
            device_roadmap="HP",
            process_node_r=28,
            device_roadmap_r="HP",
            process_node_w=28,
            device_roadmap_w="LOP",
            associativity=8,
        )
        self.width = width

    @action(bits_per_action="width")
    def read(self) -> tuple[float, float]:
        return self.read_energy, self.read_latency

    @action(bits_per_action="width")
    def write(self) -> tuple[float, float]:
        return self.write_energy, self.write_latency

class EDRAM_333_Cache(_MSXACMemory):
    """
    333-eDRAM cache (monolithic 3D integration of 3 transistor types: 7nm LOP Si
    peripherals, 22nm CNFET read path, 45nm IGZO write path) modelled via msxac.

    The 333-eDRAM cell parameters are taken from MemSysExplorer's calibrated
    sample_cells/sample_edram3t_333/. Because msxac natively handles the mixed
    device stack (via ProcessNodeR/W + DeviceRoadmapR/W), we no longer need the
    post-hoc Table III scaling that the DESTINY-based version applied.
    """
    component_name = ["EDRAM333_Cache", "333EDRAM_Cache"]
    priority = 0.9

    def __init__(self, size: int, width: int = 128, tech_node: float = 7e-9):
        capacity_bytes = size // 8

        super().__init__(
            tech_node=tech_node,
            capacity_bytes=capacity_bytes,
            width_bits=width,
            cell_file="sample_cells/sample_edram3t_333/sample_eDRAM3T_333eDRAM_cell.yaml",
            design_target="cache",
            optimization_target="WriteEDP",
            process_node=7,
            device_roadmap="LOP",
            process_node_r=22,
            device_roadmap_r="CNT",
            process_node_w=45,
            device_roadmap_w="IGZO",
            associativity=8,
            retention_time_us=501,
        )
        self.width = width

    @action(bits_per_action="width")
    def read(self) -> tuple[float, float]:
        return self.read_energy, self.read_latency

    @action(bits_per_action="width")
    def write(self) -> tuple[float, float]:
        return self.write_energy, self.write_latency
