from logging import Logger
import math
import glob
import csv
import os
import fcntl
import subprocess
from typing import Callable, Optional
from hwcomponents import ComponentModel, action
import csv
import hashlib


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
-DesignTarget: {self.design_target}
-ProcessNode: {sn}
-Capacity (KB): {int(capacity_kb)}
-WordWidth (bit): {self.width_bits}

-MemoryCellInputFile: temp_destiny.cell

-OptimizationTarget: {self.optimization_target}
{self.additional_cfg}
"""
        cell_content = f"""
-MemCellType: {self.mem_cell_type}
-CellArea (F^2): {self.cell_area_f2}
{self.additional_cell}
"""
        temp_dir = _clean_tmp_dir()
        import hashlib
        input_name = hashlib.sha256((cfg_content + cell_content).encode()).hexdigest()
        
        cfg_path = os.path.join(temp_dir, input_name + ".cfg")
        cell_path = os.path.join(temp_dir, "temp_destiny.cell")
        out_path = os.path.join(temp_dir, input_name + "_out.log")
        
        import subprocess
        import re
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
        width: int,
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
            optimization_target="WriteEDP"
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

class EDRAM(_DestinyMemory):
    component_name = ["EDRAM", "eDRAM"]
    priority = 0.3

    def __init__(self, size: int, width: int, tech_node: float = 22e-9, cell_area_f2: float = 33.1, additional_cfg: str = "", additional_cell: str = ""):
        capacity_bytes = size // 8
        super().__init__(
            tech_node=tech_node,
            capacity_bytes=capacity_bytes,
            width_bits=width,
            design_target="RAM",
            mem_cell_type="eDRAM",
            cell_area_f2=cell_area_f2,
            optimization_target="WriteEDP",
            additional_cfg=additional_cfg,
            additional_cell=additional_cell
        )
        self.width = width

    @action(bits_per_action="width")
    def read(self) -> tuple[float, float]:
        return self.read_energy, self.read_latency

    @action(bits_per_action="width")
    def write(self) -> tuple[float, float]:
        return self.write_energy, self.write_latency

class EDRAM_333(EDRAM):
    component_name = ["EDRAM333", "EDRAM_333", "333EDRAM"]
    def __init__(self, size: int, width: int, tech_node: float = 7e-9):
        super().__init__(
            size=size,
            width=width,
            tech_node=tech_node,
            cell_area_f2=387.75,
            additional_cfg="\n-StackedDieCount: 1\n"
        )

    def _customize_destiny_outputs(self):
        # Incorporate paper-specific values from Table III and Table II
        # 333-eDRAM uses IGZO for low leakage (I_OFF ~10^-21 A/um),
        # CNFET for fast read (I_ON ~10^-4 A/um), and Si CMOS for write/peripherals.
        
        # Scaling against baseline Si eDRAM (from Table III):
        # Read Latency (RWL to SA Delay @ VSN=0.5 V): 489 ps vs 793 ps
        self.read_latency *= (489.0 / 793.0)
        
        # Write Latency (WWL to 95% VSN Delay): 251 ps vs 328 ps
        self.write_latency *= (251.0 / 328.0)
        
        # Energy per cycle (Memory): 13.6 pJ vs 21.3 pJ
        self.read_energy *= (13.6 / 21.3)
        self.write_energy *= (13.6 / 21.3)
        
        # Area (64 kB Memory Area): 0.025 mm^2 vs 0.068 mm^2
        # Monolithic 3D integration puts memory cells above peripherals
        self.destiny_area *= (0.025 / 0.068)
        
        # Leakage power / Retention Time:
        # Retention time is 501 us vs 51.3 us (Table III) due to IGZO's ultra low I_OFF.
        # Leakage is primarily limited by the Si CMOS peripheral circuits now.
        # We scale leakage proportionally to the reduction in peripheral size (dominated by area efficiency)
        # combined with the total capacitance reduction and ultra-low leakage path off-state.
        self.destiny_leak_power *= (51.3 / 501.0)



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
