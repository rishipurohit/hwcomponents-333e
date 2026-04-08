import subprocess

cfg_content = """-DesignTarget: RAM
-ProcessNode: 22
-Capacity (KB): 1024
-WordWidth (bit): 64

-MemoryCellInputFile: test.cell
-OptimizationTarget: WriteEDP
"""
cell_content = """-MemCellType: DRAM
-CellArea (F^2): 33.1
"""

with open("test.cfg", "w") as f:
    f.write(cfg_content)
with open("test.cell", "w") as f:
    f.write(cell_content)

try:
    stdout = subprocess.check_output(
        ["hwcomponents_cacti/destiny_3d_cache/destiny", "test.cfg"],
        stderr=subprocess.STDOUT
    ).decode("utf-8")
    print(stdout[:500])
except subprocess.CalledProcessError as e:
    print(f"Error: {e.output.decode('utf-8')}")
